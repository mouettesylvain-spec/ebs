"""`ebs release revalidate NAME VERSION | --all` (P3-05): re-run a release's sign-off plan with
some toolchains overridden, compare it with the archived results, attach the report.

The bundle is the release's latest archive (`ebs release archive`), checked against its recorded
digest. `--toolchain-override NAME=latest|MODULE` (repeatable) adds to, and wins over,
`[revalidate].toolchain_overrides`. The re-run is a build like `ebs build --keep-going`, on
`--executor`, reading the action cache (`--no-cache`: not even that) and never writing it.
`--all --domain D` revalidates every release of D that has an archive and is not yanked (the
quarterly timer, deploy/revalidate). Each report goes to `[revalidate.notify]`. Exit 0 when
every revalidation reproduced its release, 1 otherwise.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import typer

from ebs.cas.api import CAS
from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli.build import EXECUTOR_OPTION, ExecutorChoice
from ebs.cli.plan import JSON_OPTION
from ebs.cli.release import DOMAIN_OPTION, default_domain, find_release, release_store
from ebs.cli.verify import bundle_of
from ebs.core.errors import EbsError, ExitCode
from ebs.driver.build import run_build
from ebs.driver.retry import ReasonRetry, RetrySettings
from ebs.driver.scheduler import DriverConfig
from ebs.meta.api import CacheMode, MetadataBackend, ReleaseView
from ebs.plan.planner import Planner
from ebs.plan.types import Plan
from ebs.release.archive import Git, open_bundle
from ebs.release.revalidate import (
    Notifier,
    RerunOutcome,
    Revalidation,
    RevalidationSettings,
    parse_overrides,
    revalidate,
)

__all__ = ["revalidate_command"]


class _Runner:
    """Runs a re-planned bundle as a build on the chosen executor (`BuildRunner`)."""

    def __init__(self, services: SiteServices, store: MetadataBackend, executor: str) -> None:
        self.services = services
        self.store = store
        self.executor = executor
        self.retry = ReasonRetry(RetrySettings.from_config(services.config()))

    def __call__(
        self, plan: Plan, refiner: Planner, cas: CAS, cache_mode: CacheMode
    ) -> RerunOutcome:
        services = self.services
        with services.open_executor(
            self.executor, domain=plan.domain, log_dir=services.cwd / ".ebs" / "logs",
            project=plan.project,
        ) as ex:  # fmt: skip
            outcome = run_build(
                plan, refiner=refiner, cas=cas, store=self.store, executor=ex,
                workdir=services.cwd, user=services.user(),
                config=DriverConfig(cache_mode=cache_mode, keep_going=True),
                clock=services.clock, retry=self.retry, handle_sigint=True, handle_sigterm=True,
            )  # fmt: skip
        return RerunOutcome(build=outcome.build, status=outcome.status, plan=outcome.plan)


def _targets(
    store: MetadataBackend, domain: str, name: str | None, version: str | None, every: bool
) -> list[ReleaseView]:
    if every:
        if name is not None or version is not None:
            raise UsageError("give NAME VERSION or --all, not both")
        return [
            r for r in store.list_releases(domain)
            if r.yanked_at is None and store.list_archives(r.id)
        ]  # fmt: skip
    if name is None or version is None:
        raise UsageError("give the release as NAME VERSION, or --all with --domain")
    return [find_release(store, domain, name, version)]


def _show(out: Output, result: Revalidation, errors: list[str], json_mode: bool) -> None:
    record = result.record
    rel: dict[str, Any] = result.report["release"]  # type: ignore[assignment]
    if json_mode:
        out.emit({
            "release": f"{rel['name']} {rel['version']}", "verdict": record.verdict,
            "record": record.model_dump(mode="json"), "notify_errors": errors,
        })  # fmt: skip
        return
    c = record.counts
    build = f"build {record.build}" if record.build is not None else "no build"
    out.line(
        f"{record.verdict} {rel['name']} {rel['version']} ({build}): "
        f"{c.get('equivalent', 0)} equivalent, {c.get('changed', 0)} changed, "
        f"{c.get('incomparable', 0)} incomparable; report {record.report}"
    )
    for name, module in sorted(record.toolchains.items()):
        out.line(f"  {name}: {module}")
    for e in errors:
        out.warn(f"notification failed: {e}")


NAME_ARGUMENT = typer.Argument(None, metavar="[NAME]", help="Release name.")
VERSION_ARGUMENT = typer.Argument(None, metavar="[VERSION]", help="Release version.")
OVERRIDE_OPTION = typer.Option(
    [],
    "--toolchain-override",
    metavar="NAME=latest|MODULE",
    help="Run toolchain NAME on its newest registered version (latest) or on MODULE; repeat.",
)


@handle_errors
def revalidate_command(
    ctx: typer.Context,
    name: str | None = NAME_ARGUMENT,
    version: str | None = VERSION_ARGUMENT,
    every: bool = typer.Option(
        False, "--all", help="Every archived, unyanked release of the domain."
    ),
    domain: str | None = DOMAIN_OPTION,
    override: list[str] = OVERRIDE_OPTION,
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Re-run every action (default: reuse cached results)."
    ),
    executor: ExecutorChoice = EXECUTOR_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Re-run releases from their sign-off bundles on overridden toolchains and compare."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    settings = RevalidationSettings.from_config(services.config())
    given = parse_overrides(override)
    overrides = {**settings.toolchain_overrides, **given}
    optional = set(settings.toolchain_overrides) - set(given)  # skipped where not in the plan
    store = release_store(services)
    domain = default_domain(services, domain)
    targets = _targets(store, domain, name, version, every)
    if not targets and not json_mode:
        out.line(f"no archived release to revalidate in domain {domain}")
    notifier = Notifier(settings.notify)
    runner = _Runner(services, store, executor.value)
    reproduced = True
    for rel in targets:
        try:
            services.check_domain_access(rel.domain)
            path = bundle_of(services, rel.name, rel.version, domain)
            with tempfile.TemporaryDirectory(prefix="ebs-revalidate-") as tmp:
                bundle = open_bundle(path, Path(tmp) / "bundle")
                result = revalidate(
                    bundle, store=store, cas=services.cas(rel.domain), rules=services.rules(),
                    git=Git(), work=Path(tmp) / "work", run=runner, overrides=overrides,
                    optional_overrides=optional, settings=settings,
                    cache_mode="off" if no_cache else "read", by=services.user(),
                    clock=services.clock,
                )  # fmt: skip
        except EbsError as exc:
            if not every:
                raise
            # --all (the quarterly timer): one release that cannot be revalidated must not
            # keep the others from being revalidated.
            reproduced = False
            if json_mode:
                out.emit({"release": f"{rel.name} {rel.version}", "verdict": None,
                          "error": str(exc)})  # fmt: skip
            else:
                out.warn(f"cannot revalidate {rel.name} {rel.version}: {exc}")
            continue
        errors = notifier.notify(result)
        _show(out, result, errors, json_mode)
        reproduced = reproduced and result.record.verdict == "reproduced"
    if not reproduced:
        raise typer.Exit(int(ExitCode.ACTIONS_FAILED))
