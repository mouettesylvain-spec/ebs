"""Revalidation: re-run a release's sign-off plan on newer tools and compare (P3-05).

`revalidate` re-plans the release from its sign-off bundle (`ebs.release.verify.plan_bundle`: the
flow at the recorded commit, sources hashed afresh, imports matched against the bundled trees)
with some toolchains overridden (R1): `NAME=latest` takes the newest registered toolchain of the
same tool (the module's part before `/`), `NAME=MODULE` that module's current toolchain; the
others keep their bundled records. The plan then runs as a normal build through the injected
`BuildRunner` (the CLI wires the driver and the site's executor), in revalidation mode: the
action cache is read (`read`) or ignored (`off`, `--no-cache`), never written, so a run on
unproven tools cannot feed other builds.

Each action the release's build recorded is then compared with its re-run (R2): by the
comparator configured for its step name, else for its rule kind (`[revalidate.comparators]`,
default `questa.sim = "sim"`), on the outputs of both runs materialized under the work dir
(recorded bytes from the bundle when it holds them, else from the domain's CAS); without one,
by status and the digests of its deterministic outputs (`digest`). An action whose outputs are
all nondeterministic has nothing a digest can tell: it is `incomparable` and not counted.

The report (R3) is a canonical JSON document and its Markdown rendering, both put in the
domain's CAS and attached to the release with `record_revalidation` (which makes them GC roots
of the release). The overall verdict is `failed` when the re-run did not complete (no plan, no
build, or an action without a result), `differences` when an action changed or a chosen
comparator could not compare it, else `reproduced`. `Notifier` sends the Markdown to a webhook
and/or by email (R4, `[revalidate.notify]`).
"""

from __future__ import annotations

import json
import smtplib
import urllib.request
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import CasError, ConfigError, EbsError, ReleaseError, ToolchainError
from ebs.core.log import get_logger
from ebs.core.tree import TreeManifest
from ebs.core.types import JsonValue
from ebs.meta.api import (
    BlobRef,
    BuildId,
    BuildStatus,
    CacheMode,
    ComparisonKind,
    MetadataBackend,
    OutputResult,
    ResultManifest,
    RevalidationCreate,
    RevalidationRecord,
    RevalidationVerdict,
    ToolchainRecord,
)
from ebs.plan.planfile import plan_digest
from ebs.plan.planner import Planner
from ebs.plan.types import ActionSpec, Plan
from ebs.release.archive import Bundle, Git, restore_objects
from ebs.release.verify import BundleToolchains, plan_bundle
from ebs.rules.comparators.api import Comparator, Verdict, get_comparator
from ebs.rules.registry import RuleRegistry
from ebs.toolchain.model import Toolchain
from ebs.toolchain.registry import to_toolchain

__all__ = [
    "DEFAULT_COMPARATORS",
    "BuildRunner",
    "Notifier",
    "NotifySettings",
    "OverrideToolchains",
    "RerunOutcome",
    "Revalidation",
    "RevalidationSettings",
    "parse_overrides",
    "render_markdown",
    "revalidate",
]

log = get_logger(__name__)

LATEST: Final = "latest"
DEFAULT_COMPARATORS: Final[Mapping[str, str]] = {"questa.sim": "sim"}
"""Step name or rule kind -> comparator name, before `[revalidate.comparators]`."""
REPORT_FORMAT: Final = "ebs.revalidation"
_SETTINGS_KEYS: Final = frozenset({"toolchain_overrides", "comparators", "comparator", "notify"})
_NOTIFY_KEYS: Final = frozenset(
    {"webhook", "email_to", "email_from", "smtp_host", "smtp_port", "when", "timeout_s"}
)


# --- overrides ---------------------------------------------------------------------------------


def parse_overrides(values: Iterable[str]) -> dict[str, str]:
    """`["questa=latest", "spyglass=vc_spyglass/2026.03"]` -> {name: "latest" | module}."""
    found: dict[str, str] = {}
    for value in values:
        name, sep, target = value.partition("=")
        name, target = name.strip(), target.strip()
        if not sep or not name or not target or "=" in target or "," in value:
            raise ReleaseError(
                f"invalid toolchain override {value!r}: give NAME=latest or NAME=MODULE "
                "(e.g. questa=latest), one per --toolchain-override"
            )
        if name in found:
            raise ReleaseError(f"toolchain {name!r} is overridden twice")
        found[name] = target
    return found


def _tool(module: str) -> str:
    """The module without its version, as the registry names it (`ToolchainRecord.name`)."""
    return module.rpartition("/")[0] or module


class OverrideToolchains:
    """A `ToolchainResolver`: the bundle's toolchain records, except the overridden ones, which
    are resolved in the registry when the resolver is made (so a bad override fails before
    anything runs). An override in `optional` (from the site config, which covers every release
    of a domain) is ignored for a plan without that toolchain; any other one is an error."""

    def __init__(
        self,
        records: Mapping[str, Mapping[str, JsonValue]],
        overrides: Mapping[str, str],
        store: MetadataBackend,
        *,
        optional: Collection[str] = (),
    ) -> None:
        self._bundled = BundleToolchains(records)
        self.overrides = {n: t for n, t in overrides.items() if n in records or n not in optional}
        self.chosen: dict[str, ToolchainRecord] = {}
        for name, target in self.overrides.items():
            doc = records.get(name)
            if doc is None:
                raise ReleaseError(
                    f"no toolchain {name!r} in the release's plan (it has: "
                    f"{', '.join(sorted(records)) or 'none'}); fix --toolchain-override or "
                    "[revalidate].toolchain_overrides"
                )
            self.chosen[name] = self._find(name, str(doc.get("module", "")), target, store)

    @staticmethod
    def _find(name: str, module: str, target: str, store: MetadataBackend) -> ToolchainRecord:
        if target == LATEST:
            tool = _tool(module)
            current = [r for r in store.list_toolchains(current_only=True) if r.name == tool]
            if not current:
                raise ToolchainError(
                    f"toolchain {name!r}: no registered toolchain of {tool!r} to take as "
                    f"latest; run `ebs toolchain register {tool}/<version>` first"
                )
            return max(current, key=lambda r: (r.registered_at, str(r.id)))
        found = store.current_toolchain(target)
        if found is None:
            raise ToolchainError(
                f"toolchain {name!r}: module {target!r} is not registered; run "
                f"`ebs toolchain register {target}` or use {name}=latest"
            )
        return found

    def resolve(self, name: str, module: str) -> Toolchain:
        record = self.chosen.get(name)
        if record is None:
            return self._bundled.resolve(name, module)
        return to_toolchain(record, name)


# --- settings ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class NotifySettings:
    """`[revalidate.notify]`: where a report summary is sent (R4). Empty: nowhere."""

    webhook: str = ""
    email_to: tuple[str, ...] = ()
    email_from: str = "ebs-revalidate@localhost"
    smtp_host: str = "localhost"
    smtp_port: int = 25
    when: Literal["always", "problems"] = "always"  # problems: differences or failed only
    timeout_s: float = 30.0

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> NotifySettings:
        unknown = sorted(set(section) - _NOTIFY_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [revalidate.notify]: {', '.join(unknown)}; expected "
                f"{', '.join(sorted(_NOTIFY_KEYS))}"
            )
        webhook = section.get("webhook", "")
        if not isinstance(webhook, str) or (webhook and not webhook.startswith("https://")):
            raise ConfigError(
                f"[revalidate.notify].webhook must be an https:// URL, got {webhook!r}"
            )
        to = section.get("email_to", [])
        if not isinstance(to, list) or not all(isinstance(a, str) and "@" in a for a in to):
            raise ConfigError("[revalidate.notify].email_to must be a list of email addresses")
        texts: dict[str, str] = {}
        for key in ("email_from", "smtp_host"):
            value = section.get(key, getattr(cls, key))
            if not isinstance(value, str) or not value:
                raise ConfigError(f"[revalidate.notify].{key} must be a non-empty string")
            texts[key] = value
        port = section.get("smtp_port", 25)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ConfigError(f"[revalidate.notify].smtp_port must be a port, got {port!r}")
        when = section.get("when", "always")
        if when not in ("always", "problems"):
            raise ConfigError(
                f"[revalidate.notify].when must be 'always' or 'problems', got {when!r}"
            )
        timeout = section.get("timeout_s", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ConfigError(f"[revalidate.notify].timeout_s must be positive, got {timeout!r}")
        return cls(
            webhook=webhook, email_to=tuple(to), email_from=texts["email_from"],
            smtp_host=texts["smtp_host"], smtp_port=port,
            when=when, timeout_s=float(timeout),
        )  # fmt: skip


@dataclass(frozen=True)
class RevalidationSettings:
    """The `[revalidate]` section. `comparators` maps a step name or a rule kind to the
    comparator used for its actions (a step name wins over a kind)."""

    toolchain_overrides: Mapping[str, str] = field(default_factory=dict)
    comparators: Mapping[str, Comparator] = field(default_factory=dict)
    notify: NotifySettings = field(default_factory=NotifySettings)

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, Any]]) -> RevalidationSettings:
        section = config.get("revalidate", {})
        unknown = sorted(set(section) - _SETTINGS_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [revalidate]: {', '.join(unknown)}; expected "
                f"{', '.join(sorted(_SETTINGS_KEYS))}"
            )
        overrides = section.get("toolchain_overrides", {})
        if not isinstance(overrides, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
        ):
            raise ConfigError(
                "[revalidate].toolchain_overrides must map toolchain names to 'latest' or a "
                'module, e.g. { questa = "latest" }'
            )
        try:
            parsed = parse_overrides(f"{k}={v}" for k, v in overrides.items())
        except ReleaseError as exc:
            raise ConfigError(f"[revalidate].toolchain_overrides: {exc}") from None
        chosen = {**DEFAULT_COMPARATORS, **section.get("comparators", {})}
        params = section.get("comparator", {})
        if not isinstance(params, dict):
            raise ConfigError("[revalidate.comparator] must be a table of comparator settings")
        comparators: dict[str, Comparator] = {}
        for target, name in chosen.items():
            try:
                if not isinstance(name, str):
                    raise ValueError(f"the comparator name must be a string, got {name!r}")
                settings = params.get(name, {})
                if not isinstance(settings, dict):
                    raise ValueError(f"[revalidate.comparator.{name}] must be a table")
                comparators[target] = get_comparator(name, settings)
            except ValueError as exc:
                raise ConfigError(f"[revalidate.comparators] {target} = {name!r}: {exc}") from None
        notify = section.get("notify", {})
        if not isinstance(notify, dict):
            raise ConfigError("[revalidate.notify] must be a table")
        return cls(
            toolchain_overrides=parsed,
            comparators=comparators,
            notify=NotifySettings.from_mapping(notify),
        )

    def comparator_for(self, action: ActionSpec) -> Comparator | None:
        return self.comparators.get(action.step) or self.comparators.get(action.rule.kind)


# --- running -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RerunOutcome:
    """What the re-run build ended as: its id, status and plan as refined while it ran."""

    build: BuildId
    status: BuildStatus
    plan: Plan


class BuildRunner(Protocol):
    def __call__(
        self, plan: Plan, refiner: Planner, cas: CAS, cache_mode: CacheMode
    ) -> RerunOutcome: ...


class _TreeFallbackCAS:
    """The domain's CAS, whose tree reads fall back to the bundle's tree manifests (the imported
    outputs' trees, which re-planning matches import globs against)."""

    def __init__(self, cas: CAS, trees: Mapping[Digest, TreeManifest]) -> None:
        self._cas = cas
        self._trees = dict(trees)

    def get_tree(self, d: Digest) -> TreeManifest:
        found = self._trees.get(d)
        return found if found is not None else self._cas.get_tree(d)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cas, name)


# --- comparing ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Compared:
    action_id: str
    step: str
    kind: str
    comparator: str
    verdict: Verdict
    counted: bool = True  # False: an incomparable that does not make the verdict `differences`
    missing: bool = False  # the re-run has no result for it

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id, "step": self.step, "kind": self.kind,
            "comparator": self.comparator, "verdict": self.verdict.kind,
            "details": list(self.verdict.details), "counted": self.counted,
        }  # fmt: skip


def _digest_compare(
    action: ActionSpec, old: ResultManifest, new: ResultManifest
) -> tuple[Verdict, bool]:
    details: list[str] = []
    if old.status != new.status:
        details.append(f"status: {old.status} -> {new.status}")
    compared = 0
    for out in action.outputs:
        if not out.deterministic:
            continue
        before, after = old.outputs.get(out.name), new.outputs.get(out.name)
        if before is None and after is None:
            continue
        compared += 1
        if before is None or after is None or before.digest != after.digest:
            details.append(
                f"output {out.name}: {before.digest if before else 'missing'} -> "
                f"{after.digest if after else 'missing'}"
            )
    if details:
        return Verdict.changed(*details), True
    if compared == 0:
        return Verdict.incomparable(
            f"only nondeterministic outputs and no comparator for kind {action.rule.kind!r} "
            "(set one in [revalidate.comparators])"
        ), False  # fmt: skip
    return Verdict.equivalent(), True


class _Materializer:
    """Places outputs of both runs under `root` for the comparators; a missing object is left
    out (the comparator sees no path for that output)."""

    def __init__(self, root: Path, live: CAS, recorded: CAS) -> None:
        self._root = root
        self._live = live
        self._recorded = recorded
        self._n = 0

    def place(self, outputs: Mapping[str, OutputResult], *, old: bool) -> dict[str, Path]:
        self._n += 1
        base = self._root / f"{self._n:06d}"
        placed: dict[str, Path] = {}
        for name, out in sorted(outputs.items()):
            dest = base / name
            sources = (self._recorded, self._live) if old else (self._live,)
            for cas in sources:
                try:
                    if not cas.has(out.digest):
                        continue
                    cas.materialize(out.digest, out.type, dest)
                except (CasError, OSError) as exc:
                    log.warning("revalidate_materialize_failed", digest=str(out.digest),
                                error=str(exc))  # fmt: skip
                    continue
                placed[name] = dest
                break
        return placed


def _recorded_results(bundle: Bundle) -> dict[str, ResultManifest]:
    doc = bundle.json("actions.json")
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, dict):
        raise ReleaseError(f"actions.json of sign-off bundle {bundle.path} has no results")
    try:
        return {
            str(action_id): ResultManifest.model_validate_json(json.dumps(r))
            for action_id, r in results.items()
        }
    except ValueError as exc:
        raise ReleaseError(f"actions.json of sign-off bundle {bundle.path}: {exc}") from None


def _compare_all(
    recorded: Plan,
    old_results: Mapping[str, ResultManifest],
    new_results: Callable[[str], ResultManifest | None],
    new_actions: Mapping[str, ActionSpec],
    settings: RevalidationSettings,
    files: _Materializer,
) -> list[_Compared]:
    compared: list[_Compared] = []
    for action in sorted(recorded.actions, key=lambda a: a.action_id):
        comparator = settings.comparator_for(action)
        name = comparator.name if comparator is not None else "digest"

        def add(verdict: Verdict, *, counted: bool = True, missing: bool = False) -> None:
            compared.append(_Compared(
                action.action_id, action.step, action.rule.kind, name, verdict,  # noqa: B023
                counted=counted, missing=missing,
            ))  # fmt: skip

        old = old_results.get(action.action_id)
        if old is None:
            add(Verdict.incomparable("the release's build has no result for it"), counted=False)
            continue
        if action.action_id not in new_actions:
            add(Verdict.changed("the action is not in the re-planned flow"))
            continue
        new = new_results(action.action_id)
        if new is None:
            add(Verdict.incomparable("no result in the re-run (it did not complete)"),
                missing=True)  # fmt: skip
            continue
        if comparator is None:
            verdict, counted = _digest_compare(action, old, new)
            add(verdict, counted=counted)
            continue
        verdict = comparator.compare(
            files.place(old.outputs, old=True), files.place(new.outputs, old=False),
            old.summary, new.summary,
        )  # fmt: skip
        if verdict.kind == "equivalent" and old.status != new.status:
            verdict = Verdict.changed(f"status: {old.status} -> {new.status}")
        add(verdict)
    return compared


def _verdict(compared: Sequence[_Compared], complete: bool) -> RevalidationVerdict:
    if not complete or any(c.missing for c in compared):
        return "failed"
    if any(c.verdict.kind != "equivalent" and c.counted for c in compared):
        return "differences"
    return "reproduced"


def _counts(compared: Sequence[_Compared]) -> dict[ComparisonKind, int]:
    counts: dict[ComparisonKind, int] = {"equivalent": 0, "changed": 0, "incomparable": 0}
    for c in compared:
        counts[c.verdict.kind] += 1
    return counts


# --- reporting ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Revalidation:
    """The outcome of `revalidate`: the stored record, the report and its Markdown."""

    record: RevalidationRecord
    report: dict[str, JsonValue]
    markdown: str


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Mapping[str, Any]) -> str:
    """The Markdown summary of a revalidation report (the notification body)."""
    rel = report["release"]
    lines = [
        f"# Revalidation of {rel['name']} {rel['version']}: {report['verdict']}",
        "",
        f"- Domain: {rel['domain']}",
        f"- Bundle: `{report['bundle']}`",
        f"- Build: {report['build'] if report['build'] is not None else 'none'}"
        f" ({report['build_status'] or 'not run'}), cache {report['cache_mode']}",
        f"- Run by {report['created_by']} at {report['created_at']}",
        "",
        "## Toolchains",
        "",
    ]
    for name, tc in sorted(report["toolchains"].items()):
        before, after = tc["recorded"]["module"], tc["used"]["module"]
        how = f" (override: {tc['override']})" if tc["override"] else ""
        lines.append(f"- {name}: {before} → {after}{how}")
    counts = report["counts"]
    lines += [
        "",
        "## Actions",
        "",
        f"{counts['equivalent']} equivalent, {counts['changed']} changed, "
        f"{counts['incomparable']} incomparable.",
    ]
    if report.get("error"):
        lines += ["", f"**The re-run failed:** {_md(report['error'])}"]
    shown = [a for a in report["actions"] if a["verdict"] != "equivalent"]
    if shown:
        lines += ["", "| Action | Comparator | Verdict | Details |", "| --- | --- | --- | --- |"]
        for a in shown:
            note = "" if a["counted"] else " (not counted)"
            details = "<br>".join(_md(d) for d in a["details"])
            lines.append(
                f"| `{_md(a['action_id'])}` | {a['comparator']} | {a['verdict']}{note} | "
                f"{details} |"
            )
    return "\n".join(lines) + "\n"


def _toolchain_entries(
    recorded: Plan, resolver: OverrideToolchains, used: Plan | None
) -> dict[str, JsonValue]:
    entries: dict[str, JsonValue] = {}
    for name, tc in sorted(recorded.toolchains.items()):
        chosen = resolver.chosen.get(name)
        if used is not None and name in used.toolchains:
            now = used.toolchains[name]
            after: dict[str, JsonValue] = {"module": now.module, "id": str(now.id)}
        elif chosen is not None:
            after = {"module": chosen.module, "id": str(chosen.id)}
        else:
            after = {"module": tc.module, "id": str(tc.id)}
        entries[name] = {
            "recorded": {"module": tc.module, "id": str(tc.id)},
            "used": after,
            "override": resolver.overrides.get(name),
        }
    return entries


def revalidate(
    bundle: Bundle,
    *,
    store: MetadataBackend,
    cas: CAS,
    rules: RuleRegistry,
    git: Git,
    work: Path,
    run: BuildRunner,
    overrides: Mapping[str, str],
    optional_overrides: Collection[str] = (),
    settings: RevalidationSettings,
    cache_mode: CacheMode = "read",
    by: str,
    clock: Clock | None = None,
) -> Revalidation:
    """Re-run `bundle`'s release (in `work`, created) and attach the report to it.

    Raises for a request that cannot be honoured (cache mode `write`, an override naming no
    toolchain of the plan (unless in `optional_overrides`) or no registered toolchain); a re-run
    that cannot be planned, run or compared is a `failed` revalidation, recorded with its error."""
    clock = clock or SystemClock()
    if cache_mode == "write":
        raise ReleaseError(
            "a revalidation never writes the action cache (its tools are unproven); use the "
            "cache mode read (default) or off (--no-cache)"
        )
    release = bundle.release()
    recorded = bundle.plan()
    resolver = OverrideToolchains(
        bundle.toolchains(), overrides, store, optional=optional_overrides
    )
    domain_cas = cast(CAS, _TreeFallbackCAS(cas, bundle.trees()))
    plan: Plan | None = None
    outcome: RerunOutcome | None = None
    error: str | None = None
    try:
        plan, planner = plan_bundle(
            bundle, cas=domain_cas, toolchains=resolver, rules=rules, git=git, work=work,
            clock=clock,
        )  # fmt: skip
        outcome = run(plan, planner, domain_cas, cache_mode)
    except EbsError as exc:
        error = str(exc)
        log.warning("revalidate_rerun_failed", release=f"{release.name} {release.version}",
                    error=error)  # fmt: skip

    compared: list[_Compared] = []
    compared_all = False
    if outcome is not None:
        build = outcome.build
        try:  # the build ran (maybe for hours): an error here must still be recorded
            scratch = FsCAS(work / "recorded-cas", release.domain, clock=clock)
            restore_objects(bundle, scratch)
            compared = _compare_all(
                recorded, _recorded_results(bundle),
                lambda action_id: store.get_result(build, action_id),
                {a.action_id: a for a in outcome.plan.actions}, settings,
                _Materializer(work / "outputs", cas, scratch),
            )  # fmt: skip
            compared_all = True
        except EbsError as exc:
            error = f"comparing the re-run build {build} failed: {exc}"
            log.warning("revalidate_compare_failed", release=f"{release.name} {release.version}",
                        error=error)  # fmt: skip
    complete = compared_all and outcome is not None and outcome.status in ("passed", "failed")
    verdict = _verdict(compared, complete)
    counts = _counts(compared)
    toolchains = _toolchain_entries(recorded, resolver, outcome.plan if outcome else plan)
    if outcome is not None and error is None and not complete:
        error = f"the re-run build {outcome.build} ended {outcome.status}"
    now = clock.now()
    report: dict[str, JsonValue] = {
        "format": REPORT_FORMAT,
        "v": 1,
        "release": {
            "domain": release.domain,
            "name": release.name,
            "version": release.version,
            "id": int(release.id),
        },
        "bundle": str(bundle.digest),
        "recorded_plan": str(plan_digest(recorded)),
        "plan": None if plan is None else str(plan_digest(outcome.plan if outcome else plan)),
        "build": None if outcome is None else int(outcome.build),
        "build_status": None if outcome is None else outcome.status,
        "cache_mode": cache_mode,
        "toolchains": toolchains,
        "verdict": verdict,
        "counts": {k: n for k, n in counts.items()},
        "actions": [c.to_json() for c in compared],
        "error": error,
        "created_at": now.isoformat(),
        "created_by": by,
    }
    markdown = render_markdown(report)
    data = canonical_json(report)
    text = markdown.encode()
    report_digest, markdown_digest = cas.put_bytes(data), cas.put_bytes(text)
    store.register_blobs(release.domain, [
        BlobRef(digest=report_digest, size=len(data), kind="file"),
        BlobRef(digest=markdown_digest, size=len(text), kind="file"),
    ])  # fmt: skip
    used = {
        name: str(cast(dict[str, Any], entry)["used"]["module"])
        for name, entry in toolchains.items()
    }
    record = store.record_revalidation(
        release.id,
        RevalidationCreate(
            build=None if outcome is None else outcome.build, verdict=verdict,
            report=report_digest, markdown=markdown_digest, toolchains=used, counts=counts,
        ),
        by=by,
    )  # fmt: skip
    return Revalidation(record=record, report=report, markdown=markdown)


# --- notification (R4) -------------------------------------------------------------------------

PostJson = Callable[[str, bytes, float], None]
SendMail = Callable[[EmailMessage, NotifySettings], None]


def _post_json(url: str, body: bytes, timeout_s: float) -> None:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout_s):
        pass


def _send_mail(message: EmailMessage, settings: NotifySettings) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.timeout_s) as smtp:
        smtp.send_message(message)


class Notifier:
    """Sends a revalidation's verdict and Markdown report to the configured webhook (a JSON
    `{"text", "verdict", "release", "report"}` POST) and email recipients. A failed delivery is
    logged and returned, never raised: the revalidation is already recorded."""

    def __init__(
        self,
        settings: NotifySettings,
        *,
        post_json: PostJson = _post_json,
        send_mail: SendMail = _send_mail,
    ) -> None:
        self.settings = settings
        self._post = post_json
        self._send = send_mail

    def notify(self, result: Revalidation) -> list[str]:
        """Deliver `result`; the delivery errors, one line each."""
        s = self.settings
        verdict = result.record.verdict
        if s.when == "problems" and verdict == "reproduced":
            return []
        rel = cast(dict[str, Any], result.report["release"])
        title = f"[ebs] revalidation of {rel['name']} {rel['version']}: {verdict}"
        errors: list[str] = []
        if s.webhook:
            body = json.dumps({
                "text": f"{title}\n\n{result.markdown}", "verdict": verdict,
                "release": f"{rel['name']} {rel['version']}", "domain": rel["domain"],
                "report": str(result.record.report),
            }).encode()  # fmt: skip
            try:
                self._post(s.webhook, body, s.timeout_s)
            except (OSError, ValueError) as exc:
                errors.append(f"webhook {s.webhook}: {exc}")
        if s.email_to:
            message = EmailMessage()
            message["Subject"] = title
            message["From"] = s.email_from
            message["To"] = ", ".join(s.email_to)
            message.set_content(result.markdown)
            try:
                self._send(message, s)
            except (OSError, smtplib.SMTPException) as exc:
                errors.append(f"email via {s.smtp_host}:{s.smtp_port}: {exc}")
        for e in errors:
            log.warning("revalidate_notify_failed", error=e)
        return errors
