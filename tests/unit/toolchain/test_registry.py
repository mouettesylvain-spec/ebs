"""Toolchain registry: immutable registrations, drift, resolution and checks (P1-07)."""

from __future__ import annotations

import os
import zlib
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import ToolchainError
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planner import Planner
from ebs.toolchain.fingerprint import FingerprintEntry, fingerprint_roots
from ebs.toolchain.registry import (
    DRIFT_LIST_LIMIT,
    FileChange,
    Registration,
    RegistryToolchainResolver,
    check,
    decode_manifest,
    diff_manifests,
    encode_manifest,
    format_drift,
    register,
)
from tests.helpers.plan import RTL, FakeSnapshotter, flow, registry

MODULE = "questa/2025.2"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock()
        self.store = InMemoryMetadataStore(clock=self.clock)
        self.tree = tmp_path / "tools" / "questa" / "2025.2"
        (self.tree / "bin").mkdir(parents=True)
        self.write("bin/vsim", "v1", mode=0o755)
        self.write("lib/libsim.so", "lib v1")
        self.write("docs/readme.txt", "doc")

    def write(self, rel: str, text: str, *, mode: int = 0o644) -> None:
        path = self.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": f"{self.tree}/bin:/usr/bin:/bin", "QUESTA_HOME": str(self.tree), **extra}

    def register(
        self, env: Mapping[str, str] | None = None, *, module: str = MODULE, user: str = "alice"
    ) -> Registration:
        return register(
            self.store,
            module=module,
            version=module.rpartition("/")[2],
            env=self.env() if env is None else env,
            install_roots=(self.tree,),
            user=user,
        )


@pytest.fixture
def e(tmp_path: Path) -> Env:
    return Env(tmp_path)


# R2
def test_idempotent(e: Env) -> None:
    first = e.register()
    row = e.store.get_toolchain(first.toolchain.id)
    assert row is not None
    assert (first.status, first.superseded, first.changes) == ("registered", (), ())
    assert format_drift(first) is None

    e.clock.advance(3600)
    again = e.register(user="bob")
    assert again.status == "unchanged"
    assert again.toolchain == first.toolchain
    assert again.superseded == ()
    assert format_drift(again) is None
    assert e.store.list_toolchains() == [row]  # one row, untouched: registered_at, registered_by


# R2
def test_drift_new_id_and_warning(e: Env) -> None:
    old = e.register()
    old_row = e.store.get_toolchain(old.toolchain.id)
    e.write("bin/vsim", "v2 patched", mode=0o755)  # silently patched install
    e.write("bin/vlog", "new tool", mode=0o755)
    (e.tree / "docs" / "readme.txt").unlink()

    new = e.register()
    assert new.status == "registered"
    assert new.toolchain.id != old.toolchain.id
    assert new.toolchain.fingerprint == fingerprint_roots([e.tree])
    assert new.previous == old.toolchain.id
    assert new.superseded == (old.toolchain.id,)
    root = str(e.tree)
    assert new.changes == (
        FileChange(f"{root}/bin/vlog", "added"),
        FileChange(f"{root}/bin/vsim", "changed"),
        FileChange(f"{root}/docs/readme.txt", "removed"),
    )
    assert new.changes_total == 3

    # The old snapshot is immutable: only superseded_by changed.
    assert old_row is not None
    assert e.store.get_toolchain(old.toolchain.id) == old_row.model_copy(
        update={"superseded_by": new.toolchain.id}
    )
    assert e.store.current_toolchain(MODULE) == e.store.get_toolchain(new.toolchain.id)

    warning = format_drift(new)
    assert warning is not None
    assert str(old.toolchain.id) in warning
    assert str(new.toolchain.id) in warning
    for change in new.changes:
        assert f"{change.change}: {change.path}" in warning


# R2
def test_drift_warning_lists_first_20(e: Env) -> None:
    e.register()
    for i in range(DRIFT_LIST_LIMIT + 5):
        e.write(f"lib/new{i:02}.so", "x")
    new = e.register()
    assert new.changes_total == DRIFT_LIST_LIMIT + 5
    assert len(new.changes) == DRIFT_LIST_LIMIT
    warning = format_drift(new)
    assert warning is not None
    listed = [line for line in warning.splitlines() if line.strip().startswith("added: ")]
    assert len(listed) == 20
    assert listed[0].strip() == f"added: {e.tree}/lib/new00.so"
    assert listed[-1].strip() == f"added: {e.tree}/lib/new19.so"
    assert "and 5 more" in warning


# R2
def test_env_only_drift_lists_variables(e: Env) -> None:
    old = e.register()
    new = e.register(e.env(QUESTA_MODE="batch", QUESTA_HOME="/elsewhere"))
    assert new.toolchain.id != old.toolchain.id
    assert new.changes == ()
    assert new.env_changes == ("QUESTA_HOME", "QUESTA_MODE")
    warning = format_drift(new)
    assert warning is not None
    assert "QUESTA_HOME, QUESTA_MODE" in warning


# R2
def test_reverted_install_reinstates_old_id(e: Env) -> None:
    a = e.register()
    e.write("bin/vsim", "v2", mode=0o755)
    b = e.register()
    e.write("bin/vsim", "v1", mode=0o755)
    os.utime(e.tree / "bin" / "vsim", ns=_mtime_of(e, a))
    back = e.register()
    assert back.status == "reinstated"
    assert back.toolchain.id == a.toolchain.id
    assert back.superseded == (b.toolchain.id,)
    current = e.store.current_toolchain(MODULE)
    assert current is not None
    assert current.id == a.toolchain.id
    old_b = e.store.get_toolchain(b.toolchain.id)
    assert old_b is not None
    assert old_b.superseded_by == a.toolchain.id


def _mtime_of(e: Env, reg: Registration) -> tuple[int, int]:
    # The first registration's mtime of bin/vsim, read back from the stored manifest.
    manifest = e.store.toolchain_manifest(reg.toolchain.id)
    assert manifest is not None
    entry = decode_manifest(manifest)[(str(e.tree), "bin/vsim")]
    return (entry.mtime_ns, entry.mtime_ns)


# R2
def test_modules_are_independent(e: Env) -> None:
    a = e.register(module="questa/2025.2")
    b = e.register(module="questa/2025.3")
    assert a.toolchain.id != b.toolchain.id
    assert b.superseded == ()
    assert {r.id for r in e.store.list_toolchains(current_only=True)} == {
        a.toolchain.id,
        b.toolchain.id,
    }


# R3
def test_resolve_latest(e: Env) -> None:
    resolver = RegistryToolchainResolver(e.store)
    with pytest.raises(ToolchainError, match=r"ebs toolchain register questa/2025\.2"):
        resolver.resolve("questa", MODULE)

    first = e.register()
    tc = RegistryToolchainResolver(e.store).resolve("sim", MODULE)
    assert tc.name == "sim"  # the flow-level name, not part of the id
    assert (tc.id, tc.module, tc.version) == (first.toolchain.id, MODULE, "2025.2")
    assert tc.install_roots == (e.tree,)
    assert dict(tc.env) == e.env()
    row = e.store.get_toolchain(tc.id)
    assert row is not None
    assert row.last_used_at == e.clock.now()  # R4 needs usage

    e.write("bin/vsim", "v2", mode=0o755)
    second = e.register()
    tc2 = RegistryToolchainResolver(e.store).resolve("sim", MODULE)
    assert tc2.id == second.toolchain.id != first.toolchain.id


# R3
def test_resolve_rejects_tampered_row(e: Env) -> None:
    reg = e.register()
    record = e.store._toolchains[str(reg.toolchain.id)]  # simulate a hand-edited database row
    e.store._toolchains[str(reg.toolchain.id)] = record.model_copy(
        update={"env": {"PATH": "/evil"}}
    )
    with pytest.raises(ToolchainError, match="does not match"):
        RegistryToolchainResolver(e.store).resolve("questa", MODULE)


# R3
def test_old_plan_keeps_id(e: Env, tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", "test")
    steps = {
        "lint": {"kind": "shell", "toolchain": "questa", "command": ["vsim"], "outputs": {"o": "o"}}
    }

    def plan_toolchain() -> tuple[str, str]:
        resolver = RegistryToolchainResolver(e.store)
        p = Planner(cas, FakeSnapshotter(cas, RTL), resolver, registry())
        plan = p.plan(flow(steps), base=tmp_path)
        (action,) = plan.actions
        assert action.toolchain is not None
        return str(action.toolchain.id), str(plan.toolchains["questa"].id)

    old = e.register()
    assert plan_toolchain() == (str(old.toolchain.id),) * 2
    old_tc = RegistryToolchainResolver(e.store).get(old.toolchain.id, name="questa")

    e.write("bin/vsim", "v2", mode=0o755)
    new = e.register()
    assert plan_toolchain() == (str(new.toolchain.id),) * 2  # new plans move on…
    # …while the id an old plan recorded still resolves to the exact old snapshot.
    assert RegistryToolchainResolver(e.store).get(old.toolchain.id, name="questa") == old_tc
    with pytest.raises(ToolchainError, match="not registered"):
        RegistryToolchainResolver(e.store).get(fingerprint_roots([e.tree]), name="questa")


# R4
def test_check_reports_drift_only(e: Env, tmp_path: Path) -> None:
    other = tmp_path / "tools" / "lint" / "1.0"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "lint").write_text("lint")
    stale_tree = tmp_path / "tools" / "stale" / "1.0"
    stale_tree.mkdir(parents=True)
    (stale_tree / "f").write_text("f")

    stale = register(
        e.store, module="stale/1.0", version="1.0", env={}, install_roots=(stale_tree,), user="a"
    )
    e.clock.advance(timedelta(days=40).total_seconds())
    drifting = e.register()
    steady = register(
        e.store, module="lint/1.0", version="1.0", env={}, install_roots=(other,), user="a"
    )
    RegistryToolchainResolver(e.store).resolve("questa", MODULE)  # used just now
    e.write("bin/vsim", "patched", mode=0o755)
    (stale_tree / "f").write_text("changed")  # drifted too, but unused for 40 days
    before = e.store.list_toolchains()

    results = check(e.store, days=30, now=e.clock.now())
    by_id = {r.record.id: r for r in results}
    assert set(by_id) == {drifting.toolchain.id, steady.toolchain.id}  # stale: not used lately
    assert stale.toolchain.id not in by_id
    assert by_id[steady.toolchain.id].status == "ok"
    drift = by_id[drifting.toolchain.id]
    assert drift.status == "drift"
    assert drift.fingerprint == fingerprint_roots([e.tree])
    assert drift.changes == (FileChange(f"{e.tree}/bin/vsim", "changed"),)
    # Never changes ids: the registry is exactly as before.
    assert e.store.list_toolchains() == before


# R4
def test_check_reports_missing_roots(e: Env) -> None:
    reg = e.register()
    for path in sorted(e.tree.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    e.tree.rmdir()
    (result,) = check(e.store, days=30, now=e.clock.now())
    assert result.record.id == reg.toolchain.id
    assert result.status == "error"
    assert result.error is not None
    assert "does not exist" in result.error


_names = st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), min_size=1, max_size=12)
_entries = st.builds(
    FingerprintEntry,
    root=st.sampled_from(["/opt/a", "/opt/b"]),
    path=_names,
    kind=st.sampled_from(["file", "dir", "symlink", "other"]),
    size=st.integers(0, 2**40),
    mtime_ns=st.integers(0, 2**62),
    executable=st.booleans(),
    content=st.one_of(st.none(), st.binary(max_size=4).map(hash_bytes)),
    target=st.one_of(st.none(), _names),
)
_manifests = st.lists(_entries, max_size=30).map(lambda es: {(e.root, e.path): e for e in es})


# R2: the stored manifest round-trips exactly, so drift lists are exact
@settings(max_examples=200, deadline=None)
@given(_manifests)
def test_manifest_round_trip(manifest: dict[tuple[str, str], FingerprintEntry]) -> None:
    entries = [manifest[k] for k in sorted(manifest)]
    assert decode_manifest(encode_manifest(entries)) == manifest


# R2
@settings(max_examples=200, deadline=None)
@given(_manifests, _manifests)
def test_diff_manifests_properties(
    old: dict[tuple[str, str], FingerprintEntry], new: dict[tuple[str, str], FingerprintEntry]
) -> None:
    changes, total = diff_manifests(old, new)
    assert total == len({k for k in old.keys() | new.keys() if old.get(k) != new.get(k)})
    assert len(changes) == min(total, DRIFT_LIST_LIMIT)
    assert [c.path for c in changes] == sorted(c.path for c in changes)
    assert diff_manifests(old, old) == ((), 0)
    flipped = {"added": "removed", "removed": "added", "changed": "changed"}
    back, back_total = diff_manifests(new, old)
    assert back_total == total
    if total <= DRIFT_LIST_LIMIT:
        assert {(c.path, flipped[c.change]) for c in back} == {(c.path, c.change) for c in changes}


def test_corrupt_manifest() -> None:
    with pytest.raises(ToolchainError, match="corrupt"):
        decode_manifest(b"not zlib")
    with pytest.raises(ToolchainError, match="corrupt"):
        decode_manifest(zlib.compress(b'["/a","p"]\n'))


# R4: one unusable manifest does not stop the nightly check of the others
def test_check_survives_corrupt_manifest(e: Env) -> None:
    reg = e.register()
    e.store._toolchain_manifests[str(reg.toolchain.id)] = b"garbage"
    e.write("bin/vsim", "patched", mode=0o755)
    (result,) = check(e.store, days=30, now=e.clock.now())
    assert result.status == "drift"
    assert result.manifest_missing
    assert result.changes == ()


# R2: a corrupt predecessor manifest still registers, with an unknown file list
def test_register_survives_corrupt_manifest(e: Env) -> None:
    old = e.register()
    e.store._toolchain_manifests[str(old.toolchain.id)] = b"garbage"
    e.write("bin/vsim", "patched", mode=0o755)
    new = e.register()
    assert new.status == "registered"
    assert new.manifest_missing
    warning = format_drift(new)
    assert warning is not None
    assert "changed files unknown" in warning


# R4: "used" means last_used_at, even for an old registration
def test_check_includes_recently_used_old_registration(e: Env) -> None:
    reg = e.register()
    e.clock.advance(timedelta(days=40).total_seconds())
    RegistryToolchainResolver(e.store).resolve("questa", MODULE)
    e.clock.advance(timedelta(days=10).total_seconds())
    e.write("bin/vsim", "patched", mode=0o755)
    (result,) = check(e.store, days=30, now=e.clock.now())
    assert (result.record.id, result.status) == (reg.toolchain.id, "drift")
    e.clock.advance(timedelta(days=20).total_seconds())  # last use exactly 30 days ago
    assert len(check(e.store, days=30, now=e.clock.now())) == 1
    e.clock.advance(1)
    assert check(e.store, days=30, now=e.clock.now()) == []


# R4: superseded snapshots are not reported as drift
def test_check_ignores_superseded(e: Env) -> None:
    e.register()
    e.write("bin/vsim", "v2", mode=0o755)
    b = e.register()
    (result,) = check(e.store, days=30, now=e.clock.now())
    assert (result.record.id, result.status) == (b.toolchain.id, "ok")


# R3: one resolver (one plan) keeps one id per module
def test_resolver_fixed_per_instance(e: Env) -> None:
    first = e.register()
    resolver = RegistryToolchainResolver(e.store)
    assert resolver.resolve("questa", MODULE).id == first.toolchain.id
    e.write("bin/vsim", "v2", mode=0o755)
    e.register()
    assert resolver.resolve("questa", MODULE).id == first.toolchain.id


# R3
def test_get_rejects_tampered_old_row(e: Env) -> None:
    old = e.register()
    e.write("bin/vsim", "v2", mode=0o755)
    e.register()
    key = str(old.toolchain.id)
    e.store._toolchains[key] = e.store._toolchains[key].model_copy(update={"env": {"A": "b"}})
    with pytest.raises(ToolchainError, match="does not match"):
        RegistryToolchainResolver(e.store).get(old.toolchain.id, name="questa")


# R2: removed variables count as env drift
def test_env_drift_reports_removed_variables(e: Env) -> None:
    e.register(e.env(QUESTA_MODE="batch"))
    new = e.register()
    assert new.env_changes == ("QUESTA_MODE",)


# R2: exactly 20 changes are all listed, with no "more" line
def test_drift_warning_exactly_20(e: Env) -> None:
    e.register()
    for i in range(DRIFT_LIST_LIMIT):
        e.write(f"lib/new{i:02}.so", "x")
    warning = format_drift(e.register())
    assert warning is not None
    assert sum(line.strip().startswith("added: ") for line in warning.splitlines()) == 20
    assert "more" not in warning
