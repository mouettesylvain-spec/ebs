"""`ebs discover` CLI with a fake strace that writes a canned trace (task P3-02).

The real strace runs in tests/integration/sandbox/test_discover_make.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs.cli.discover import _write
from ebs.core.errors import ExitCode, FlowError
from ebs.sandbox.discover import Proposal
from tests.helpers.cli import Site, TestServices, write_site

# Records a fixed trace (the staged input, an undeclared file) to `-o LOG`, then runs the command.
FAKE_STRACE = """\
#!/bin/sh
while [ "$1" != "--" ]; do
  if [ "$1" = "-o" ]; then shift; log=$1; fi
  shift
done
shift
printf '1 openat(AT_FDCWD<%s>, "src/a.txt", O_RDONLY) = 3\\n' "$PWD" > "$log"
printf '1 openat(AT_FDCWD, "/elsewhere/.secret", O_RDONLY) = 3\\n' >> "$log"
exec "$@"
"""


def _site(tmp_path: Path, *, strace: bool = True, script: str = FAKE_STRACE) -> Site:
    write_site(tmp_path)
    services = TestServices(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if strace:
        fake = bin_dir / "strace"
        fake.write_text(script)
        fake.chmod(0o755)
    services.environ["PATH"] = str(bin_dir)
    return Site(tmp_path, services)


# R1, R2, R4
def test_report_patch_and_audit(tmp_path: Path) -> None:
    site = _site(tmp_path)
    flow = site.proj / "flow.yaml"
    before = flow.read_text()
    result = site.invoke(["discover", "gen"])
    assert result.exit_code == 0, result.output
    assert "discover gen: exit code 0" in result.stdout
    assert "/elsewhere/.secret  (pid 1 ?)" in result.stdout
    assert "      src: src/a.txt\n" in result.stdout
    assert "#   /elsewhere/.secret\n" in result.stdout
    assert flow.read_text() == before
    audit = site.invoke(["discover", "gen", "--audit", "--json"])
    assert audit.exit_code == ExitCode.ACTIONS_FAILED, audit.output
    doc = json.loads(audit.stdout)
    assert doc["action_id"] == "gen"
    assert [u["path"] for u in doc["undeclared"]] == ["/elsewhere/.secret"]
    assert doc["counts"]["input"] == 1
    assert doc["dir"] is None
    assert not list((tmp_path / "scratch").glob("repro-*/*"))


# R2: --write puts the proposal in the flow file; the rest of the file is kept.
def test_write(tmp_path: Path) -> None:
    site = _site(tmp_path)
    flow = site.proj / "flow.yaml"
    flow.write_text(flow.read_text().replace("steps:\n", "steps:  # all of them\n"))
    result = site.invoke(["discover", "gen", "--write", "--keep", "--json"])
    assert result.exit_code == 0, result.output
    text = flow.read_text()
    assert "inputs:\n      src: src/a.txt\n" in text
    assert "steps:  # all of them\n" in text
    assert "matrix: { table: tests.csv }" in text or "table: tests.csv" in text
    doc = json.loads(result.stdout)
    assert doc["written"] is True
    assert (Path(doc["dir"]) / "logs" / "strace.log").is_file()  # --keep


# R1: --row picks the matrix instance; one whose upstream never ran cannot be staged.
def test_row_selection(tmp_path: Path) -> None:
    site = _site(tmp_path)
    missing = site.invoke(["discover", "sim", "--row", "test=nope"])
    assert missing.exit_code == ExitCode.USAGE
    assert "no instance of 'sim' has test=nope" in missing.output
    pending = site.invoke(["discover", "sim", "--row", "test=random"])
    assert pending.exit_code == ExitCode.USAGE
    assert "sim[test=random]" in pending.output
    assert "run `ebs build sim` first" in pending.output
    bad = site.invoke(["discover", "sim", "--row", "random"])
    assert bad.exit_code == ExitCode.USAGE
    assert "expected COLUMN=VALUE" in bad.output
    unknown = site.invoke(["discover", "nosuch"])
    assert unknown.exit_code == ExitCode.USAGE, unknown.output


def test_strace_missing(tmp_path: Path) -> None:
    site = _site(tmp_path, strace=False)
    result = site.invoke(["discover", "gen"])
    assert result.exit_code == ExitCode.INFRA
    assert "needs strace" in result.output


# I16: a SIGTERM (a CI job timeout) while the step runs still removes the scratch.
def test_sigterm_removes_scratch(tmp_path: Path) -> None:
    signalled = FAKE_STRACE.replace('exec "$@"', "kill -TERM $PPID\nsleep 5")
    site = _site(tmp_path, script=signalled)
    result = site.invoke(["discover", "gen"])
    assert result.exit_code == ExitCode.CANCELLED, result.output
    assert list((tmp_path / "scratch").glob("repro-*")), "the sandbox was never built"
    assert not list((tmp_path / "scratch").glob("repro-*/*"))


# R4: a step that fails stops its trace early, so the audit cannot pass it.
def test_audit_fails_on_partial_trace(tmp_path: Path) -> None:
    failing = FAKE_STRACE.replace("/elsewhere/.secret", "/usr/lib/libc.so").replace(
        'exec "$@"', "exit 3"
    )
    site = _site(tmp_path, script=failing)
    clean = site.invoke(["discover", "gen", "--json"])
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["undeclared"] == []
    audit = site.invoke(["discover", "gen", "--audit"])
    assert audit.exit_code == ExitCode.ACTIONS_FAILED, audit.output
    assert "audit incomplete" in audit.output


# R2: strace that writes nothing must not be read as "nothing was read" (it would drop inputs).
def test_empty_trace_is_infra_error(tmp_path: Path) -> None:
    empty = FAKE_STRACE.replace('> "$log"', "> /dev/null").replace('>> "$log"', ">> /dev/null")
    site = _site(tmp_path, script=empty)
    flow = site.proj / "flow.yaml"
    before = flow.read_text()
    result = site.invoke(["discover", "gen", "--write"])
    assert result.exit_code == ExitCode.INFRA, result.output
    assert "strace wrote no trace" in result.output
    assert flow.read_text() == before
    assert not list((tmp_path / "scratch").glob("repro-*/*"))


# R1, R4: the layout wiring: a stale output make stats is not missing; a staged file the step
# needs but the flow does not declare is, and fails the audit; $HOME via a symlinked scratch is
# scratch.
def test_layout_wiring(tmp_path: Path) -> None:
    script = """#!/bin/sh
while [ "$1" != "--" ]; do
  if [ "$1" = "-o" ]; then shift; log=$1; fi
  shift
done
shift
printf '1 newfstatat(AT_FDCWD<%s>, "out.txt", 0x7ffd, 0) = -1 ENOENT\\n' "$PWD" > "$log"
printf '1 openat(AT_FDCWD, "%s/.rc", O_RDONLY) = 3\\n' "$HOME" >> "$log"
printf '1 openat(AT_FDCWD<%s>, "src/b.txt", O_RDONLY) = -1 ENOENT\\n' "$PWD" >> "$log"
exec "$@"
"""
    real = tmp_path / "real-scratch"
    real.mkdir()
    write_site(tmp_path, config_extra="")
    config = tmp_path / "config.toml"
    config.write_text(config.read_text().replace(str(tmp_path / "scratch"), str(tmp_path / "link")))
    (tmp_path / "link").symlink_to(real)
    services = TestServices(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "strace").write_text(script)
    (bin_dir / "strace").chmod(0o755)
    services.environ["PATH"] = str(bin_dir)
    site = Site(tmp_path, services)
    (site.proj / "out.txt").write_text("stale\n")  # gen's output, left in the checkout
    (site.proj / "src" / "b.txt").write_text("needed\n")
    result = site.invoke(["discover", "gen", "--audit", "--json"])
    assert result.exit_code == ExitCode.ACTIONS_FAILED, result.output
    doc = json.loads(result.stdout)
    assert [(u["path"], u["missing"]) for u in doc["undeclared"]] == [
        (str((site.proj / "src" / "b.txt").resolve()), True)
    ]
    assert doc["counts"]["scratch"] == 1  # $HOME/.rc through the symlinked scratch
    assert doc["proposal"]["inputs"]["discovered"] == "src/b.txt"


# R2: --write never leaves an invalid flow (or a temporary file) behind.
def test_write_rejects_invalid_flow(tmp_path: Path) -> None:
    write_site(tmp_path)
    flow = tmp_path / "proj" / "flow.yaml"
    before = flow.read_text()
    with pytest.raises(FlowError):
        _write(flow, "gen", Proposal(inputs={"x": 3}))
    assert flow.read_text() == before
    assert not list(flow.parent.glob(".flow.yaml.discover-*"))
