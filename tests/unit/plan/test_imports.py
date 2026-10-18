"""`${imports.alias}` in the planner, the runner and the build record (P2-03 R1-R3, R5)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.core.errors import ExitCode, MetadataError, PlanError
from ebs.meta._common import check_release_imports
from ebs.meta.api import ReleaseImport, ReleaseView
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planfile import decode, encode
from ebs.plan.types import ImportInput, InputRef, Plan, PlanLock
from ebs.release.lock import LockFile, lock_digest, plan_lock, resolve_lock
from ebs.runner.stage import resolve_contents, stage_inputs
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.imports import LINK, SRCS, publish
from tests.helpers.plan import flow, planner
from tests.helpers.release import finish

IMPORTS = {"rtl": {"from": "rtl-team/cpu", "channel": "stable"}}
LINT = {
    "kind": "shell",
    "command": ["lint", "${imports.rtl}/pack.srcs/rtl/files.f"],
    "inputs": {"srcs": "${imports.rtl}/pack.srcs/**/*.sv",
               "filelist": "${imports.rtl}/pack.srcs/rtl/files.f"},
    "outputs": {"report": "report.txt"},
}  # fmt: skip


@pytest.fixture
def store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore()


def _release(store: InMemoryMetadataStore, cas: FsCAS, tmp: Path) -> ReleaseView:
    return publish(
        store, cas, tmp / "rtl",
        {"pack.srcs": SRCS, "compile.worklib": {"lib/_info": "worklib\n"}, "gen.report": b"ok\n"},
        nondeterministic=frozenset({"compile.worklib"}), channel="stable",
    )  # fmt: skip


def _lock(store: InMemoryMetadataStore, imports: dict[str, Any] = IMPORTS) -> LockFile:
    return resolve_lock(store, flow({"s": LINT}, imports=imports), None)


def _plan(cas: FsCAS, tmp: Path, steps: dict[str, Any], lock: PlanLock | None,
          imports: dict[str, Any] = IMPORTS) -> Plan:  # fmt: skip
    p, snap = planner(cas, {"local.txt": "x\n"})
    plan = p.plan(flow(steps, imports=imports), base=tmp, lock=lock)
    assert all(not pattern.startswith("imports/") for _, pattern in snap.calls)  # never rehashed
    return plan


# R3: an import glob matches the files of the release's trees, read from the consumer domain's
# CAS; each is an input by the id the release recorded (no re-hashing), staged under
# imports/<alias>/<output>/; a whole output is one input with the release's id (for a
# `deterministic: false` output, not its content); `${imports.x}` renders the staging path.
def test_import_globs_resolve_by_id(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore
) -> None:
    release = _release(store, cas, tmp_path)
    lock = plan_lock(_lock(store))
    steps = {
        "lint": LINT,
        "elab": {"kind": "shell", "command": ["elab", "${imports.rtl}/compile.worklib"],
                 "inputs": {"lib": "${imports.rtl}/compile.worklib",
                            "rep": "${imports.rtl/gen.report}"},
                 "outputs": {"model": {"dir": "model"}}},
        "all": {"kind": "shell", "command": ["ls"], "inputs": {"rtl": "${imports.rtl}"},
                "outputs": {"o": "o.txt"}},
    }  # fmt: skip
    plan = _plan(cas, tmp_path, steps, lock)
    lint = plan.action("lint")
    want = ["rtl/a.sv", "rtl/b.sv", "rtl/sub/c.sv"]  # `*` never matches .hidden.sv
    files = {f"imports/rtl/pack.srcs/{p}": p for p in [*want, "rtl/files.f"]}
    assert [r.logical_path for r in lint.inputs] == sorted(files)
    for ref in lint.inputs:
        content = hash_bytes(SRCS[files[ref.logical_path]].encode())
        assert ref == InputRef(
            ref.logical_path, "file",
            ImportInput("rtl", f"pack.srcs/{files[ref.logical_path]}", content), content,
        )  # fmt: skip
    assert lint.argv == ("lint", "imports/rtl/pack.srcs/rtl/files.f")
    assert lint.key is not None

    worklib = release.outputs["compile.worklib"]
    report = release.outputs["gen.report"]
    elab = plan.action("elab")
    assert elab.argv == ("elab", "imports/rtl/compile.worklib")
    assert elab.inputs == (
        InputRef("imports/rtl/compile.worklib", "tree",
                 ImportInput("rtl", "compile.worklib", worklib.digest), worklib.id),
        InputRef("imports/rtl/gen.report", "file",
                 ImportInput("rtl", "gen.report", report.digest), report.id),
    )  # fmt: skip
    assert worklib.id != worklib.digest
    every = plan.action("all")
    assert [r.logical_path for r in every.inputs] == [
        "imports/rtl/compile.worklib", "imports/rtl/gen.report", "imports/rtl/pack.srcs",
    ]  # fmt: skip
    assert plan.lock == lock.digest

    # The key follows the release's ids: another release with other bytes is another key.
    changed = publish(store, cas, tmp_path / "rtl2",
                      {"pack.srcs": {**SRCS, "rtl/a.sv": "module a2; endmodule\n"},
                       "compile.worklib": {"lib/_info": "worklib\n"}, "gen.report": b"ok\n"},
                      version="2", channel="stable")  # fmt: skip
    assert changed.version == "2"
    again = _plan(cas, tmp_path, steps, plan_lock(_lock(store)))
    assert again.action("lint").key != lint.key

    # plan.json keeps the import sources (with their content) and the lock digest.
    decoded = decode(encode(plan))
    assert decoded == plan

    # The runner stages the recorded content: files, and the nondeterministic tree's bytes.
    contents = resolve_contents(plan, elab, None)
    assert contents == {"imports/rtl/compile.worklib": worklib.digest,
                        "imports/rtl/gen.report": report.digest}  # fmt: skip
    work = tmp_path / "work"
    stage_inputs(elab, cas, work, contents, copy_threshold=1 << 20)
    assert (work / "imports/rtl/compile.worklib/lib/_info").read_text() == "worklib\n"
    assert (work / "imports/rtl/gen.report").read_bytes() == b"ok\n"
    stage_inputs(lint, cas, work, resolve_contents(plan, lint, None), copy_threshold=1 << 20)
    assert (work / "imports/rtl/pack.srcs/rtl/sub/c.sv").read_text() == SRCS["rtl/sub/c.sv"]


# R3: what an import reference cannot resolve is a plan error naming the fix.
@pytest.mark.parametrize(
    ("inputs", "match"),
    [
        pytest.param({"x": "${imports.rtl}/pack.srcs/**/*.vhd"}, r"matches nothing in release "
                     r"rtl-team/cpu 1 \(import 'rtl'\)", id="no-match"),
        pytest.param({"x": "${imports.rtl}/nope.srcs"}, r"outputs: compile.worklib, gen.report, "
                     r"pack.srcs", id="unknown-output"),
        pytest.param({"x": "${imports.rlt}/pack.srcs"}, r"unknown import 'rlt'.*did you mean 'rtl'",
                     id="unknown-alias"),
        pytest.param({"x": "src/${imports.rtl}"}, r"must start the input value", id="not-a-prefix"),
        pytest.param({"x": "${imports.rtl}/../x"}, r"leaves the release", id="escape"),
    ],
)  # fmt: skip
def test_import_reference_errors(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore, inputs: dict[str, str], match: str
) -> None:
    _release(store, cas, tmp_path)
    step = {"kind": "shell", "command": ["x"], "inputs": inputs}
    with pytest.raises(PlanError, match=match):
        _plan(cas, tmp_path, {"s": step}, plan_lock(_lock(store)))


# R3: materialization reads the consumer domain's CAS: a release whose trees are not there
# (not exported into this domain) cannot be planned.
def test_release_not_in_consumer_cas(tmp_path: Path, store: InMemoryMetadataStore) -> None:
    other = FsCAS(tmp_path / "cas", "test")
    _release(store, other, tmp_path)
    consumer = FsCAS(tmp_path / "elsewhere", "test")
    with pytest.raises(PlanError, match=r"not in the CAS of domain 'test'.*ebs release export"):
        _plan(consumer, tmp_path, {"s": LINT}, plan_lock(_lock(store)))


def _stale(lock: LockFile, **changes: Any) -> LockFile:
    entry = lock.imports["rtl"].model_copy(update=changes)
    return LockFile(imports={"rtl": entry})


# R2: every import needs a lock entry that matches what the flow asks for; otherwise the plan
# fails pointing at `ebs lock update`. The planner gets no store: it cannot resolve a channel.
@pytest.mark.parametrize(
    ("case", "imports", "match"),
    [
        ("no-lock", IMPORTS, r"no flow.lock"),
        ("no-entry", IMPORTS, r"flow.lock has no entry for import 'rtl'"),
        ("other-name", IMPORTS, r"pins release other/cpu.*the flow imports rtl-team/cpu"),
        ("other-channel", {"rtl": {"from": "rtl-team/cpu", "channel": "nightly"}},
         r"pins .* from channel 'stable'.*the flow asks for channel 'nightly'"),
        ("other-version", {"rtl": {"from": "rtl-team/cpu", "version": "2"}},
         r"pins .* 1.*the flow asks for version 2"),
        ("version-via-channel", {"rtl": {"from": "rtl-team/cpu", "version": "1"}},
         r"pins .* 1.*the flow asks for version 1"),
        ("other-domain", IMPORTS, r"pins a release of domain 'partner'.*domain 'test'"),
    ],
)  # fmt: skip
def test_missing_or_stale_lock(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore, case: str,
    imports: dict[str, Any], match: str,
) -> None:  # fmt: skip
    _release(store, cas, tmp_path)
    lock = _lock(store)
    variants: dict[str, LockFile | None] = {
        "no-lock": None,
        "no-entry": LockFile(),
        "other-name": _stale(lock, name="other/cpu"),
        "other-channel": lock,
        "other-version": lock,
        "version-via-channel": lock,
        "other-domain": _stale(lock, domain="partner"),
    }
    chosen = variants[case]
    pinned = None if chosen is None else plan_lock(chosen)
    with pytest.raises(PlanError, match=match) as info:
        _plan(cas, tmp_path, {"s": LINT}, pinned, imports=imports)
    assert "ebs lock update rtl" in str(info.value)


# R1: plan.json records the digest of the lock it was planned with (content, not layout).
def test_lock_digest_in_plan(cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore) -> None:
    _release(store, cas, tmp_path)
    lock = _lock(store)
    plan = _plan(cas, tmp_path, {"s": LINT}, plan_lock(lock))
    assert plan.lock == lock_digest(lock)
    assert dataclasses.replace(plan, lock=None) != plan
    plain = planner(cas, {"local.txt": "x\n"})[0].plan(
        flow({"s": {"kind": "shell", "command": ["true"]}}), base=tmp_path
    )
    assert plain.lock is None  # no imports: no lock is read or recorded


VERIF = """\
version: 1
project: verif
domain: test
imports:
  rtl: { from: rtl-team/cpu, channel: stable }
steps:
  lint:
    kind: shell
    command: [cat, "${imports.rtl}/pack.srcs/rtl/a.sv"]
    inputs: { srcs: "${imports.rtl}/pack.srcs/**/*.sv" }
    outputs: { report: report.txt }
"""


# R5: a build records one `release_imports` row per import (alias -> the pinned release), the
# rows `ebs used-by` reads; the store refuses a release of another domain.
def test_release_imports_recorded(tmp_path: Path) -> None:
    write_site(tmp_path, flow=VERIF)
    site = Site(tmp_path, TestServices(tmp_path))
    store = site.services.memory_store
    cas = site.services.cas("test")
    release = publish(store, cas, tmp_path / "rtl", {"pack.srcs": SRCS}, channel="stable")
    missing = site.invoke(["build"])
    assert missing.exit_code == ExitCode.USAGE
    assert "ebs lock update" in missing.output
    assert site.invoke(["lock", "update"]).exit_code == ExitCode.OK
    built = site.invoke(["build", "--json"])
    assert built.exit_code == ExitCode.OK, built.output
    build = json.loads(built.stdout.splitlines()[-1])["build"]
    assert store.list_release_imports(build) == [ReleaseImport(alias="rtl", release=release.id)]
    planned = site.invoke(["plan", "--json"])
    assert planned.exit_code == ExitCode.OK, planned.output

    # store rules (shared by every backend)
    store.record_release_imports(build, [ReleaseImport(alias="rtl", release=release.id)])
    assert len(store.list_release_imports(build)) == 1  # idempotent
    newer = publish(store, cas, tmp_path / "r2", {"pack.srcs": SRCS}, version="2")
    with pytest.raises(MetadataError, match="already imports"):
        store.record_release_imports(build, [ReleaseImport(alias="rtl", release=newer.id)])
    partner = publish(store, FsCAS(tmp_path / "cas", "partner"), tmp_path / "p",
                      {"pack.srcs": SRCS}, name="vip/x")  # fmt: skip
    with pytest.raises(MetadataError, match=r"no release with id .* domain 'test'") as info:
        store.record_release_imports(build, [ReleaseImport(alias="vip", release=partner.id)])
    assert "partner" not in str(info.value)  # nothing about another domain's release
    assert "vip/x" not in str(info.value)


# R3: a nondeterministic *file* output has the release's id whether it is named or globbed;
# an output whose name holds glob characters (a matrix action's) is literal before its `/`;
# writable import inputs stay writable; a glob matching a symlink is refused (like sources).
def test_import_edge_cases(cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore) -> None:
    release = publish(
        store, cas, tmp_path / "rtl",
        {"gen.report": b"ok\n", "compile[lib=a].worklib": {"x.v": "x\n", "y.v": "y\n"},
         "pack.srcs": {"a.sv": "a\n", "l.sv": LINK + "a.sv"}},
        nondeterministic=frozenset({"gen.report"}), channel="stable",
    )  # fmt: skip
    lock = plan_lock(_lock(store))
    report = release.outputs["gen.report"]
    assert report.id != report.digest
    lib = "${imports.rtl}/compile[lib=a].worklib"
    steps = {
        "named": {"kind": "shell", "command": ["x"], "inputs": {"r": "${imports.rtl}/gen.report"}},
        "globbed": {"kind": "shell", "command": ["x"], "inputs": {"r": "${imports.rtl}/gen.*"}},
        "matrix": {"kind": "shell", "command": ["x"], "inputs": {"w": lib + "/*.v"}},
        "rw": {"kind": "shell", "command": ["x"],
               "inputs": {"w": {"from": lib, "writable": True},
                          "f": {"from": "${imports.rtl}/pack.srcs/a.sv", "writable": True}}},
        "whole": {"kind": "shell", "command": ["x"], "inputs": {"s": "${imports.rtl}/pack.srcs"}},
    }  # fmt: skip
    plan = _plan(cas, tmp_path, steps, lock)
    for action in ("named", "globbed"):
        [ref] = plan.action(action).inputs
        assert (ref.logical_path, ref.id) == ("imports/rtl/gen.report", report.id)
        assert isinstance(ref.source, ImportInput)
        assert ref.source.content == report.digest
        contents = resolve_contents(plan, plan.action(action), None)
        assert contents == {ref.logical_path: report.digest}
    matrix = plan.action("matrix").inputs
    assert [r.logical_path for r in matrix] == [
        "imports/rtl/compile[lib=a].worklib/x.v", "imports/rtl/compile[lib=a].worklib/y.v",
    ]  # fmt: skip
    assert [r.id for r in matrix] == [hash_bytes(b"x\n"), hash_bytes(b"y\n")]
    rw = plan.action("rw")
    assert [(r.logical_path, r.kind, r.writable) for r in rw.inputs] == [
        ("imports/rtl/compile[lib=a].worklib", "tree", True),
        ("imports/rtl/pack.srcs/a.sv", "file", True),
    ]
    work = tmp_path / "work"
    stage_inputs(rw, cas, work, resolve_contents(plan, rw, None), copy_threshold=1 << 20)
    staged = work / "imports/rtl/pack.srcs/a.sv"
    assert staged.stat().st_nlink == 1  # a private copy, never a link into the CAS
    staged.write_text("changed\n")
    assert [r.kind for r in plan.action("whole").inputs] == ["tree"]  # the link comes along
    globbed = {"kind": "shell", "command": ["x"], "inputs": {"s": "${imports.rtl}/pack.srcs/*"}}
    with pytest.raises(PlanError, match=r"matches the symlink 'pack.srcs/l.sv'"):
        _plan(cas, tmp_path, {"s": globbed}, lock)


# R3: more malformed import references.
@pytest.mark.parametrize(
    "value",
    ["${imports.rtl}pack.srcs", "${imports.rtl}/..", "${imports.rtl}/.",
     "${imports.rtl}/${imports.rtl}"],
)  # fmt: skip
def test_import_reference_shape(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore, value: str
) -> None:
    _release(store, cas, tmp_path)
    step = {"kind": "shell", "command": ["x"], "inputs": {"x": value}}
    with pytest.raises(PlanError, match=r"leaves the release|must start the input value"):
        _plan(cas, tmp_path, {"s": step}, plan_lock(_lock(store)))


# R1: a flow without imports records no lock digest, even when one is passed.
def test_no_imports_no_lock_digest(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore
) -> None:
    _release(store, cas, tmp_path)
    plain = planner(cas, {"local.txt": "x\n"})[0].plan(
        flow({"s": {"kind": "shell", "command": ["true"]}}), base=tmp_path,
        lock=plan_lock(_lock(store)),
    )  # fmt: skip
    assert plain.lock is None


# R5: store rules shared by every backend: one alias names one release, also within a batch,
# and a refused batch records nothing; already recorded rows are not new.
def test_release_import_batch_rules(
    cas: FsCAS, tmp_path: Path, store: InMemoryMetadataStore
) -> None:
    one = _release(store, cas, tmp_path)
    two = publish(store, cas, tmp_path / "r2", {"pack.srcs": SRCS}, version="2")
    build = finish(store, {}, status=None)
    rows = [ReleaseImport(alias="rtl", release=one.id), ReleaseImport(alias="rtl", release=two.id)]
    with pytest.raises(MetadataError, match="already imports"):
        store.record_release_imports(build, rows)
    assert store.list_release_imports(build) == []
    row = rows[0]
    known = {int(one.id): one}
    assert check_release_imports(build, "test", [row], known, {"rtl": int(one.id)}) == []
    assert check_release_imports(build, "test", [row], known, {}) == [row]
