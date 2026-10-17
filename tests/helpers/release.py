"""Builds with recorded results, for release, channel and export tests (P2-02).

Works on any store with builds and releases (in-memory, PostgreSQL, HTTP): `finish` runs each
action through queued -> running -> done/failed with a result, then finishes the build.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import (
    ActionRow,
    BuildCreate,
    BuildId,
    BuildStatus,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.release.releases import ReleaseBackend

DOMAIN = "test"


def out(data: bytes, *, deterministic: bool = True, kind: str = "file") -> OutputResult:
    d = hash_bytes(data)
    oid = d if deterministic else hash_bytes(b"nd:" + data)
    return OutputResult(digest=d, id=oid, type=kind, size=len(data))  # type: ignore[arg-type]


@dataclass
class Action:
    outputs: Mapping[str, OutputResult] = field(default_factory=dict)
    passed: bool = True
    inputs: Mapping[str, Digest] = field(default_factory=dict)


def action_key(build: BuildId, action_id: str) -> Digest:
    return hash_bytes(f"key:{build}:{action_id}".encode())


def finish(
    store: ReleaseBackend,
    actions: Mapping[str, Action],
    *,
    domain: str = DOMAIN,
    project: str = "demo",
    user: str = "alice",
    status: BuildStatus | None = "passed",
) -> BuildId:
    """A build of `project` with `actions` (id -> outputs); `status` None leaves it running."""
    build = store.create_build(
        BuildCreate(
            uuid=uuid4(),
            domain=domain,
            project=project,
            plan_digest=hash_bytes(b"plan"),
            flow_repo="",
            flow_commit="",
            flow_dirty=False,
            user_name=user,
            cache_mode="write",
        )
    )
    store.add_actions(
        build, [ActionRow(action_id=a, step=a.split("[", 1)[0]) for a in sorted(actions)]
    )
    for action_id, spec in sorted(actions.items()):
        key = action_key(build, action_id)
        store.set_action_state(build, action_id, "running", key=key)
        store.record_result(
            build,
            action_id,
            ResultManifest(
                action_key=key,
                status="passed" if spec.passed else "failed",
                exit_code=0 if spec.passed else 1,
                inputs=dict(spec.inputs),
                outputs=dict(spec.outputs),
                log=None,
                summary={},
                resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
                runner=RunnerInfo(version="0", host="node-a"),
            ),
        )
        store.set_action_state(build, action_id, "done" if spec.passed else "failed")
    if status is not None:
        store.finish_build(build, status)
    return build
