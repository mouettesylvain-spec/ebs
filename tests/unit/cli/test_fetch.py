from __future__ import annotations

import dataclasses
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from ebs.cli._records import BuildRecord
from ebs.cli.outputs import action_dir
from ebs.core.digest import hash_bytes
from ebs.core.errors import ExitCode
from ebs.meta.api import BuildCreate
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planfile import decode, encode
from tests.helpers.cli import Site, append_event, write_build
from tests.helpers.outputs import (
    CONTENT,
    MODEL,
    ODD,
    U1,
    Built,
    digest_of,
    edit_outputs,
    write_outputs_build,
)


def _ok(site: Site, *args: str) -> str:
    result = site.invoke(["fetch", *args])
    assert result.exit_code == ExitCode.OK, result.output
    return result.stdout


def _readonly(path: Path) -> bool:
    return stat.S_IMODE(path.lstat().st_mode) & 0o222 == 0


# R3
def test_file_output_bytes_and_readonly(site: Site) -> None:
    built = write_outputs_build(site)
    out = _ok(site, "gen.report")
    assert out == "gen.report -> ebs-out/gen/reports/gen.rpt\n"
    dest = site.proj / "ebs-out" / "gen" / "reports" / "gen.rpt"
    assert dest.read_bytes() == CONTENT[("gen", "report")]
    assert not dest.is_symlink()
    assert _readonly(dest)
    blob = built.cas.blob_path(digest_of(built, "gen", "report"))
    assert dest.stat().st_ino != blob.stat().st_ino  # a copy: does not alias the CAS

    doc = json.loads(site.invoke(["fetch", "sim[test=smoke].result", "-o", "o2", "--json"]).stdout)
    assert doc == {
        "v": 1,
        "uuid": str(U1),
        "build": built.build,
        "fetched": [
            {
                "action_id": "sim[test=smoke]",
                "name": "result",
                "digest": str(digest_of(built, "sim[test=smoke]", "result")),
                "dest": "o2/sim[test=smoke]/result.json",
            }
        ],
    }
    assert (site.proj / "o2" / "sim[test=smoke]" / "result.json").read_bytes() == CONTENT[
        ("sim[test=smoke]", "result")
    ]


# R3
def test_tree_output(site: Site, tmp_path: Path) -> None:
    write_outputs_build(site)
    out = _ok(site, "gen.model", "-o", str(tmp_path / "dl"))
    assert out == f"gen.model -> {tmp_path / 'dl' / 'gen' / 'model'}\n"
    model = tmp_path / "dl" / "gen" / "model"
    for rel, data in MODEL.items():
        assert (model / rel).read_bytes() == data
        assert _readonly(model / rel)
    assert sorted(p.relative_to(model).as_posix() for p in model.rglob("*")) == [
        "sub",
        "sub/lib.v",
        "top.v",
    ]


# R3 (R2: an output the plan does not declare goes to its name)
def test_all_outputs_when_name_omitted(site: Site) -> None:
    write_outputs_build(site)
    out = _ok(site, "gen")
    assert out.splitlines() == [
        "gen.extra -> ebs-out/gen/extra",
        "gen.model -> ebs-out/gen/model",
        "gen.report -> ebs-out/gen/reports/gen.rpt",
    ]
    base = site.proj / "ebs-out" / "gen"
    assert (base / "extra").read_bytes() == CONTENT[("gen", "extra")]
    assert (base / "model" / "top.v").read_bytes() == MODEL["top.v"]
    assert (base / "reports" / "gen.rpt").is_file()


# R3
def test_step_selects_all_actions(site: Site) -> None:
    write_outputs_build(site)
    # `sim` is also an ambiguous prefix: the step name wins
    assert _ok(site, "sim").splitlines() == [
        "sim[test=smoke].result -> ebs-out/sim[test=smoke]/result.json",
        "sim[test=random].result -> ebs-out/sim[test=random]/result.json",
    ]
    base = site.proj / "ebs-out"
    assert (base / "sim[test=random]" / "result.json").read_bytes() == CONTENT[
        ("sim[test=random]", "result")
    ]  # a test failure's outputs are fetchable
    assert _ok(site, "sim.result", "-o", "o2").splitlines() == [
        "sim[test=smoke].result -> o2/sim[test=smoke]/result.json",
        "sim[test=random].result -> o2/sim[test=random]/result.json",
    ]
    assert _ok(site, "sim[test=r", "-o", "o3").splitlines() == [  # a unique prefix
        "sim[test=random].result -> o3/sim[test=random]/result.json",
    ]


# R3
def test_action_dir_encoding(site: Site) -> None:
    write_outputs_build(site)
    assert action_dir("lint[block=alu]") == "lint[block=alu]"
    assert action_dir(ODD) == "x[v=a%2Fb%25c.d]"
    ids = ["a/b", "a%2Fb", "a%252Fb", "a%b", ".", "..", "%2E", "%2E%2E"]
    dirs = [action_dir(i) for i in ids]
    assert len(set(dirs)) == len(ids)  # distinct ids never share a dir
    assert all("/" not in d and d not in (".", "..") for d in dirs)
    # The whole argument is an action id first (it contains a dot and a slash)
    assert _ok(site, ODD) == "x[v=a/b%c.d].out -> ebs-out/x[v=a%2Fb%25c.d]/out.txt\n"
    assert (site.proj / "ebs-out" / "x[v=a%2Fb%25c.d]" / "out.txt").read_bytes() == b"odd\n"
    # ... and split at its last dot only when it names no action
    assert _ok(site, f"{ODD}.out", "-o", "o2").splitlines() == [
        "x[v=a/b%c.d].out -> o2/x[v=a%2Fb%25c.d]/out.txt"
    ]


# R3
def test_flat(site: Site) -> None:
    write_outputs_build(site)
    assert _ok(site, "gen.report", "sim[test=smoke]", "--flat").splitlines() == [
        "gen.report -> ebs-out/reports/gen.rpt",
        "sim[test=smoke].result -> ebs-out/result.json",
    ]
    assert (site.proj / "ebs-out" / "reports" / "gen.rpt").is_file()
    assert (site.proj / "ebs-out" / "result.json").is_file()


# R3
def test_mode_option(site: Site) -> None:
    built = write_outputs_build(site)
    blob = built.cas.blob_path(digest_of(built, "gen", "report"))
    _ok(site, "gen.report", "--mode", "symlink", "-o", "s")
    linked = site.proj / "s" / "gen" / "reports" / "gen.rpt"
    assert linked.is_symlink()
    assert linked.resolve() == blob.resolve()
    _ok(site, "gen.report", "--mode", "hardlink", "-o", "h")
    hard = site.proj / "h" / "gen" / "reports" / "gen.rpt"
    assert not hard.is_symlink()
    assert hard.stat().st_ino == blob.stat().st_ino
    _ok(site, "gen.report", "--mode", "copy", "-o", "c")
    assert (site.proj / "c" / "gen" / "reports" / "gen.rpt").stat().st_ino != blob.stat().st_ino
    bad = site.invoke(["fetch", "gen.report", "--mode", "auto"])
    assert bad.exit_code == ExitCode.USAGE
    assert not (site.proj / "ebs-out").exists()


# R4
def test_flat_collision_writes_nothing(site: Site) -> None:
    write_outputs_build(site)
    result = site.invoke(["fetch", "gen.report", "sim", "--flat"])
    assert result.exit_code == ExitCode.USAGE
    assert "sim[test=smoke]" in result.stderr
    assert "sim[test=random]" in result.stderr
    assert "--flat" in result.stderr
    assert not (site.proj / "ebs-out").exists()  # not even the first, unrelated output


# R4 (a dest inside another one is a collision too)
def test_nested_dests_write_nothing(site: Site) -> None:
    built = write_outputs_build(site)
    _rewrite_final_plan(site, built, "model/top.v")  # gen.report inside the gen.model tree
    result = site.invoke(["fetch", "gen"])
    assert result.exit_code == ExitCode.USAGE
    assert "gen.model" in result.stderr
    assert "gen.report" in result.stderr
    assert not (site.proj / "ebs-out").exists()


# R4
def test_existing_dest_needs_force(site: Site) -> None:
    write_outputs_build(site)
    _ok(site, "gen.report", "gen.model")
    report = site.proj / "ebs-out" / "gen" / "reports" / "gen.rpt"
    os.chmod(report, 0o644)
    report.write_bytes(b"edited\n")
    (site.proj / "ebs-out" / "gen" / "model" / "mine.txt").write_text("user file\n")

    again = site.invoke(["fetch", "gen.report", "gen.model"])
    assert again.exit_code == ExitCode.USAGE
    assert "--force" in again.stderr
    assert str(Path("ebs-out/gen/reports/gen.rpt")) in again.stderr
    assert report.read_bytes() == b"edited\n"  # untouched

    _ok(site, "gen.report", "gen.model", "--force")
    assert report.read_bytes() == CONTENT[("gen", "report")]
    assert _readonly(report)
    model = site.proj / "ebs-out" / "gen" / "model"
    assert not (model / "mine.txt").exists()  # the tree is replaced, not merged
    assert (model / "top.v").read_bytes() == MODEL["top.v"]


# R4 R5 (review: a file where a directory must go is found before anything is written)
def test_file_in_the_way_writes_nothing(site: Site) -> None:
    write_outputs_build(site)
    (site.proj / "ebs-out").mkdir()
    (site.proj / "ebs-out" / "gen").write_text("stray\n")
    result = site.invoke(["fetch", "sim[test=smoke]", "gen.report", "--force"])
    assert result.exit_code == ExitCode.USAGE
    assert "not a directory" in result.stderr
    assert not (site.proj / "ebs-out" / "sim[test=smoke]").exists()
    assert (site.proj / "ebs-out" / "gen").read_text() == "stray\n"


# R5 (review: --force that cannot remove the old dest is an actionable error)
def test_force_unremovable_dest(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    write_outputs_build(site)
    _ok(site, "gen.model")

    def denied(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("ebs.cli.outputs.shutil.rmtree", denied)
    result = site.invoke(["fetch", "gen.model", "--force"])
    assert result.exit_code == ExitCode.USAGE
    assert result.stderr.startswith("ebs: error: --force cannot remove ebs-out/gen/model")
    assert "Permission denied" in result.stderr


def _rewrite_final_plan(site: Site, built: Built, path: str) -> None:
    """Give `gen.report` the declared path `path` in the build's final plan."""
    record = BuildRecord.read(built.dir)
    assert record.final_plan is not None
    with built.cas.open(record.final_plan) as f:
        plan = decode(f.read())
    plan = edit_outputs(
        plan,
        lambda a, o: dataclasses.replace(o, path=path) if (a, o.name) == ("gen", "report") else o,
    )
    digest = built.cas.put_bytes(encode(plan))
    record.model_copy(update={"final_plan": digest}).write(built.dir)


# R4
@pytest.mark.parametrize("path", ["../../escaped.rpt", "/abs/escaped.rpt", "a/../../escaped.rpt"])
def test_path_escape_rejected(site: Site, path: str) -> None:
    built = write_outputs_build(site)
    _rewrite_final_plan(site, built, path)
    for flat in ([], ["--flat"]):
        result = site.invoke(["fetch", "gen.report", "-o", "dl/inner", *flat])
        assert result.exit_code == ExitCode.USAGE, result.output
        assert "outside" in result.stderr
    assert not any(p.name == "escaped.rpt" for p in site.root.rglob("*"))
    assert not (site.proj / "dl").exists()


# R4
def test_symlink_inside_dir_rejected(site: Site, tmp_path: Path) -> None:
    write_outputs_build(site)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (site.proj / "ebs-out").mkdir()
    (site.proj / "ebs-out" / "gen").symlink_to(outside)
    result = site.invoke(["fetch", "gen.report", "--force"])
    assert result.exit_code == ExitCode.USAGE
    assert "outside" in result.stderr
    assert list(outside.iterdir()) == []


# R5
@pytest.mark.parametrize(
    ("args", "code", "words"),
    [
        (["nope"], ExitCode.USAGE, ["nope"]),
        (["sim[test="], ExitCode.USAGE, ["sim[test=smoke]", "sim[test=random]"]),
        (["sim[test=x.y"], ExitCode.USAGE, ["no action, step or action output"]),
        (["gen.nope"], ExitCode.USAGE, ["nope", "extra, model, report"]),
        (["late"], ExitCode.USAGE, ["late", "running", "no result"]),
        (["gen", "--no-store"], ExitCode.USAGE, ["[metadata].url"]),
        (["gone"], ExitCode.INFRA, ["garbage-collected", "ebs build --no-cache gone"]),
    ],
    ids=["unknown-action", "ambiguous-prefix", "dotted-unknown", "unknown-output", "no-result",
         "no-metadata", "missing-blob"],
)  # fmt: skip
def test_errors(site: Site, args: list[str], code: ExitCode, words: list[str]) -> None:
    write_outputs_build(site)
    if "--no-store" in args:
        args = [a for a in args if a != "--no-store"]
        site.services.with_store = False
    result = site.invoke(["fetch", *args])
    assert result.exit_code == code, result.output
    for word in words:
        assert word in result.stderr
    assert result.stderr.startswith("ebs: error:")
    assert not (site.proj / "ebs-out").exists()


# R5 (review: an ambiguous prefix that contains a dot still lists the candidates)
def test_ambiguous_dotted_prefix(site: Site) -> None:
    write_build(site.proj, U1, {"a[v=1.5]": "a", "a[v=1.6]": "a"}, [], build=1)
    site.services.memory_store.create_build(
        BuildCreate(
            uuid=U1, domain="test", project="demo", plan_digest=hash_bytes(b"p"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice", cache_mode="read",
        )
    )  # fmt: skip
    result = site.invoke(["fetch", "a[v=1."])
    assert result.exit_code == ExitCode.USAGE
    assert "a[v=1.5]" in result.stderr
    assert "a[v=1.6]" in result.stderr


# R5
@pytest.mark.parametrize("state", ["infra_failed", "skipped", "cancelled"])
def test_no_result_states(site: Site, state: str) -> None:
    built = write_outputs_build(site)
    event = "infra_failed" if state == "infra_failed" else "finished"
    append_event(built.dir, event, "late", {"state": state, "reason": "node_fail"})
    result = site.invoke(["fetch", "late"])
    assert result.exit_code == ExitCode.USAGE
    assert state in result.stderr


# R5
def test_failure_part_way_leaves_no_partial_dest(site: Site) -> None:
    built = write_outputs_build(site)
    model = built.cas.get_tree(digest_of(built, "gen", "model"))
    built.cas.delete(
        next(e.digest for e in model.entries if e.type == "file" and e.digest is not None)
    )
    result = site.invoke(["fetch", "gen.report", "gen.model"])
    assert result.exit_code == ExitCode.INFRA
    assert "garbage-collected" in result.stderr
    assert (site.proj / "ebs-out" / "gen" / "reports" / "gen.rpt").is_file()  # earlier one stays
    assert not (site.proj / "ebs-out" / "gen" / "model").exists()


class _ReadOnlyStore:
    """Forwards reads to the in-memory store; records any other method that is called."""

    READS = frozenset({"get_result", "get_build", "list_actions"})

    def __init__(self, inner: InMemoryMetadataStore) -> None:
        self.inner = inner
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self.READS and not name.startswith("_") and name != "close":
            self.writes.append(name)
        return getattr(self.inner, name)


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes]]:
    snap = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        data = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
        snap[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, data)
    return snap


# R6
def test_read_only(site: Site) -> None:
    write_outputs_build(site)
    store = _ReadOnlyStore(site.services.memory_store)
    site.services.memory_store = store  # type: ignore[assignment]
    cas_before = _snapshot(site.root / "cas")
    builds_before = _snapshot(site.proj / ".ebs")
    assert site.invoke(["outputs"]).exit_code == ExitCode.OK
    assert site.invoke(["outputs", "--json"]).exit_code == ExitCode.OK
    _ok(site, "gen", "sim")
    _ok(site, "gen", "--force", "--mode", "hardlink")
    _ok(site, "gen.model", "--mode", "symlink", "-o", "s")
    assert site.invoke(["fetch", "gone"]).exit_code == ExitCode.INFRA
    # The only store call besides reads: each successful fetch records its use for GC
    # (P1-09 R1, deferred here by P1-13), which changes no cache, build or CAS state.
    assert store.writes == ["touch"] * 3
    assert _snapshot(site.root / "cas") == cas_before
    assert _snapshot(site.proj / ".ebs") == builds_before
