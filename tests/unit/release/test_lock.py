"""`flow.lock`: format, `ebs lock update` and `ebs lock check` (P2-03 R1, R4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import ExitCode, ReleaseError
from ebs.flow.model import Flow
from ebs.meta.memory import InMemoryMetadataStore
from ebs.release.lock import (
    LockedOutput,
    LockEntry,
    LockFile,
    _release_digest,
    diff_locks,
    dump_lock,
    import_rows,
    lock_digest,
    parse_lock,
    release_digest,
    resolve_lock,
)
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.imports import SRCS, publish

VERIF = """\
version: 1
project: verif
domain: test
imports:
  rtl: { from: rtl-team/cpu, channel: stable }
  vip: { from: vip/axi, version: "2.0" }
steps:
  lint:
    kind: shell
    command: [cat, "${imports.rtl}/pack.srcs/rtl/a.sv"]
    inputs: { srcs: "${imports.rtl}/pack.srcs/**/*.sv" }
    outputs: { report: report.txt }
"""


def _flow(imports: dict[str, dict[str, str]]) -> Flow:
    return Flow.model_validate(
        {"version": 1, "project": "verif", "domain": "test", "imports": imports,
         "steps": {"s": {"kind": "shell", "command": ["true"]}}}
    )  # fmt: skip


def _site(tmp_path: Path) -> Site:
    write_site(tmp_path, flow=VERIF)
    return Site(tmp_path, TestServices(tmp_path))


def _publish_both(site: Site, tmp: Path, version: str = "1", srcs: dict[str, str] = SRCS) -> None:
    store, cas = site.services.memory_store, site.services.cas("test")
    publish(
        store,
        cas,
        tmp / f"rtl-{version}",
        {"pack.srcs": srcs, "gen.report": b"ok\n"},
        version=version,
        channel="stable",
        nondeterministic=frozenset({"gen.report"}),
    )
    if store.get_release("test", "vip/axi", "2.0") is None:
        publish(store, cas, tmp / "vip", {"pack.vip": {"axi.sv": "module axi; endmodule\n"}},
                name="vip/axi", version="2.0")  # fmt: skip


# R1: sorted keys, one release per alias with domain, name, version, release digest and output
# ids; dump(parse(text)) == text, parse(dump(lock)) == lock; the digest ignores YAML layout.
def test_format_roundtrip_stable(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _publish_both(site, tmp_path)
    store = site.services.memory_store
    flow = _flow({"vip": {"from": "vip/axi", "version": "2.0"},
                  "rtl": {"from": "rtl-team/cpu", "channel": "stable"}})  # fmt: skip
    lock = resolve_lock(store, flow, None)
    text = dump_lock(lock)
    assert parse_lock(text) == lock
    assert dump_lock(parse_lock(text)) == text
    lines = text.splitlines()
    assert lines[0].startswith("#")  # a header saying how the file is written
    assert lines.index("  rtl:") < lines.index("  vip:")  # aliases sorted, not flow order
    rtl = lines[lines.index("  rtl:") + 1 : lines.index("  vip:")]
    keys = [line.strip().split(":")[0] for line in rtl if line.startswith("    ") and
            not line.startswith("     ")]  # fmt: skip
    assert keys == ["channel", "domain", "name", "outputs", "release", "version"]
    view = store.get_release("test", "rtl-team/cpu", "1")
    assert view is not None
    entry = lock.imports["rtl"]
    assert (entry.domain, entry.name, entry.version, entry.channel) == (
        "test", "rtl-team/cpu", "1", "stable",
    )  # fmt: skip
    assert entry.release == release_digest(view)
    assert {n: (o.id, o.digest) for n, o in entry.outputs.items()} == {
        n: (o.id, o.digest) for n, o in view.outputs.items()
    }
    assert entry.outputs["gen.report"].id != entry.outputs["gen.report"].digest  # both kept
    assert lock.imports["vip"].channel is None  # pinned by version
    assert "version: '1'" in text  # versions stay strings
    # The digest is over the content, not the bytes: another layout gives the same digest.
    relaid = text.replace("  ", "    ")
    assert parse_lock(relaid) == lock
    assert lock_digest(parse_lock(relaid)) == lock_digest(lock)
    other = resolve_lock(store, _flow({"rtl": {"from": "rtl-team/cpu", "channel": "stable"}}),
                         None)  # fmt: skip
    assert lock_digest(other) != lock_digest(lock)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param("version: 2\nimports: {}\n", "version", id="format-version"),
        pytest.param("imports: [1]\n", "imports", id="not-a-map"),
        pytest.param("version: 1\nimports: {rtl: {name: x}}\n", "rtl", id="missing-fields"),
        pytest.param("version: 1\nimports: {}\nextra: 1\n", "extra", id="unknown-key"),
        pytest.param("version: 1\nimports: {: [\n", "flow.lock", id="bad-yaml"),
    ],
)
def test_invalid_lock_is_an_actionable_error(text: str, match: str) -> None:
    with pytest.raises(ReleaseError, match=match) as info:
        parse_lock(text)
    assert "ebs lock update" in str(info.value)


_digests = st.binary(min_size=1, max_size=8).map(hash_bytes)
_names = st.from_regex(r"[a-z][a-z0-9_.-]{0,8}(/[a-z][a-z0-9_.-]{0,8})?", fullmatch=True)
_versions = st.from_regex(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,10}", fullmatch=True)
_outputs = st.dictionaries(
    st.from_regex(r"[a-z][a-z0-9_]{0,5}(\[[a-z]=[a-z0-9.]{1,4}\])?\.[a-z][a-z0-9_]{0,5}",
                  fullmatch=True),
    st.builds(LockedOutput, digest=_digests, id=_digests,
              size=st.integers(min_value=0, max_value=2**40),
              type=st.sampled_from(["file", "tree"])),
    min_size=1, max_size=3,
)  # fmt: skip
_entries = st.builds(
    LockEntry,
    domain=st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True),
    name=_names,
    version=_versions,
    channel=st.none() | st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True),
    release=_digests,
    outputs=_outputs,
).map(lambda e: e.model_copy(update={"release": _release_digest(e.name, e.version, e.outputs)}))


# R1 (property): any lock survives dump -> parse unchanged, and dumping is a fixed point.
@given(st.dictionaries(st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,8}", fullmatch=True), _entries,
                       max_size=3))  # fmt: skip
def test_roundtrip_property(imports: dict[str, LockEntry]) -> None:
    lock = LockFile(imports=imports)
    text = dump_lock(lock)
    assert parse_lock(text) == lock
    assert dump_lock(parse_lock(text)) == text
    assert lock_digest(parse_lock(text)) == lock_digest(lock)


# R1/R5: the planner stages `outputs` and the build records `release`; a hand edit that swaps
# outputs under a release digest is refused, so the recorded release is the one staged.
def test_hand_edited_outputs_refused(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _publish_both(site, tmp_path)
    _publish_both(site, tmp_path, version="2", srcs={**SRCS, "rtl/a.sv": "module x; endmodule\n"})
    store = site.services.memory_store
    flow = _flow({"rtl": {"from": "rtl-team/cpu", "version": "1"}})
    lock = resolve_lock(store, flow, None)
    v2 = store.get_release("test", "rtl-team/cpu", "2")
    assert v2 is not None
    src = v2.outputs["pack.srcs"]
    swapped = (
        lock.imports["rtl"]
        .outputs["pack.srcs"]
        .model_copy(update={"digest": src.digest, "id": src.id})
    )
    entry = lock.imports["rtl"]
    edited = LockFile(imports={"rtl": entry.model_copy(
        update={"outputs": {**entry.outputs, "pack.srcs": swapped}})})  # fmt: skip
    with pytest.raises(ReleaseError, match=r"edited by hand.*ebs lock update rtl"):
        parse_lock(dump_lock(edited))
    assert parse_lock(dump_lock(lock)) == lock


# R4: `lock update` resolves channels and versions, shows old -> new version and the changed
# outputs, and writes the file; updating some aliases keeps the others' pins.
def test_update_diff(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _publish_both(site, tmp_path)
    lock_path = site.proj / "flow.lock"
    first = site.invoke(["lock", "update"])
    assert first.exit_code == ExitCode.OK, first.output
    assert "rtl: added rtl-team/cpu 1 (channel stable)" in first.stdout
    assert "vip: added vip/axi 2.0" in first.stdout
    written = lock_path.read_text()
    assert parse_lock(written).imports["rtl"].version == "1"

    same = site.invoke(["lock", "update"])
    assert same.exit_code == ExitCode.OK, same.output
    assert "up to date" in same.stdout
    assert lock_path.read_text() == written

    changed = {**SRCS, "rtl/a.sv": "module a; wire w; endmodule\n"}
    _publish_both(site, tmp_path, version="2", srcs=changed)  # the channel moves to 2
    assert lock_path.read_text() == written  # moving a channel never touches the lock
    only_vip = site.invoke(["lock", "update", "vip"])
    assert only_vip.exit_code == ExitCode.OK, only_vip.output
    assert lock_path.read_text() == written  # rtl kept at 1
    second = site.invoke(["lock", "update", "rtl", "--json"])
    assert second.exit_code == ExitCode.OK, second.output
    doc = json.loads(second.stdout)
    assert doc["changes"] == [{
        "alias": "rtl", "change": "changed", "name": "rtl-team/cpu",
        "old_version": "1", "new_version": "2", "outputs_changed": ["pack.srcs"],
        "outputs_added": [], "outputs_removed": [],
    }]  # fmt: skip
    assert doc["written"] is True
    again = site.invoke(["lock", "update", "rtl"])
    assert "up to date" in again.stdout
    lock = parse_lock(lock_path.read_text())
    assert lock.imports["rtl"].version == "2"
    assert lock.imports["vip"].version == "2.0"

    unknown = site.invoke(["lock", "update", "rlt"])
    assert unknown.exit_code == ExitCode.USAGE
    assert "no import 'rlt'; its imports: rtl, vip" in unknown.output


def test_diff_texts(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _publish_both(site, tmp_path)
    store = site.services.memory_store
    flow = _flow({"rtl": {"from": "rtl-team/cpu", "channel": "stable"}})
    old = resolve_lock(store, flow, None)
    publish(store, site.services.cas("test"), tmp_path / "r2",
            {"pack.srcs": SRCS, "pack.extra": b"x"}, version="2", channel="stable")  # fmt: skip
    new = resolve_lock(store, flow, old)
    [change] = diff_locks(old, new)
    assert change.describe() == (
        "rtl: rtl-team/cpu 1 → 2 (channel stable); added pack.extra; removed gen.report"
    )
    [gone] = diff_locks(old, LockFile())
    assert gone.describe() == "rtl: removed (was rtl-team/cpu 1)"
    assert diff_locks(old, old) == []


# R4: lock update refuses what it cannot pin, with the fix in the message.
@pytest.mark.parametrize(
    ("imports", "match"),
    [
        pytest.param({"rtl": {"from": "rtl-team/cpu", "channel": "nightly"}},
                     r"no channel rtl-team/cpu:nightly in domain 'test'", id="no-channel"),
        pytest.param({"rtl": {"from": "rtl-team/cpu", "version": "9"}},
                     r"no release rtl-team/cpu 9 in domain 'test'.*ebs release export",
                     id="no-version"),
        pytest.param({"rtl": {"from": "rtl-team/cpu", "version": "0"}}, r"yanked: bad",
                     id="yanked"),
    ],
)  # fmt: skip
def test_update_errors(tmp_path: Path, imports: dict[str, dict[str, str]], match: str) -> None:
    store = InMemoryMetadataStore()
    cas = FsCAS(tmp_path / "cas", "test")
    publish(store, cas, tmp_path / "r1", {"pack.srcs": SRCS}, channel="stable")
    old = publish(store, cas, tmp_path / "r0", {"pack.srcs": SRCS}, version="0")
    store.yank_release(old.id, by="alice", reason="bad")
    with pytest.raises(ReleaseError, match=match):
        resolve_lock(store, _flow(imports), None)


# R4: `lock check` exits non-zero (and writes nothing) whenever `lock update` would change the
# file: no lock, a moved channel, a new import, a hand-edited layout.
def test_check_exit_code(tmp_path: Path) -> None:
    site = _site(tmp_path)
    _publish_both(site, tmp_path)
    lock_path = site.proj / "flow.lock"
    missing = site.invoke(["lock", "check"])
    assert missing.exit_code == ExitCode.ACTIONS_FAILED, missing.output
    assert "ebs lock update" in missing.output
    assert not lock_path.exists()

    assert site.invoke(["lock", "update"]).exit_code == ExitCode.OK
    ok = site.invoke(["lock", "check"])
    assert ok.exit_code == ExitCode.OK, ok.output
    written = lock_path.read_text()

    _publish_both(site, tmp_path, version="2", srcs={**SRCS, "rtl/b.sv": "module b2; endmodule\n"})
    moved = site.invoke(["lock", "check"])
    assert moved.exit_code == ExitCode.ACTIONS_FAILED, moved.output
    assert "rtl: rtl-team/cpu 1 → 2 (channel stable); changed pack.srcs" in moved.output
    assert lock_path.read_text() == written

    assert site.invoke(["lock", "update"]).exit_code == ExitCode.OK
    lock_path.write_text(lock_path.read_text().replace("  ", "    "))  # same content
    relaid = site.invoke(["lock", "check"])
    assert relaid.exit_code == ExitCode.ACTIONS_FAILED, relaid.output
    assert "layout" in relaid.output

    assert site.invoke(["lock", "update"]).exit_code == ExitCode.OK
    flow = (site.proj / "flow.yaml").read_text()
    (site.proj / "flow.yaml").write_text(
        flow.replace("imports:\n", 'imports:\n  rtl2: { from: rtl-team/cpu, version: "1" }\n')
    )
    added = site.invoke(["lock", "check"])
    assert added.exit_code == ExitCode.ACTIONS_FAILED
    assert "rtl2: added rtl-team/cpu 1" in added.output


def test_release_digest_ignores_domain_and_metadata(tmp_path: Path) -> None:
    store = InMemoryMetadataStore()
    a = publish(store, FsCAS(tmp_path / "cas", "test"), tmp_path / "a", {"pack.srcs": SRCS})
    b = publish(store, FsCAS(tmp_path / "cas", "test"), tmp_path / "b", {"pack.srcs": SRCS},
                version="2")  # fmt: skip
    exported = a.model_copy(update={"domain": "partner", "id": 99, "created_by": "bob"})
    assert release_digest(exported) == release_digest(a)  # an exported copy pins the same
    assert release_digest(b) != release_digest(a)  # the version is part of it
    assert isinstance(release_digest(a), Digest)


def _nd_lock(tmp_path: Path) -> tuple[InMemoryMetadataStore, FsCAS, Flow, LockFile]:
    store, cas = InMemoryMetadataStore(), FsCAS(tmp_path / "cas", "test")
    publish(store, cas, tmp_path / "r1", {"pack.srcs": SRCS, "gen.report": b"ok\n"},
            channel="stable", nondeterministic=frozenset({"gen.report"}))  # fmt: skip
    flow = _flow({"rtl": {"from": "rtl-team/cpu", "channel": "stable"}})
    return store, cas, flow, resolve_lock(store, flow, None)


def _edit_output(lock: LockFile, output: str, **changes: object) -> LockFile:
    entry = lock.imports["rtl"]
    edited = entry.outputs[output].model_copy(update=changes)
    return LockFile(imports={"rtl": entry.model_copy(
        update={"outputs": {**entry.outputs, output: edited}})})  # fmt: skip


# R1/R5: any hand edit of one output field (the id a nondeterministic output passes into
# downstream keys included) is refused, and every field is part of the release digest.
@pytest.mark.parametrize(
    "change",
    [{"id": hash_bytes(b"other")}, {"size": 999}, {"type": "tree"}, {"digest": hash_bytes(b"x")}],
    ids=["id", "size", "type", "digest"],
)
def test_hand_edited_output_field_refused(tmp_path: Path, change: dict[str, object]) -> None:
    _, _, _, lock = _nd_lock(tmp_path)
    with pytest.raises(ReleaseError, match="edited by hand"):
        parse_lock(dump_lock(_edit_output(lock, "gen.report", **change)))


def test_release_digest_covers_every_field(tmp_path: Path) -> None:
    store = InMemoryMetadataStore()
    a = publish(store, FsCAS(tmp_path / "cas", "test"), tmp_path / "a", {"gen.report": b"ok\n"})
    base = release_digest(a)
    assert release_digest(a.model_copy(update={"name": "other/cpu"})) != base
    out = a.outputs["gen.report"]
    for change in ({"id": hash_bytes(b"o")}, {"digest": hash_bytes(b"o")}, {"size": 7},
                   {"type": "tree"}):  # fmt: skip
        changed = a.model_copy(update={"outputs": {"gen.report": out.model_copy(update=change)}})
        assert release_digest(changed) != base, change


# R1, R4: a change of an output's id alone changes the lock digest and shows in the diff.
def test_id_only_change_is_visible(tmp_path: Path) -> None:
    _, _, _, lock = _nd_lock(tmp_path)
    entry = lock.imports["rtl"]
    edited = _edit_output(lock, "gen.report", id=hash_bytes(b"other id")).imports["rtl"]
    edited = edited.model_copy(
        update={"release": _release_digest(edited.name, edited.version, edited.outputs)}
    )
    other = LockFile(imports={"rtl": edited})
    assert lock_digest(other) != lock_digest(lock)
    [change] = diff_locks(lock, other)
    assert change.outputs_changed == ("gen.report",)
    assert entry.outputs["pack.srcs"] == edited.outputs["pack.srcs"]


# R5: a store that disagrees with the lock (same name and version, other outputs) is refused
# before a build records provenance to the wrong release.
def test_import_rows_store_disagrees(tmp_path: Path) -> None:
    store, _, flow, lock = _nd_lock(tmp_path)
    assert [r.alias for r in import_rows(store, flow, lock)] == ["rtl"]
    elsewhere = InMemoryMetadataStore()
    publish(elsewhere, FsCAS(tmp_path / "cas2", "test"), tmp_path / "x",
            {"pack.srcs": {"z.sv": "z\n"}}, channel="stable")  # fmt: skip
    foreign = resolve_lock(elsewhere, flow, None)
    with pytest.raises(ReleaseError, match="has other outputs than the lock pins"):
        import_rows(store, flow, foreign)
    with pytest.raises(ReleaseError, match="does not exist"):
        import_rows(InMemoryMetadataStore(), flow, lock)


# R4: a channel still pointing at a release yanked after the move is not pinned.
def test_channel_to_yanked_release_refused(tmp_path: Path) -> None:
    store, _, flow, _ = _nd_lock(tmp_path)
    release = store.get_release("test", "rtl-team/cpu", "1")
    assert release is not None
    store.yank_release(release.id, by="alice", reason="bad seed")
    with pytest.raises(ReleaseError, match="yanked: bad seed"):
        resolve_lock(store, flow, None)


# R4: updating some aliases resolves the others that have no pin yet, and keeps the rest.
def test_update_some_aliases(tmp_path: Path) -> None:
    store, cas, _, _ = _nd_lock(tmp_path)
    publish(store, cas, tmp_path / "v", {"pack.vip": {"axi.sv": "x\n"}}, name="vip/axi",
            version="2.0")  # fmt: skip
    flow = _flow({"rtl": {"from": "rtl-team/cpu", "channel": "stable"},
                  "vip": {"from": "vip/axi", "version": "2.0"}})  # fmt: skip
    only_vip = resolve_lock(store, _flow({"vip": {"from": "vip/axi", "version": "2.0"}}), None)
    both = resolve_lock(store, flow, only_vip, ["vip"])
    assert sorted(both.imports) == ["rtl", "vip"]  # rtl had no pin: resolved anyway
    publish(store, cas, tmp_path / "r2", {"pack.srcs": SRCS}, version="2", channel="stable")
    kept = resolve_lock(store, flow, both, ["vip"])
    assert kept.imports["rtl"].version == "1"  # not asked for: keeps its pin
    assert resolve_lock(store, flow, both, ["rtl"]).imports["rtl"].version == "2"
