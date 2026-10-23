"""`ebs verify`: would an exact rerun of a release's build reproduce its action keys? (P3-04 R3)

Nothing is executed and nothing of the live system is read: the flow is checked out from the
bundle's git bundle at the recorded commit, toolchains come from the bundled records
(`BundleToolchains`), import globs are matched against the bundled tree manifests, and sources are
hashed afresh (`rehash`, no stat cache, no git ids) into a scratch CAS. The new plan is refined
with the output ids the build recorded for its producers (as the driver refined it while it ran),
so every action the build keyed gets a key again, and `diff_plans` compares it with the recorded
plan: an action whose key differs is reported with the key-document fields that differ.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import DigestError, ReleaseError, ToolchainError
from ebs.core.tree import TreeManifest
from ebs.core.types import JsonValue
from ebs.flow.loader import load_flow
from ebs.plan.diff import diff_plans
from ebs.plan.planfile import plan_digest
from ebs.plan.planner import Planner, refine_plan
from ebs.plan.types import ActionDiff, ActionOutputInput, Plan
from ebs.release.archive import FLOW_BUNDLE, Bundle, Git
from ebs.release.lock import LOCK_FILE, load_lock, plan_lock
from ebs.rules.registry import RuleRegistry
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache
from ebs.toolchain.model import Toolchain, ToolchainResolver

__all__ = ["BundleToolchains", "VerifyReport", "plan_bundle", "replan", "verify_bundle"]

_MEMORY: Final = Path(":memory:")


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """The outcome of `verify_bundle`. `mismatches` holds every action whose key differs, that
    only one plan has, or that the build keyed but the re-plan could not; `unkeyed` the actions
    the build itself never keyed (nothing to compare)."""

    release: str  # "name version"
    plan: Digest  # of the recorded plan compared against
    actions: int  # in the recorded plan
    matched: int
    mismatches: tuple[ActionDiff, ...]
    unkeyed: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "v": 1,
            "release": self.release,
            "plan": str(self.plan),
            "ok": self.ok,
            "actions": self.actions,
            "matched": self.matched,
            "mismatches": [
                {"action_id": d.action_id, "status": d.status, "fields": list(d.changed)}
                for d in self.mismatches
            ],
            "unkeyed": list(self.unkeyed),
        }


def _text(doc: Mapping[str, JsonValue], key: str, what: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str):
        raise ToolchainError(f"{what}: {key!r} must be a string, got {value!r}")
    return value


class BundleToolchains:
    """A `ToolchainResolver` over the toolchain records of a bundle (by flow-level name).

    The id is recomputed from the recorded parts, so a record that does not describe the
    toolchain the build used shows up as a `toolchain` key difference."""

    def __init__(self, records: Mapping[str, Mapping[str, JsonValue]]) -> None:
        self._records = dict(records)

    def resolve(self, name: str, module: str) -> Toolchain:
        doc = self._records.get(name)
        what = f"toolchain record {name!r} of the bundle"
        if doc is None or doc.get("module") != module:
            raise ToolchainError(
                f"the bundle has no toolchain record for {name!r} ({module}); the flow at the "
                "recorded commit uses a toolchain the build did not"
            )
        env, roots = doc.get("env"), doc.get("install_roots")
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ToolchainError(f"{what}: 'env' must map names to strings")
        if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
            raise ToolchainError(f"{what}: 'install_roots' must be a list of paths")
        try:
            fingerprint = Digest.parse(_text(doc, "fingerprint", what))
        except DigestError as exc:
            raise ToolchainError(f"{what}: {exc}") from None
        return Toolchain.from_parts(
            name=name,
            module=module,
            version=_text(doc, "version", what),
            install_roots=[Path(str(r)) for r in roots],
            env={k: str(v) for k, v in env.items()},
            fingerprint=fingerprint,
        )


class _BundleCAS(FsCAS):
    """A scratch CAS whose tree reads fall back to the bundle's tree manifests (the imported
    outputs' trees, whose files the bundle does not hold)."""

    def __init__(
        self, root: Path, domain: str, trees: Mapping[Digest, TreeManifest], clock: Clock
    ) -> None:
        super().__init__(root, domain, clock=clock)
        self._bundled = dict(trees)

    def get_tree(self, d: Digest) -> TreeManifest:
        found = self._bundled.get(d)
        return found if found is not None else super().get_tree(d)


def plan_bundle(
    bundle: Bundle,
    *,
    cas: CAS,
    toolchains: ToolchainResolver,
    rules: RuleRegistry,
    git: Git,
    work: Path,
    clock: Clock,
) -> tuple[Plan, Planner]:
    """Plan the bundled flow again at its recorded commit (checked out under `work`, created),
    its sources hashed afresh into `cas`, for the steps the build ran; and the planner (the
    driver's refiner when the plan is run, P3-05)."""
    recorded = bundle.plan()
    flow_git = recorded.flow.git
    if flow_git is None or not flow_git.commit:
        raise ReleaseError(f"the plan in {bundle.path} records no git commit to re-plan from")
    src = work / "src"
    git.checkout(bundle.member(FLOW_BUNDLE), flow_git.commit, src)
    flow_file = src / recorded.flow.path
    flow = load_flow(flow_file)
    lock = load_lock(flow_file.parent / LOCK_FILE) if flow.imports else None
    sources = SourceSnapshotter(
        cas, StatCache(_MEMORY, clock=clock), clock=clock, rehash=True, audit_fraction=0.0,
        git=None,
    )  # fmt: skip
    planner = Planner(cas, sources, toolchains, rules)
    plan = planner.plan(
        flow,
        base=flow_file.parent,
        info=recorded.flow,
        targets=tuple(dict.fromkeys(a.step for a in recorded.actions)),
        rehash=True,
        lock=None if lock is None else plan_lock(lock),
    )
    return plan, planner


def replan(bundle: Bundle, *, rules: RuleRegistry, git: Git, work: Path, clock: Clock) -> Plan:
    """Plan the bundled flow again at its recorded commit, refined with the recorded producer
    output ids. `work` (created) holds the checkout and the scratch CAS."""
    recorded = bundle.plan()
    cas = _BundleCAS(work / "cas", recorded.domain, bundle.trees(), clock)
    plan, _ = plan_bundle(
        bundle, cas=cas, toolchains=BundleToolchains(bundle.toolchains()), rules=rules, git=git,
        work=work, clock=clock,
    )  # fmt: skip
    outputs = {a.action_id: {o.name for o in a.outputs} for a in plan.actions}
    produced: dict[tuple[str, str], Digest] = {}
    for action in recorded.actions:
        for ref in action.inputs:
            source = ref.source
            if (
                isinstance(source, ActionOutputInput)
                and ref.id is not None
                and source.output in outputs.get(source.action_id, ())
            ):
                produced[(source.action_id, source.output)] = ref.id
    return refine_plan(plan, produced)


def verify_bundle(
    bundle: Bundle, *, rules: RuleRegistry, git: Git, work: Path, clock: Clock | None = None
) -> VerifyReport:
    """Re-plan `bundle` (in `work`, created) and compare every action key with the recorded one."""
    recorded = bundle.plan()
    new = replan(bundle, rules=rules, git=git, work=work, clock=clock or SystemClock())
    keyed = {a.action_id: a.key is not None for a in recorded.actions}
    matched = 0
    mismatches: list[ActionDiff] = []
    unkeyed: list[str] = []
    for diff in diff_plans(recorded, new):
        if diff.status == "unchanged":
            matched += 1
        elif diff.status == "unknown" and not keyed[diff.action_id]:
            unkeyed.append(diff.action_id)
        else:
            mismatches.append(diff)
    release = bundle.release()
    return VerifyReport(
        release=f"{release.name} {release.version}",
        plan=plan_digest(recorded),
        actions=len(recorded.actions),
        matched=matched,
        mismatches=tuple(mismatches),
        unkeyed=tuple(unkeyed),
    )
