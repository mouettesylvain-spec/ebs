"""`ebs reproduce ACTION` (P1-10 R2, R4, R5): the runner's exact sandbox, on a login node."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ebs.cli.reproduce import _status
from ebs.core.digest import hash_file
from ebs.core.errors import ExitCode
from ebs.core.tree import build_tree
from ebs.meta.api import BuildId
from ebs.meta.memory import InMemoryMetadataStore
from ebs.runner.collect import debug_action_dir
from ebs.runner.reproduce import OutputCheck, Sandbox
from ebs.runner.run import ToolRun
from ebs.runner.stage import JOB_MARKER, Scratch
from tests.helpers.cli import Site
from tests.helpers.repro import GEN, SIM, ReproBuild, write_repro_build, write_repro_site

SLURM = """\
[slurm]
default_partition = "short"
[slurm.account_map]
demo = "acct-demo"
[slurm.qos]
default = "normal"
"""


@pytest.fixture
def built(site: Site) -> ReproBuild:
    write_repro_site(site, copy_threshold=8, extra=SLURM)  # model's files (10 B) are CAS links
    return write_repro_build(site)


class _Foreground:
    """Records what `ebs reproduce` hands the terminal to (bash, salloc) instead of running it."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str] | None
    ) -> int:
        self.calls.append((list(argv), cwd, None if env is None else dict(env)))
        return 0


def _tree(root: Path) -> str:
    return str(build_tree(root, partial(hash_file, algo="sha256"))[0])


def _normalized(env: Mapping[str, str], scratch: Path) -> dict[str, str]:
    return {k: v.replace(str(scratch), "<scratch>") for k, v in env.items()}


def _reproduce(site: Site, *args: str) -> dict[str, Any]:
    result = site.invoke(["reproduce", *args, "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


# R2: same staging (copies and CAS links, config files, verified digests) and the same env as
# the runner, through the runner's own code.
def test_sandbox_equivalent_to_runner(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    dest = tmp_path / "repro"
    doc = _reproduce(site, SIM, "--dir", str(dest))
    kept = built.kept[SIM]
    assert doc["dir"] == str(dest)
    assert doc["work"] == str(dest / "work")
    assert _tree(dest / "work") == _tree(kept / "work")
    staged = dest / "work" / "model" / "sub" / "m.v"
    on_node = kept / "work" / "model" / "sub" / "m.v"
    assert staged.stat().st_ino == on_node.stat().st_ino  # both linked from the same CAS blob
    assert (dest / "work" / "out.txt").stat().st_nlink == 1  # small inputs are copied
    command = json.loads(
        (
            debug_action_dir(built.debug_root, "test", str(built.build), SIM) / "command.json"
        ).read_text()
    )
    assert _normalized(doc["env"], dest) == _normalized(command["env"], kept)
    assert doc["env"]["HOME"] == str(dest / "home")
    assert doc["env"]["LM_LICENSE_FILE"] == "1717@license.example"  # passthrough, as the runner
    assert doc["argv"] == list(built.spec(SIM).argv)
    assert doc["ran"] is None  # neither --run nor --interactive: only the sandbox

    gen_dir = tmp_path / "gen"
    gen = _reproduce(site, GEN, "--dir", str(gen_dir))
    spec = built.spec(GEN)
    gen_staged = [r.logical_path for r in spec.inputs] + list(spec.config_files)
    on_node = built.kept[GEN] / "work"  # also holds what gen produced: compare what was staged
    for rel in gen_staged:
        assert hash_file(gen_dir / "work" / rel)[0] == hash_file(on_node / rel)[0], rel
    top = {rel.split("/", 1)[0] for rel in gen_staged}
    assert {p.name for p in (gen_dir / "work").iterdir()} == top  # nothing else
    assert gen["env"]["TOOL_HOME"] == str(gen_dir / "home" / "tool")  # toolchain env, $HOME set


def _repro_dir(site: Site) -> Path:
    return site.services.runner_settings().scratch_dir / f"repro-{os.getuid()}"


# R2: by default the sandbox goes to local scratch; text mode says what it did. A second
# reproduction never replaces the first (it was promised to be kept).
def test_default_dir_and_text(site: Site, built: ReproBuild) -> None:
    result = site.invoke(["reproduce", GEN])
    assert result.exit_code == ExitCode.OK, result.output
    (first,) = [p for p in _repro_dir(site).iterdir() if p.is_dir()]
    assert str(first) in result.stdout
    assert "staged 1 input" in result.stdout
    assert "--interactive" in result.stdout  # how to go on
    assert stat.S_IMODE(_repro_dir(site).stat().st_mode) == 0o700
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert not (first / JOB_MARKER).exists()  # not under SLURM
    (first / "work" / "notes.txt").write_text("my edits\n")

    again = site.invoke(["reproduce", GEN])
    assert again.exit_code == ExitCode.OK, again.output
    roots = sorted(p for p in _repro_dir(site).iterdir() if p.is_dir())
    assert len(roots) == 2
    assert (first / "work" / "notes.txt").read_text() == "my edits\n"
    (second,) = [p for p in roots if p != first]
    assert str(second) in again.stdout


# R2 (I16): on a node (--salloc's inner run) the sandbox carries the epilog's job marker.
def test_sandbox_marked_for_epilog_under_slurm(
    site: Site, built: ReproBuild, tmp_path: Path
) -> None:
    site.services.environ["SLURM_JOB_ID"] = "777"
    doc = _reproduce(site, GEN)
    root = Path(doc["dir"])
    assert (root / JOB_MARKER).read_text() == "777\n"
    assert root.parent.parent == site.services.runner_settings().scratch_dir  # */*/.ebs-job
    explicit = _reproduce(site, GEN, "--dir", str(tmp_path / "mine"))
    assert not (Path(explicit["dir"]) / JOB_MARKER).exists()  # the user's own directory


# R2: --run runs the command in the sandbox; --interactive drops into bash --norc with its env.
def test_run_and_interactive(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    doc = _reproduce(site, SIM, "--dir", str(tmp_path / "a"), "--run")
    assert doc["ran"]["exit_code"] == 1
    assert doc["ran"]["status"] == "failed"
    assert "assertion failed" in (tmp_path / "a" / "logs" / "tool.log").read_text()

    shell = _Foreground()
    site.services.run_foreground = shell  # type: ignore[method-assign]
    result = site.invoke(["reproduce", SIM, "--dir", str(tmp_path / "b"), "--interactive"])
    assert result.exit_code == ExitCode.OK, result.output
    ((argv, cwd, env),) = shell.calls
    assert argv[-1] == "--norc"
    assert Path(argv[0]).name == "bash"
    assert cwd == tmp_path / "b" / "work"
    assert env is not None
    assert env["SEED"] == "7"
    assert env["HOME"] == str(tmp_path / "b" / "home")

    both = site.invoke(["reproduce", SIM, "--run", "--interactive"])
    assert both.exit_code == ExitCode.USAGE


# R2: --salloc re-runs `ebs reproduce` inside an allocation with the action's resources,
# licenses and account, so the sandbox is built on the node's local disk.
def test_salloc_argv(site: Site, built: ReproBuild) -> None:
    fg = _Foreground()
    site.services.run_foreground = fg  # type: ignore[method-assign]
    result = site.invoke(["reproduce", "sim[test=s", "--salloc", "--interactive"])
    assert result.exit_code == ExitCode.OK, result.output
    ((argv, _, env),) = fg.calls
    assert env is None  # the allocation inherits the user's environment
    split = argv.index("srun")
    assert argv[:split] == [
        "salloc",
        "--job-name=ebs:repro:sim",
        "--partition=short",
        "--account=acct-demo",
        "--qos=normal",
        "--cpus-per-task=2",
        "--mem=3072M",
        "--time=0-00:12:00",  # 600 s + the runner's grace
        "--licenses=simfeat@flexlm:1",
    ]
    inner = argv[split:]
    assert inner[:2] == ["srun", "--pty"]
    assert inner[3:] == ["reproduce", SIM, "--build", str(built.uuid), "--interactive"]
    assert Path(inner[2]).name == "ebs"
    assert not _repro_dir(site).exists()  # built on the node, by the inner command

    fg.calls.clear()
    as_json = site.invoke(["reproduce", SIM, "--salloc", "--run", "--json"])
    assert as_json.exit_code == ExitCode.OK, as_json.output
    ((argv, _, _),) = fg.calls
    assert argv[-2:] == ["--run", "--json"]  # the inner command prints the JSON document
    assert as_json.stdout == ""
    assert "salloc" in as_json.stderr

    no_mode = site.invoke(["reproduce", SIM, "--salloc"])
    assert no_mode.exit_code == ExitCode.USAGE
    assert "--run or --interactive" in no_mode.stderr


# P1-10 follow-up: --time replaces the allocation's time limit (the action's time + grace), e.g.
# for an interactive GUI session; it only makes sense with --salloc.
def test_salloc_time_override(site: Site, built: ReproBuild) -> None:
    fg = _Foreground()
    site.services.run_foreground = fg  # type: ignore[method-assign]
    result = site.invoke(["reproduce", SIM, "--salloc", "--interactive", "--time", "4h"])
    assert result.exit_code == ExitCode.OK, result.output
    ((argv, _, _),) = fg.calls
    assert "--time=0-04:00:00" in argv[: argv.index("srun")]
    alone = site.invoke(["reproduce", SIM, "--run", "--time", "4h"])
    assert alone.exit_code == ExitCode.USAGE
    assert "add --salloc" in alone.stderr
    bad = site.invoke(["reproduce", SIM, "--salloc", "--run", "--time", "soon"])
    assert bad.exit_code == ExitCode.USAGE
    assert "--time" in bad.stderr


# P1-10 follow-up: `--run` exits 1 when an output differs from the recorded result (CI can use
# the comparison directly), 0 when every compared output is the same.
def test_run_exit_code_follows_the_comparison(
    site: Site, built: ReproBuild, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    same = site.invoke(["reproduce", GEN, "--dir", str(tmp_path / "a"), "--run", "--json"])
    assert same.exit_code == ExitCode.OK, same.output
    assert json.loads(same.stdout)["ran"]["identical"] is True
    monkeypatch.setattr("ebs.cli.reproduce.identical", lambda checks: False)
    differs = site.invoke(["reproduce", GEN, "--dir", str(tmp_path / "b"), "--run", "--json"])
    assert differs.exit_code == ExitCode.ACTIONS_FAILED, differs.output
    assert json.loads(differs.stdout)["ran"]["identical"] is False


class _ReadOnlyStore(InMemoryMetadataStore):
    """Fails the test on any write: reproduce only reads results and resolves outputs."""

    def __init__(self, inner: InMemoryMetadataStore) -> None:
        self.__dict__.update(inner.__dict__)

    def _no(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("ebs reproduce wrote to the metadata store")

    cache_get = cache_put = cache_replace_failed = create_build = add_actions = _no
    set_action_state = record_result = finish_build = touch = register_blobs = _no
    acquire_lease = renew_lease = pin_build = unpin_build = emit = _no
    register_toolchain = touch_toolchain = _no


# R4: never the action cache, never the store; the sandbox is marked as a repro.
def test_no_cache_writes(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    store = site.services.memory_store
    before = store.cache_get("test", built.results[GEN].action_key)
    objects = sorted(str(d) for d, _ in built.cas.iter_digests())
    records = sorted(str(p) for p in (site.proj / ".ebs").rglob("*"))
    site.services.memory_store = _ReadOnlyStore(store)
    for action_id, name in ((GEN, "g"), (SIM, "s")):
        doc = _reproduce(site, action_id, "--dir", str(tmp_path / name), "--run")
        assert doc["ran"] is not None
        record = json.loads((tmp_path / name / "repro.json").read_text())
        assert record["kind"] == "repro"
        assert record["cache_mode"] == "off"
        assert record["build"] == str(built.uuid)
        assert record["action_id"] == action_id
        assert record["key"] == str(built.spec(action_id).key)
    site.services.memory_store = store
    assert sorted(str(d) for d, _ in built.cas.iter_digests()) == objects  # nothing uploaded
    assert sorted(str(p) for p in (site.proj / ".ebs").rglob("*")) == records  # no build record
    assert store.cache_get("test", built.results[GEN].action_key) == before
    assert store.get_result(BuildId(built.build), SIM) == built.results[SIM]


# R5: inputs that were garbage-collected are named, with what to do; nothing is staged.
def test_missing_inputs_message(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    gen_out = built.results[GEN].outputs
    built.cas.delete(gen_out["out"].digest)
    built.cas.delete(gen_out["model"].digest)
    dest = tmp_path / "repro"
    result = site.invoke(["reproduce", SIM, "--dir", str(dest)])
    assert result.exit_code == ExitCode.INFRA
    err = result.stderr
    assert "out.txt" in err
    assert str(gen_out["out"].digest) in err
    assert "model" in err
    assert "ebs build --no-cache" in err
    assert not dest.exists()

    # A nondeterministic input whose producer result is unknown cannot be found either.
    site.services.with_store = False
    no_store = site.invoke(["reproduce", SIM, "--dir", str(dest)])
    assert no_store.exit_code == ExitCode.USAGE
    assert "[metadata].url" in no_store.stderr
    assert not dest.exists()


# R5: a tree whose root manifest survived but one of whose files was swept is just as gone.
def test_partly_collected_tree(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    model = built.results[GEN].outputs["model"].digest
    (sub,) = built.cas.get_tree(model).entries
    assert sub.digest is not None
    (leaf,) = built.cas.get_tree(sub.digest).entries
    assert leaf.digest is not None
    built.cas.delete(leaf.digest)
    assert built.cas.has(model)
    dest = tmp_path / "repro"
    result = site.invoke(["reproduce", SIM, "--dir", str(dest)])
    assert result.exit_code == ExitCode.INFRA
    assert "model" in result.stderr
    assert "1 object(s) of it not in the CAS" in result.stderr
    assert "ebs build --no-cache" in result.stderr
    assert "out.txt" not in result.stderr  # only what is really gone
    assert not dest.exists()


# R5 / R2: a sandbox is never built over existing files.
def test_dir_must_be_empty(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine\n")
    result = site.invoke(["reproduce", GEN, "--dir", str(dest)])
    assert result.exit_code == ExitCode.USAGE
    assert "not empty" in result.stderr
    assert (dest / "keep.txt").read_text() == "mine\n"


# R3: --run concludes what the runner would: a log write error is infra, and a pass that
# lacks a required output is `failed` (as the runner's `collected.missing`).
def test_status_matches_runner(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    doc = _reproduce(site, GEN, "--dir", str(tmp_path / "s"))
    root = Path(doc["dir"])
    scratch = Scratch(root, root / "work", root / "home", root / "tmp", root / "logs")
    sandbox = Sandbox(scratch, {}, {})
    spec = built.spec(GEN)
    ok = ToolRun(0, False, 0.0, 0.0, 0, 0, None)
    missing = [OutputCheck("out", "missing", None, None)]
    assert _status(site.services, spec, ok, sandbox, []) == "passed"
    assert _status(site.services, spec, ok, sandbox, missing) == "failed"
    log_error = dataclasses.replace(ok, log_error="disk full")
    assert _status(site.services, spec, log_error, sandbox, []).startswith("infra")
