"""SLSA v1 provenance for releases: an in-toto Statement per release (P3-03 R1).

`provenance_statement(release, build, plan, imports)` describes how a release's outputs were
made, from what the build recorded:

- `subject`: each released output, `<action id>.<output>` with its content digest (the bytes
  an auditor holds: a file, or a tree's manifest); its passed-down id, type and size are
  annotations.
- `predicate.buildDefinition.externalParameters`: the flow (path, git repository, commit,
  dirty), the release (name, version, domain), the project, `params` (the digest of every
  action's params, `params_digest`) and `lock` (the `flow.lock` digest, absent without one).
- `internalParameters`: the plan digest (the build's final plan when it has one) and key schema.
- `resolvedDependencies`: the imported releases (`imports.<alias>`, release digest), the
  toolchains (`toolchains.<name>`, toolchain id) and the flow commit (`gitCommit`).
- `runDetails`: builder `ebs@<version that planned the build>`, the build UUID as
  `invocationId`, `startedOn`/`finishedOn` from the build record.

The statement is plain JSON data; `ebs.release.sign` wraps it in a signed DSSE envelope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ebs.core.canon import digest_json
from ebs.core.digest import Digest
from ebs.core.errors import ReleaseError
from ebs.core.types import JsonValue
from ebs.meta.api import BuildId, BuildView, MetadataBackend, ReleaseView
from ebs.plan.types import ParamValue, Plan
from ebs.release.lock import release_digest

__all__ = [
    "BUILD_TYPE",
    "PREDICATE_TYPE",
    "STATEMENT_TYPE",
    "ImportDep",
    "import_deps",
    "params_digest",
    "provenance_statement",
]

STATEMENT_TYPE: Final = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE: Final = "https://slsa.dev/provenance/v1"
BUILD_TYPE: Final = "urn:ebs:build-type:flow:v1"


@dataclass(frozen=True, slots=True)
class ImportDep:
    """A release the build imported through `alias`, by its lock identity (`release_digest`)."""

    alias: str
    name: str
    version: str
    release: Digest


def import_deps(store: MetadataBackend, build: BuildId) -> list[ImportDep]:
    """The releases `build` recorded as imports (P2-03), by alias."""
    deps: list[ImportDep] = []
    for row in store.list_release_imports(build):
        view = store.get_release_by_id(row.release)
        if view is None:
            raise ReleaseError(
                f"build {build} imports release id {row.release} through {row.alias!r}, but "
                "that release cannot be read; check your membership of its domain"
            )
        deps.append(ImportDep(row.alias, view.name, view.version, release_digest(view)))
    return deps


def params_digest(plan: Plan) -> Digest:
    """Digest of `{action id: params}` over every action of `plan` (order does not matter)."""
    return digest_json({a.action_id: _params(a.params) for a in plan.actions})


def _params(params: Mapping[str, ParamValue]) -> JsonValue:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in params.items()}


def _digest_set(d: Digest) -> dict[str, JsonValue]:
    return {d.algo: d.hex}


def _time(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def provenance_statement(
    release: ReleaseView, build: BuildView, plan: Plan, imports: Sequence[ImportDep]
) -> dict[str, JsonValue]:
    """The in-toto Statement (SLSA provenance v1 predicate) of `release`, made by `build`."""
    if release.build != build.id:
        raise ReleaseError(
            f"release {release.name} {release.version} was not made from build {build.id} "
            f"(its build is {release.build}); attest it with its own build"
        )
    subjects: list[JsonValue] = []
    for name, o in sorted(release.outputs.items()):
        subjects.append(
            {
                "name": name,
                "digest": _digest_set(o.digest),
                "annotations": {
                    "id": str(o.id),
                    "type": o.type,
                    "size": o.size,
                    "action": o.action_id,
                    "output": o.output,
                },
            }
        )
    flow: dict[str, JsonValue] = {"path": plan.flow.path}
    git = plan.flow.git
    if git is not None:
        flow.update(repository=git.repo, commit=git.commit, dirty=git.dirty)
    external: dict[str, JsonValue] = {
        "flow": flow,
        "project": plan.project,
        "release": {"name": release.name, "version": release.version, "domain": release.domain},
        "params": str(params_digest(plan)),
    }
    if plan.lock is not None:
        external["lock"] = str(plan.lock)
    deps: list[JsonValue] = []
    for dep in sorted(imports, key=lambda d: d.alias):
        deps.append(
            {
                "name": f"imports.{dep.alias}",
                "uri": f"ebs:release/{dep.name}@{dep.version}",
                "digest": _digest_set(dep.release),
            }
        )
    for tc_name, tc in sorted(plan.toolchains.items()):
        deps.append(
            {
                "name": f"toolchains.{tc_name}",
                "uri": f"ebs:toolchain/{tc.module}",
                "digest": _digest_set(tc.id),
            }
        )
    if git is not None and git.commit:
        deps.append(
            {"name": "flow", "uri": git.repo or plan.flow.path, "digest": {"gitCommit": git.commit}}
        )
    metadata: dict[str, JsonValue] = {
        "invocationId": str(build.uuid),
        "startedOn": _time(build.created_at),
    }
    if build.finished_at is not None:
        metadata["finishedOn"] = _time(build.finished_at)
    plan_ref = build.final_plan_digest or build.plan_digest
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": external,
                "internalParameters": {"plan": str(plan_ref), "keySchema": plan.key_schema},
                "resolvedDependencies": deps,
            },
            "runDetails": {
                "builder": {"id": f"ebs@{plan.ebs_version}"},
                "metadata": metadata,
            },
        },
    }
