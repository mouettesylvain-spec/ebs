"""A small GC world: a tmp fs CAS and an in-memory store sharing one FakeClock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import (
    ActionRow,
    BlobRef,
    BuildCreate,
    BuildId,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.meta.memory import InMemoryMetadataStore

DOMAIN = "test"
DAY = 86_400.0


@dataclass
class World:
    tmp: Path
    clock: FakeClock
    store: InMemoryMetadataStore
    cas: FsCAS

    def advance(self, *, days: float = 0, hours: float = 0) -> None:
        self.clock.advance(days * DAY + hours * 3600)

    def blob(self, data: bytes, *, register: bool = True) -> Digest:
        d = self.cas.put_bytes(data)
        if register:
            self.store.register_blobs(DOMAIN, [BlobRef(digest=d, size=len(data), kind="file")])
        return d

    def tree(self, files: dict[str, bytes], *, register: str = "root") -> Digest:
        """A tree stored with `put_tree`; `register`: "root", "all" (children too) or "none"."""
        src = self.tmp / f"src-{uuid4().hex}"
        for rel, data in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        d = self.cas.put_tree(src)
        if register == "root":
            self.store.register_blobs(DOMAIN, [BlobRef(digest=d, size=0, kind="tree")])
        elif register == "all":
            self.store.register_blobs(DOMAIN, tree_refs(self.cas, d))
        return d

    def entry(self, label: str, outputs: dict[str, Digest], *, log: Digest | None = None) -> Digest:
        """A passed action-cache entry with these outputs; returns its key."""
        key = hash_bytes(f"key:{label}".encode())
        self.store.cache_put(DOMAIN, key, manifest(key, outputs, log=log, cas=self.cas))
        return key

    def build(self, *, project: str = "demo", plan: Digest | None = None) -> BuildId:
        build = self.store.create_build(
            BuildCreate(
                uuid=uuid4(),
                domain=DOMAIN,
                project=project,
                plan_digest=plan or hash_bytes(b"plan"),
                flow_repo="",
                flow_commit="",
                flow_dirty=False,
                user_name="alice",
                cache_mode="write",
            )
        )
        return build

    def record(
        self,
        build: BuildId,
        action_id: str,
        outputs: dict[str, Digest],
        *,
        log: Digest | None = None,
    ) -> Digest:
        """Add an action to `build` and record a result with these outputs; returns its key."""
        key = hash_bytes(f"key:{build}:{action_id}".encode())
        self.store.add_actions(
            build, [ActionRow(action_id=action_id, step=action_id.split("[")[0])]
        )
        self.store.record_result(build, action_id, manifest(key, outputs, log=log, cas=self.cas))
        return key


def tree_refs(cas: FsCAS, root: Digest) -> list[BlobRef]:
    """The tree and every manifest and blob below it."""
    refs = [BlobRef(digest=root, size=0, kind="tree")]
    for entry in cas.get_tree(root).entries:
        if entry.digest is None:
            continue
        if entry.type == "dir":
            refs += tree_refs(cas, entry.digest)
        else:
            refs.append(BlobRef(digest=entry.digest, size=entry.size, kind="file"))
    return refs


def manifest(
    key: Digest, outputs: dict[str, Digest], *, cas: FsCAS, log: Digest | None = None
) -> ResultManifest:
    results = {}
    for name, d in outputs.items():
        kind = "tree" if cas.tree_path(d).exists() else "file"
        results[name] = OutputResult(digest=d, id=d, type=kind, size=0)  # type: ignore[arg-type]
    return ResultManifest(
        action_key=key,
        status="passed",
        exit_code=0,
        inputs={},
        outputs=results,
        log=log,
        summary={},
        resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
        runner=RunnerInfo(version="0", host="node-a"),
    )


def present(cas: FsCAS) -> set[Digest]:
    return {d for d, _ in cas.iter_digests()}


@pytest.fixture
def world(tmp_path: Path) -> World:
    clock = FakeClock()
    store = InMemoryMetadataStore(clock=clock)
    cas = FsCAS(tmp_path / "cas", DOMAIN, clock=clock)
    return World(tmp_path, clock, store, cas)


TTL = timedelta(days=30)
