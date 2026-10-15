"""Debug collection into `/debug/<domain>/<build>/<action>/` (P1-04 R2, R6)."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.plan.types import DebugSpec
from ebs.runner.collect import TRUNCATED_NOTE, collect_debug, debug_action_dir
from ebs.runner.main import EXIT_INFRA, EXIT_OK
from tests.helpers.runner import DOMAIN, Harness, shell_spec, source_file

LICENSE_FAIL = "echo 'license checkout failed'\nexit 1\n"
LOG = "echo logged; echo hi > a.log\n"


@pytest.fixture
def debug_root(tmp_path: Path) -> Path:
    root = tmp_path / "debug"
    (root / DOMAIN).mkdir(parents=True)  # the admin creates the domain dir (D7)
    return root


def _harness(tmp_path: Path, cas: FsCAS, debug_root: Path, **overrides: object) -> Harness:
    return Harness(tmp_path, cas, settings_overrides={"debug_root": debug_root, **overrides})


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


# R2
@pytest.mark.parametrize(
    ("script", "on_success", "code", "collected"),
    [
        pytest.param(LOG, False, EXIT_OK, False, id="passed"),
        pytest.param(LOG, True, EXIT_OK, True, id="passed-on_success"),
        pytest.param(LOG + "exit 3\n", False, EXIT_OK, True, id="failed"),
        pytest.param(LOG + LICENSE_FAIL, False, EXIT_INFRA, True, id="infra"),
    ],
)
def test_rules(
    cas: FsCAS,
    tmp_path: Path,
    debug_root: Path,
    script: str,
    on_success: bool,
    code: int,
    collected: bool,
) -> None:
    spec = shell_spec(script, inputs=[source_file(cas, "in.txt", b"input")])
    spec = dataclasses.replace(spec, debug=DebugSpec(("*.log", "missing/*"), None, on_success))
    h = _harness(tmp_path, cas, debug_root)
    rc, build = h.run(spec)
    assert rc == code, h.err.getvalue()
    dest = debug_action_dir(debug_root, DOMAIN, str(build), spec.action_id)
    files = _files(dest) if dest.exists() else {}
    files.pop("live.log", None)  # R3, tested in test_livelog.py
    if not collected:
        assert files == {}
        return
    assert files["work/a.log"] == b"hi\n"
    assert b"logged" in files["tool.log"]  # always included
    command = json.loads(files["command.json"])
    assert command["action_id"] == spec.action_id
    assert command["argv"] == list(spec.argv)
    assert command["env"]["EBS_ACTION_ID"] == spec.action_id
    assert command["inputs"] == [{"path": "in.txt", "kind": "file", "id": str(spec.inputs[0].id)}]
    assert set(files) == {"work/a.log", "tool.log", "command.json"}


# R2: inputs (even when they match a glob) and symlinks are not debug output.
def test_inputs_not_collected(cas: FsCAS, tmp_path: Path, debug_root: Path) -> None:
    spec = shell_spec(
        "echo out > b.log\nln -s /etc/hostname link.log\nexit 1\n",
        inputs=[source_file(cas, "in.log", b"input")],
    )
    spec = dataclasses.replace(spec, debug=DebugSpec(("*.log",), None, False))
    h = _harness(tmp_path, cas, debug_root)  # the small input is a copy in work/
    rc, build = h.run(spec)
    assert rc == EXIT_OK
    files = _files(debug_action_dir(debug_root, DOMAIN, str(build), spec.action_id))
    assert {k for k in files if k.startswith("work/")} == {"work/b.log"}


def _work(tmp_path: Path, sizes: dict[str, int]) -> Path:
    work = tmp_path / "work"
    for name, size in sizes.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct, position-dependent bytes so a kept tail can be checked exactly.
        path.write_bytes(bytes((i * 7 + len(name)) % 251 for i in range(size)))
    return work


# R2
def test_size_cap(tmp_path: Path) -> None:
    work = _work(tmp_path, {"small.txt": 100, "mid.txt": 3000, "big/huge.txt": 50_000})
    log = tmp_path / "tool.log"
    log.write_bytes(b"L" * 20_000)
    dest = tmp_path / "dest"
    dest.mkdir()
    cap = 12_000

    result = collect_debug(
        dest,
        work=work,
        patterns=("*.txt", "big"),
        tool_log=log,
        command={"argv": ["x"]},
        max_size=cap,
    )

    files = _files(dest)
    assert sum(len(b) for b in files.values()) <= cap
    assert files["work/small.txt"] == (work / "small.txt").read_bytes()  # small files kept whole
    assert files["work/mid.txt"] == (work / "mid.txt").read_bytes()
    assert json.loads(files["command.json"]) == {"argv": ["x"]}  # always complete
    for name, original in (("work/big/huge.txt", work / "big/huge.txt"), ("tool.log", log)):
        data = files[name]
        first, _, rest = data.partition(b"\n")
        assert first.decode().startswith(TRUNCATED_NOTE.split("{", 1)[0])
        assert rest == original.read_bytes()[-len(rest) :]  # the tail is kept
        assert len(rest) > 1000
    assert set(result.truncated) == {"work/big/huge.txt", "tool.log"}


# R2: without a cap everything is copied.
def test_no_cap(tmp_path: Path) -> None:
    work = _work(tmp_path, {"a.txt": 10_000})
    log = tmp_path / "tool.log"
    log.write_bytes(b"log")
    dest = tmp_path / "dest"
    dest.mkdir()
    collect_debug(dest, work=work, patterns=("a.txt",), tool_log=log, command={}, max_size=None)
    assert (dest / "work/a.txt").read_bytes() == (work / "a.txt").read_bytes()
    assert (dest / "tool.log").read_bytes() == b"log"


@pytest.fixture
def strict_umask() -> Iterator[None]:
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def _other_group() -> int:
    """A group of this process other than its primary one, if it has one."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    return others[0] if others else os.getgid()


# R6: dirs 2750 and files 0640, in the domain dir's group, whatever the umask.
def test_permissions(cas: FsCAS, tmp_path: Path, debug_root: Path, strict_umask: None) -> None:
    domain_dir = debug_root / DOMAIN
    gid = _other_group()
    os.chown(domain_dir, -1, gid)
    domain_dir.chmod(0o2750)
    spec = shell_spec("mkdir -p sub; echo x > sub/a.log\nexit 2\n")
    spec = dataclasses.replace(spec, debug=DebugSpec(("sub",), None, False))
    h = _harness(tmp_path, cas, debug_root)
    rc, build = h.run(spec)
    assert rc == EXIT_OK, h.err.getvalue()

    build_dir = domain_dir / str(build)
    seen_files = 0
    for dirpath, dirnames, filenames in os.walk(build_dir):
        st = os.stat(dirpath)
        assert stat.S_IMODE(st.st_mode) == 0o2750, dirpath
        assert st.st_gid == gid, dirpath
        for name in filenames:
            fst = os.stat(Path(dirpath) / name)
            assert stat.S_IMODE(fst.st_mode) == 0o640, name
            assert fst.st_gid == gid, name
            seen_files += 1
        del dirnames  # walked by os.walk
    assert seen_files >= 4  # tool.log, command.json, live.log, work/sub/a.log


# R2: a debug area that cannot be written never turns a result into a failure.
def test_missing_debug_domain_dir(cas: FsCAS, tmp_path: Path) -> None:
    h = _harness(tmp_path, cas, tmp_path / "no-such-debug-root")
    spec = dataclasses.replace(shell_spec("exit 1\n"), debug=DebugSpec(("*",), None, False))
    rc, build = h.run(spec)
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "failed"
    assert "debug" in h.err.getvalue()
    assert h.scratch_entries() == []


def _dir_size(root: Path) -> int:
    return sum(len(b) for b in _files(root).values())


# R2: the whole debug dir, live log included, stays within max_size.
@pytest.mark.parametrize(
    "cap", [3000, 30_000, 45_000, 60_000], ids=["tiny", "live-dropped", "tight", "live-kept"]
)
def test_total_within_max_size(cas: FsCAS, tmp_path: Path, debug_root: Path, cap: int) -> None:
    script = (
        "head -c 20000 /dev/zero | tr '\\0' x; echo; head -c 9000 /dev/zero > big.out\nexit 1\n"
    )
    spec = dataclasses.replace(shell_spec(script), debug=DebugSpec(("*.out",), cap, False))
    h = _harness(tmp_path, cas, debug_root)
    rc, build = h.run(spec)
    assert rc == EXIT_OK, h.err.getvalue()
    dest = debug_action_dir(debug_root, DOMAIN, str(build), spec.action_id)
    files = _files(dest)
    assert _dir_size(dest) <= cap
    assert {"tool.log", "command.json", "work/big.out"} <= set(files)
    # The live log (~20 kB) is kept only when it fits in half the cap, and then counts in it.
    assert ("live.log" in files) == (cap >= 45_000)
    if cap == 45_000:
        assert files["tool.log"].startswith(TRUNCATED_NOTE.split("{", 1)[0].encode())


# R2: one file the tool left unreadable is skipped; the rest is still collected.
def test_unreadable_file_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    work = _work(tmp_path, {"a.txt": 10, "b.txt": 10, "c.txt": 10})
    log = tmp_path / "tool.log"
    log.write_bytes(b"log")
    dest = tmp_path / "dest"
    dest.mkdir()
    real_open = builtins.open

    def failing_open(file: object, *args: object, **kwargs: object) -> object:
        if str(file).endswith("b.txt") and str(file).startswith(str(work)):
            raise PermissionError(13, "Permission denied", str(file))  # mode 000 (root ignores it)
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(builtins, "open", failing_open)
    result = collect_debug(
        dest, work=work, patterns=("*.txt",), tool_log=log, command={}, max_size=None
    )
    assert result.unreadable == ("work/b.txt",)
    assert (dest / "work/a.txt").exists()
    assert (dest / "work/c.txt").exists()
    assert not (dest / "work/b.txt").exists()
    assert (dest / "tool.log").read_bytes() == b"log"


# R2: patterns that would leave the work dir (only a hand-made plan can hold them) match nothing.
def test_escaping_patterns_ignored(tmp_path: Path) -> None:
    work = _work(tmp_path, {"in/a.txt": 5})
    (tmp_path / "secret.txt").write_text("s")
    log = tmp_path / "tool.log"
    log.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    result = collect_debug(
        dest,
        work=work,
        patterns=("../secret.txt", str(tmp_path / "secret.txt"), "in/../../*.txt"),
        tool_log=log,
        command={},
        max_size=None,
    )
    assert set(result.files) == {"command.json", "tool.log"}


# R2: a symlink inside a collected dir is never followed (it could point into another domain).
def test_symlink_inside_collected_dir_skipped(cas: FsCAS, tmp_path: Path, debug_root: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"other-domain-bytes")
    script = (
        f"mkdir sub; echo ok > sub/ok.log; ln -s {secret} sub/leak.log; ln -s / sub/root\nexit 1\n"
    )
    spec = dataclasses.replace(shell_spec(script), debug=DebugSpec(("sub",), None, False))
    h = _harness(tmp_path, cas, debug_root)
    rc, build = h.run(spec)
    assert rc == EXIT_OK
    files = _files(debug_action_dir(debug_root, DOMAIN, str(build), spec.action_id))
    assert {k for k in files if k.startswith("work/")} == {"work/sub/ok.log"}
    assert all(b"other-domain-bytes" not in data for data in files.values())


# R2: an excluded input path does not exclude its siblings that share a prefix.
def test_exclude_is_by_path(tmp_path: Path) -> None:
    work = _work(tmp_path, {"a.log": 3, "a.log2": 3, "d/x.log": 3, "d2/y.log": 3})
    log = tmp_path / "tool.log"
    log.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    result = collect_debug(
        dest,
        work=work,
        patterns=("**/*.log*",),
        tool_log=log,
        command={},
        max_size=None,
        exclude=["a.log", "d"],
    )
    assert {f for f in result.files if f.startswith("work/")} == {"work/a.log2", "work/d2/y.log"}


# R2: a retry's collection replaces the earlier attempt's files.
def test_retry_replaces_work_files(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    log.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    for name in ("a.log", "b.log"):
        work = tmp_path / f"work-{name}"
        work.mkdir()
        (work / name).write_text(name)
        collect_debug(dest, work=work, patterns=("*.log",), tool_log=log, command={}, max_size=None)
    assert sorted(p.name for p in (dest / "work").iterdir()) == ["b.log"]


# R2/R4: after SIGTERM only command.json and the log tail are kept, so cleanup fits KillWait.
def test_sigterm_keeps_only_log_and_command(cas: FsCAS, tmp_path: Path, debug_root: Path) -> None:

    import signal

    from ebs.runner.errors import Interrupted
    from ebs.runner.main import EXIT_INFRA

    spec = shell_spec(
        "echo before; head -c 100000 /dev/zero > big.log\nkill -TERM $PPID; sleep 30\n"
    )
    spec = dataclasses.replace(spec, debug=DebugSpec(("*.log",), None, False))
    h = _harness(tmp_path, cas, debug_root)

    def handler(signum: int, frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise Interrupted(signum)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        # $PPID of the tool is this test process: the runner runs in-process here.
        rc, build = h.run(spec)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert rc == EXIT_INFRA, h.err.getvalue()
    dest = debug_action_dir(debug_root, DOMAIN, str(build), spec.action_id)
    files = _files(dest)
    assert {"command.json", "tool.log"} <= set(files)
    assert not any(k.startswith("work/") for k in files)
    assert b"before" in files["tool.log"]


# R6: the group is set explicitly, not only inherited through a setgid domain dir.
def test_group_applied_without_setgid(
    cas: FsCAS, tmp_path: Path, debug_root: Path, strict_umask: None
) -> None:
    gid = _other_group()
    if gid == os.getgid():
        pytest.skip("this user has no supplementary group to test with")
    domain_dir = debug_root / DOMAIN
    os.chown(domain_dir, -1, gid)
    domain_dir.chmod(0o750)  # no setgid bit: children would get the primary group
    spec = dataclasses.replace(
        shell_spec("echo x > a.log\nexit 2\n"), debug=DebugSpec(("*.log",), None, False)
    )
    h = _harness(tmp_path, cas, debug_root)
    rc, build = h.run(spec)
    assert rc == EXIT_OK, h.err.getvalue()
    for dirpath, _dirs, filenames in os.walk(domain_dir / str(build)):
        st = os.stat(dirpath)
        assert (stat.S_IMODE(st.st_mode), st.st_gid) == (0o2750, gid), dirpath
        for name in filenames:
            fst = os.stat(Path(dirpath) / name)
            assert (stat.S_IMODE(fst.st_mode), fst.st_gid) == (0o640, gid), name
