"""`ebs-runner`: execute one action of a plan on this node (interfaces.md § 9).

Lifecycle (architecture.md "Runner lifecycle on a node"): load the plan from the CAS by digest
→ create `<scratch>/<build>/<action-hash>/{work,home,tmp,logs}` → stage the declared inputs and
config files → verify every input digest (I10) → run the tool with a scrubbed env (I11) and a
timeout → classify → hash and upload outputs and log → post the ResultManifest (and the cache
entry when the build writes the cache) → delete the scratch directory, whatever happened.

Exit codes: 0 result posted (passed or failed), 64 bad usage, 70 internal error, 75 infra
failure (temporary; the driver retries), 76 input verification failed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TextIO

import ebs
from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.config import config_paths, load_config
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import CasError, ConfigError, DigestError, EbsError, MetadataError, PlanError
from ebs.core.log import get_logger
from ebs.meta.api import BuildId, CacheMode, Event, InfraReason, MetadataStore, RunnerInfo
from ebs.plan.planfile import decode
from ebs.plan.types import ActionSpec, Plan
from ebs.rules.api import RulePlugin, RuleSettings
from ebs.rules.registry import RuleRegistry
from ebs.runner.collect import collect_outputs, log_tail, output_paths
from ebs.runner.config import RunnerSettings
from ebs.runner.env import build_env
from ebs.runner.errors import (
    EXIT_INFRA,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY,
    InfraError,
    InputVerificationError,
    Interrupted,
    RunnerError,
    UsageError,
)
from ebs.runner.result import make_manifest
from ebs.runner.run import ToolRun, run_tool
from ebs.runner.stage import (
    Scratch,
    resolve_contents,
    stage_inputs,
    verify_inputs,
    write_config_files,
)

__all__ = [
    "EXIT_INFRA",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VERIFY",
    "RunRequest",
    "Runner",
    "main",
]

log = get_logger(__name__)

_INFRA_REASONS: dict[str, InfraReason] = {"license": "license", "timeout": "timeout"}


@dataclass(frozen=True, slots=True)
class RunRequest:
    plan: Digest
    action_id: str
    build: BuildId | None  # None: run without posting; the manifest is printed on stdout
    keep_scratch: bool = False


class Runner:
    """Runs one action; every collaborator is injected (main() wires the real ones)."""

    def __init__(
        self,
        *,
        cas: CAS,
        store: MetadataStore | None,
        rules: RuleRegistry,
        settings: RunnerSettings,
        caller_env: Mapping[str, str],
        host: str,
        clock: Clock | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        self._cas = cas
        self._store = store
        self._rules = rules
        self._settings = settings
        self._caller_env = dict(caller_env)
        self._host = host
        self._clock = clock or SystemClock()
        self._out = out or sys.stdout
        self._err = err or sys.stderr

    def run(self, req: RunRequest) -> int:
        """Run the action; returns the exit code. Never raises (except BaseException)."""
        try:
            return self._run(req)
        except InfraError as exc:
            self._emit_infra(req, exc)
            self._err.write(f"ebs-runner: {req.action_id}: {exc}\n")
            return exc.exit_code
        except RunnerError as exc:
            self._err.write(f"ebs-runner: {req.action_id}: {exc}\n")
            return exc.exit_code
        except Exception as exc:
            log.exception("runner internal error", action_id=req.action_id)
            self._err.write(
                f"ebs-runner: {req.action_id}: internal error: {type(exc).__name__}: {exc} "
                "(this is a bug in ebs; please report it with the log above)\n"
            )
            return EXIT_INTERNAL

    # --- lifecycle ------------------------------------------------------------------------------

    def _run(self, req: RunRequest) -> int:
        plan = self._load_plan(req.plan)
        spec = self._select(plan, req.action_id)
        key = spec.key
        if key is None:
            raise UsageError(
                f"action {spec.action_id!r} has no key yet: an input's producer has not run; "
                "the driver must refine the plan before running it"
            )
        rule = self._rule(spec)
        cache_mode = self._cache_mode(req.build)
        contents = resolve_contents(plan, spec, self._store)
        toolchain_env: Mapping[str, str] = {}
        if spec.toolchain is not None:
            planned = plan.toolchains.get(spec.toolchain.name)
            if planned is None or planned.id != spec.toolchain.id:
                raise UsageError(
                    f"action {spec.action_id!r} uses toolchain {spec.toolchain.name!r} "
                    f"({spec.toolchain.id}) but the plan's toolchains do not record that id, so "
                    "its env is unknown; re-plan"
                )
            toolchain_env = planned.env
        if spec.resources.time is not None and not isinstance(spec.resources.time, int):
            raise UsageError(
                f"action {spec.action_id!r} has an unexpanded time limit "
                f"{spec.resources.time!r}; the plan must hold seconds"
            )

        build_dir = "local" if req.build is None else str(req.build)
        try:
            scratch = Scratch.create(self._settings.scratch_dir, build_dir, spec.action_id)
        except OSError as exc:
            raise InfraError(
                f"cannot create the scratch dir under {self._settings.scratch_dir}: {exc}"
            ) from exc
        log_path = scratch.logs / "tool.log"
        try:
            stage_inputs(spec, self._cas, scratch.work, contents)
            write_config_files(spec, scratch.work)
            verify_inputs(spec, scratch.work, contents)  # I10: before anything executes
            env = build_env(
                spec,
                toolchain_env=toolchain_env,
                scratch=scratch,
                caller_env=self._caller_env,
                passthrough_patterns=self._settings.passthrough_env,
            )
            run = run_tool(
                spec.argv,
                cwd=scratch.work,
                env=env,
                log_path=log_path,
                timeout_s=spec.resources.time if isinstance(spec.resources.time, int) else None,
                max_log=self._settings.max_log,
                grace_s=self._settings.kill_grace_s,
            )
            return self._finish(req, spec, key, rule, cache_mode, run, scratch, log_path)
        except InfraError as exc:
            self._keep_log(exc, log_path)  # before the scratch dir goes
            raise
        finally:
            if req.keep_scratch:
                self._err.write(f"ebs-runner: scratch kept at {scratch.root}\n")
            else:
                scratch.remove()

    def _finish(
        self,
        req: RunRequest,
        spec: ActionSpec,
        key: Digest,
        rule: RulePlugin,
        cache_mode: CacheMode,
        run: ToolRun,
        scratch: Scratch,
        log_path: Path,
    ) -> int:
        # A program that cannot start is treated as infra, not FAILED: it usually means a node or
        # toolchain install problem (automount not ready, tool missing on this node), and a
        # FAILED result would be cached for every node. Retries are bounded by the driver.
        if run.exec_error is not None:
            raise _ran(InfraError(run.exec_error), run)
        if run.log_error is not None:
            raise _ran(InfraError(run.log_error), run)
        if run.timed_out:
            timeout = InfraError(
                f"the tool exceeded its time limit of {spec.resources.time} s and was killed",
                reason="timeout",
            )
            raise _ran(timeout, run)
        classification = rule.classify(
            run.exit_code, log_tail(log_path), output_paths(spec, scratch.work)
        )
        if classification.status == "infra":
            reason = classification.reason or "other"
            infra = InfraError(
                f"infrastructure failure ({reason}), exit code {run.exit_code}",
                reason=_INFRA_REASONS.get(reason, "other"),
                detail=reason,
            )
            raise _ran(infra, run)

        try:
            collected = collect_outputs(spec, scratch.work, self._cas, key)
            log_digest = self._cas.put_file(log_path)
        except (CasError, OSError) as exc:
            raise _ran(InfraError(f"cannot upload outputs to the CAS: {exc}"), run) from exc
        summary = dict(rule.summarize(output_paths(spec, scratch.work), log_path))
        if collected.missing:
            summary["missing_output"] = ",".join(collected.missing)
        manifest = make_manifest(
            spec,
            key=key,
            passed=classification.status == "passed" and not collected.missing,
            run=run,
            outputs=collected.outputs,
            log=log_digest,
            summary=summary,
            runner=RunnerInfo(
                version=ebs.__version__,
                host=self._host,
                slurm_job_id=self._caller_env.get("SLURM_JOB_ID") or None,
            ),
        )
        if req.build is None or self._store is None:
            self._out.write(json.dumps(manifest.to_json(), sort_keys=True) + "\n")
            return EXIT_OK
        try:
            self._store.record_result(req.build, spec.action_id, manifest)
            if cache_mode == "write":
                self._store.cache_put(spec.domain, key, manifest)
        except MetadataError as exc:
            raise InfraError(f"cannot post the result: {exc}") from exc
        log.info("result posted", action_id=spec.action_id, status=manifest.status)
        return EXIT_OK

    def _keep_log(self, error: InfraError, log_path: Path) -> None:
        """Upload the tool log of a failed attempt for debugging (best effort)."""
        if error.log is None and log_path.exists():
            with contextlib.suppress(CasError, OSError):
                error.log = self._cas.put_file(log_path)

    def _emit_infra(self, req: RunRequest, error: InfraError) -> None:
        """Report an infrastructure failure: an event, never a result or cache entry (I12)."""
        if req.build is None or self._store is None:
            return
        event = Event(
            ts=self._clock.now(),
            build=req.build,
            type="infra_failed",
            action_id=req.action_id,
            data={
                "reason": error.reason,
                "detail": error.detail,
                "exit_code": error.tool_exit_code,
                "log": None if error.log is None else str(error.log),
            },
        )
        try:
            self._store.emit(event)
        except MetadataError as exc:
            log.warning("cannot emit infra_failed event", error=str(exc))

    # --- helpers --------------------------------------------------------------------------------

    def _load_plan(self, digest: Digest) -> Plan:
        if not self._cas.has(digest):
            raise UsageError(
                f"plan {digest} is not in CAS domain {self._cas.domain!r}: check --plan and "
                "--domain (the driver stores plan.json before submitting)"
            )
        try:
            with self._cas.open(digest) as f:
                data = f.read()
        except (CasError, OSError) as exc:
            raise InfraError(f"cannot read plan {digest}: {exc}") from exc
        if hash_bytes(data, digest.algo) != digest:
            raise InputVerificationError(
                f"plan {digest} in the CAS does not match its digest (corrupt CAS object)"
            )
        try:
            plan = decode(data)
        except PlanError as exc:
            raise UsageError(f"plan {digest} is not a valid plan.json: {exc}") from exc
        if plan.domain != self._cas.domain:
            raise UsageError(
                f"plan {digest} belongs to domain {plan.domain!r}, but this runner's CAS is "
                f"domain {self._cas.domain!r}"
            )
        return plan

    def _select(self, plan: Plan, action_id: str) -> ActionSpec:
        try:
            spec = plan.action(action_id)
        except KeyError:
            ids = [a.action_id for a in plan.actions]
            shown = ", ".join(ids[:10]) + (f", … ({len(ids)} in all)" if len(ids) > 10 else "")
            raise UsageError(f"the plan has no action {action_id!r}; it has: {shown}") from None
        if spec.domain != self._cas.domain:
            raise UsageError(
                f"action {action_id!r} is in domain {spec.domain!r}, not {self._cas.domain!r}"
            )
        return spec

    def _rule(self, spec: ActionSpec) -> RulePlugin:
        try:
            rule = self._rules.get(spec.rule.kind)
        except EbsError as exc:
            raise UsageError(str(exc)) from exc
        if rule.version != spec.rule.version:
            raise UsageError(
                f"rule {spec.rule.kind!r} is version {rule.version} on this node but the plan "
                f"was made with version {spec.rule.version}; install the same ebs version on "
                "the nodes as on the planning host"
            )
        return rule

    def _cache_mode(self, build: BuildId | None) -> CacheMode:
        if build is None:
            return "off"
        if self._store is None:
            raise UsageError("--build needs the metadata store: configure [metadata].url")
        try:
            return self._store.get_build(build).cache_mode
        except MetadataError as exc:
            raise InfraError(f"cannot read build {build}: {exc}") from exc


def _ran(error: InfraError, run: ToolRun) -> InfraError:
    error.tool_exit_code = run.exit_code
    return error


# --- command line -------------------------------------------------------------------------------


class _UsageExit(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"ebs-runner: {message}\n")
        raise _UsageExit


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ebs-runner", description="Execute one action of a plan on this node.")
    p.add_argument("--plan", required=True, help="plan.json digest (sha256:…)")
    p.add_argument("--action", help="action id to run")
    p.add_argument("--array-map", help="array map digest (SLURM job arrays; P1-04)")
    p.add_argument("--build", type=int, help="build id to post the result to")
    p.add_argument("--domain", help="CAS domain (default: the build's)")
    p.add_argument("--keep-scratch", action="store_true", help="keep the scratch dir, print it")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _UsageExit:
        return EXIT_USAGE
    environ = dict(os.environ)
    store: MetadataStore | None = None
    try:
        if args.array_map is not None:
            raise UsageError("--array-map (SLURM job arrays) arrives with task P1-04")
        if args.action is None:
            raise UsageError("--action is required")
        try:
            plan = Digest.parse(args.plan)
        except DigestError as exc:
            raise UsageError(f"--plan: {exc}") from exc
        home = Path(environ.get("HOME") or Path.home())
        config = load_config(config_paths(environ, cwd=Path.cwd(), home=home))
        settings = RunnerSettings.from_config(config, environ)
        metadata = config.get("metadata", {})
        if args.build is not None:
            if "url" not in metadata:
                raise UsageError("--build needs the metadata store: configure [metadata].url")
            store = _open_store(metadata)
        domain = _domain(args.domain, store, args.build)
        cas = FsCAS(settings.cas_root, domain)
        rules = RuleRegistry.from_entry_points(
            settings=RuleSettings(license_error_patterns=settings.license_error_patterns)
        )
        runner = Runner(
            cas=cas,
            store=store,
            rules=rules,
            settings=settings,
            caller_env=environ,
            host=socket.gethostname(),
        )
        request = RunRequest(
            plan, args.action, None if args.build is None else BuildId(args.build),
            keep_scratch=args.keep_scratch,
        )  # fmt: skip
        with _signal_handlers():
            return runner.run(request)
    except RunnerError as exc:
        sys.stderr.write(f"ebs-runner: {exc}\n")
        return exc.exit_code
    except MetadataError as exc:
        sys.stderr.write(f"ebs-runner: metadata store unavailable: {exc}\n")
        return EXIT_INFRA
    except (ConfigError, EbsError) as exc:
        sys.stderr.write(f"ebs-runner: {exc}\n")
        return EXIT_USAGE
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def _open_store(section: Mapping[str, object]) -> MetadataStore:
    from ebs.meta.pg import PgMetadataStore  # only when posting: keeps plain runs light

    return PgMetadataStore.from_config(section)


def _domain(given: str | None, store: MetadataStore | None, build: int | None) -> str:
    if store is None or build is None:
        if given is None:
            raise UsageError("--domain is required without --build")
        return given
    domain = store.get_build(BuildId(build)).domain
    if given is not None and given != domain:
        raise UsageError(f"--domain {given!r} does not match build {build}'s domain {domain!r}")
    return domain


@contextlib.contextmanager
def _signal_handlers() -> Iterator[None]:
    """SIGTERM/SIGINT raise Interrupted: the tool's group is killed and scratch removed."""

    def handler(signum: int, frame: FrameType | None) -> None:
        # Ignore repeats so the cleanup that follows is not interrupted itself.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise Interrupted(signum)

    previous = {s: signal.signal(s, handler) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
