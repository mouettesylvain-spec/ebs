"""`ebs-runner`: execute one action of a plan on this node (interfaces.md § 9).

Lifecycle (architecture.md "Runner lifecycle on a node"): load the plan from the CAS by digest
→ create `<scratch>/<build>/<action-hash>/{work,home,tmp,logs}` (marked with the SLURM job id
for the node epilog) → stage the declared inputs (large ones linked read-only from the CAS) and
config files → verify every input digest (I10) → emit `running`, run the tool with a scrubbed
env (I11) and a timeout inside the action's sandbox (P3-01: staging only, bwrap or apptainer),
mirroring its output to the live log and renewing the build lease → classify → hash and upload
outputs and log → post the ResultManifest (and the cache entry when the build writes the cache)
→ copy debug files to the debug area → delete the scratch directory, whatever happened (I16).

Exit codes: 0 result posted (passed or failed), 64 bad usage, 70 internal error, 75 infra
failure (temporary; the driver retries), 76 input verification failed.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import signal
import socket
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final, Literal, TextIO

import ebs
from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.config import config_paths, load_config
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import (
    CasError,
    ConfigError,
    DigestError,
    EbsError,
    MetadataError,
    PlanError,
    SandboxError,
)
from ebs.core.log import get_logger
from ebs.flow.model import SandboxMode
from ebs.meta.api import (
    LEASE_TTL_S,
    BuildId,
    CacheMode,
    Event,
    InfraReason,
    LeaseId,
    MetadataStore,
    RunnerInfo,
)
from ebs.plan.planfile import decode
from ebs.plan.types import ActionSpec, Plan
from ebs.rules.api import RulePlugin, RuleSettings
from ebs.rules.registry import RuleRegistry
from ebs.runner.collect import (
    collect_debug,
    collect_outputs,
    log_tail,
    make_debug_dir,
    output_paths,
    stored_objects,
    wants_debug,
)
from ebs.runner.config import RunnerSettings
from ebs.runner.env import build_env, toolchain_env
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
    format_infra_line,
)
from ebs.runner.heartbeat import Heartbeat
from ebs.runner.livelog import LiveLog
from ebs.runner.result import make_manifest
from ebs.runner.run import ToolRun, run_tool
from ebs.runner.stage import Scratch, prepare_work, resolve_contents
from ebs.sandbox.api import Mount, Sandbox, undeclared_read_hint
from ebs.sandbox.detect import Detection, detect

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
_INTERRUPTED_DEBUG_MAX: Final = 16 << 20  # after SIGTERM, cleanup must fit in SLURM's KillWait
_LOG_HEAD: Final = 4096  # bytes of the tool log searched for a sandbox setup error

Status = Literal["passed", "failed", "infra"]


@dataclass(frozen=True, slots=True)
class RunRequest:
    plan: Digest
    action_id: str
    build: BuildId | None  # None: run without posting; the manifest is printed on stdout
    keep_scratch: bool = False


@dataclass(slots=True)
class _Attempt:
    """What the cleanup needs to know about how the run went."""

    status: Status = "infra"  # until a result is posted
    interrupted: bool = False
    env: Mapping[str, str] | None = None
    debug: _Debug | None = None


@dataclass(frozen=True, slots=True)
class _Debug:
    dest: Path
    live: LiveLog


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
        renew_lease: Callable[[], None] | None = None,
        sandboxes: Callable[[SandboxMode], Detection] | None = None,
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
        self._renew_lease = renew_lease  # P1-09 wires the build lease; None until then
        self._sandboxes = sandboxes or functools.partial(
            detect, apptainer_image=settings.sandbox_apptainer_image
        )

    def run(self, req: RunRequest) -> int:
        """Run the action; returns the exit code. Never raises (except BaseException)."""
        try:
            return self._run(req)
        except InfraError as exc:
            # stderr first: if the store is down, emitting raises and the reason must not be lost.
            self._err.write(f"ebs-runner: {req.action_id}: {exc}\n")
            self._err.write(format_infra_line(exc.reason, exc.detail))
            self._err.flush()
            self._emit_infra(req, exc)
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
        tool_env = toolchain_env(plan, spec)
        if spec.resources.time is not None and not isinstance(spec.resources.time, int):
            raise UsageError(
                f"action {spec.action_id!r} has an unexpanded time limit "
                f"{spec.resources.time!r}; the plan must hold seconds"
            )

        sandbox = self._sandbox(spec)
        build_dir = "local" if req.build is None else str(req.build)
        job_id = self._caller_env.get("SLURM_JOB_ID") or None
        try:
            scratch = Scratch.create(
                self._settings.scratch_dir, build_dir, spec.action_id, job_id=job_id
            )
        except OSError as exc:
            raise InfraError(
                f"cannot create the scratch dir under {self._settings.scratch_dir}: {exc}"
            ) from exc
        log_path = scratch.logs / "tool.log"
        attempt = _Attempt()
        try:
            attempt.debug = self._open_debug(spec, build_dir)
            prepare_work(  # I10: every input verified before anything executes
                spec,
                self._cas,
                scratch.work,
                contents,
                copy_threshold=self._settings.copy_threshold,
            )
            self._touch_inputs(spec, contents)
            env = build_env(
                spec,
                toolchain_env=tool_env,
                scratch=scratch,
                caller_env=self._caller_env,
                passthrough_patterns=self._settings.passthrough_env,
            )
            attempt.env = env
            self._emit_running(req, spec, job_id)
            beat = Heartbeat(
                self._clock, self._settings.heartbeat_s, self._lease_hook(req, spec, contents)
            )
            beat.tick()
            live = None if attempt.debug is None else attempt.debug.live

            def poll() -> None:
                beat.tick()
                if live is not None:
                    live.tick()

            try:
                argv = sandbox.wrap(
                    spec.argv, self._mounts(plan, spec, scratch), env, os.path.abspath(scratch.work)
                )
            except SandboxError as exc:  # a path the sandbox cannot express: fix the config
                raise UsageError(
                    f"cannot run the action in the {sandbox.name} sandbox: {exc}"
                ) from exc
            run = run_tool(
                argv,
                cwd=scratch.work,
                env=env,
                log_path=log_path,
                timeout_s=spec.resources.time if isinstance(spec.resources.time, int) else None,
                max_log=self._settings.max_log,
                grace_s=self._settings.kill_grace_s,
                tee=None if live is None else live.write,
                on_poll=poll,
            )
            code, attempt.status = self._finish(
                req, spec, key, rule, cache_mode, run, scratch, log_path, sandbox
            )
            return code
        except InfraError as exc:
            attempt.interrupted = isinstance(exc, Interrupted)
            self._keep_log(exc, log_path)  # before the scratch dir goes
            raise
        finally:
            try:
                self._close_debug(spec, attempt, scratch, log_path)
            except Interrupted:  # a first SIGTERM during collection: the scratch still goes
                log.warning("debug collection interrupted by a signal", action_id=spec.action_id)
            finally:
                self._drop_scratch(req, scratch)

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
        sandbox: Sandbox,
    ) -> tuple[int, Status]:
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
        setup_error = sandbox.setup_error(run.exit_code, _log_head(log_path))
        if setup_error is not None:  # the tool never started: not a result of the tool
            raise _ran(InfraError(f"the {sandbox.name} sandbox failed: {setup_error}"), run)
        tail = log_tail(log_path)
        classification = rule.classify(run.exit_code, tail, output_paths(spec, scratch.work))
        hint = None
        if classification.status != "passed":
            hint = undeclared_read_hint(tail, sandbox=sandbox.name, step=spec.step)
        if hint is not None:  # R3: before an infra raise too, so the driver's log shows it
            self._err.write(f"ebs-runner: {spec.action_id}: hint: {hint}\n")
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
        if hint is not None:
            summary["sandbox_hint"] = hint
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
                sandbox=sandbox.name,
            ),
        )
        status: Status = "passed" if manifest.status == "passed" else "failed"
        if req.build is None or self._store is None:
            self._out.write(json.dumps(manifest.to_json(), sort_keys=True) + "\n")
            return EXIT_OK, status
        try:
            # Register every stored object first: a put is a use, and GC must see it (P1-09 R1).
            stored = stored_objects(self._cas, collected.outputs, log_digest, log_path)
            self._store.register_blobs(spec.domain, stored)
            # A put of bytes that already existed kept the stored object; GC may have claimed it
            # just before the registration (which blocks any later claim). Never post a result
            # whose bytes are gone: the retry stores them again (I15).
            gone = [str(r.digest) for r in stored if not self._cas.has(r.digest)]
            if gone:
                raise InfraError(
                    f"outputs were garbage-collected while being stored ({', '.join(gone[:3])}); "
                    "retrying the action stores them again"
                )
            self._store.record_result(req.build, spec.action_id, manifest)
            if cache_mode == "write":
                self._store.cache_put(spec.domain, key, manifest)
        except MetadataError as exc:
            raise InfraError(f"cannot post the result: {exc}") from exc
        log.info("result posted", action_id=spec.action_id, status=manifest.status)
        return EXIT_OK, status

    # --- sandbox (P3-01) ----------------------------------------------------------------------

    def _sandbox(self, spec: ActionSpec) -> Sandbox:
        """The sandbox for the action's mode on this node; InfraError if it cannot have it."""
        try:
            found = self._sandboxes(spec.sandbox)
        except SandboxError as exc:
            raise InfraError(f"cannot run the action in its sandbox: {exc}") from exc
        # stderr, not the logger: without a store the manifest is printed on stdout.
        if found.warning is not None:
            self._err.write(f"ebs-runner: {spec.action_id}: warning: {found.warning}\n")
        return found.sandbox

    def _mounts(self, plan: Plan, spec: ActionSpec, scratch: Scratch) -> list[Mount]:
        """Toolchain install roots, site binds and the domain's CAS (large inputs are links
        into it) read-only, then the action's scratch read-write, at the same paths inside."""
        ro: list[str] = []
        if spec.toolchain is not None:
            ro += plan.toolchains[spec.toolchain.name].install_roots  # checked by toolchain_env
        ro += self._settings.sandbox_ro_binds
        ro.append(os.path.abspath(self._settings.cas_root_for(spec.domain) / spec.domain))
        root = os.path.abspath(scratch.root)
        return [*(Mount(p, p) for p in dict.fromkeys(ro)), Mount(root, root, writable=True)]

    # --- grid: running event, debug area, scratch ---------------------------------------------

    def _emit_running(self, req: RunRequest, spec: ActionSpec, job_id: str | None) -> None:
        """Say once where the tool runs (R5); the driver's own `running` comes from SLURM."""
        if req.build is None or self._store is None:
            return
        event = Event(
            ts=self._clock.now(),
            build=req.build,
            type="running",
            action_id=spec.action_id,
            data={"host": self._host, "job_id": job_id},
        )
        try:
            self._store.emit(event)
        except MetadataError as exc:
            log.warning("cannot emit running event", error=str(exc))

    def _open_debug(self, spec: ActionSpec, build_dir: str) -> _Debug | None:
        """The action's debug dir with its live log; None without `[debug].root` or on error."""
        root = self._settings.debug_root_for(spec.domain)
        if root is None:
            return None
        try:
            dest = make_debug_dir(root, spec.domain, build_dir, spec.action_id)
            return _Debug(dest, LiveLog(dest / "live.log", clock=self._clock))
        except OSError as exc:
            self._debug_unavailable(spec, exc)
            return None

    def _close_debug(
        self, spec: ActionSpec, attempt: _Attempt, scratch: Scratch, log_path: Path
    ) -> None:
        """Keep the live log and collect debug files, or drop both after a pass (R2, R3)."""
        debug = attempt.debug
        if debug is None:
            return
        debug.live.close()
        if not wants_debug(spec.debug, attempt.status):
            debug.live.remove()
            with contextlib.suppress(OSError):
                debug.dest.rmdir()  # only if nothing else is in it
                debug.dest.parent.rmdir()  # the build dir, once no action left any
            return
        max_size = spec.debug.max_size
        if max_size is None:
            max_size = self._settings.debug_max_size
        patterns = spec.debug.collect
        # The live log counts against the cap (R2). Past half of it, drop it: tool.log holds
        # the same output's tail, and gets the room instead.
        reserved = debug.live.size()
        if reserved > max_size // 2:
            debug.live.remove()
            reserved = 0
        if attempt.interrupted:  # SLURM sends SIGKILL after KillWait: keep it short
            patterns, max_size = (), min(max_size, _INTERRUPTED_DEBUG_MAX)
        try:
            collect_debug(
                debug.dest,
                work=scratch.work,
                patterns=patterns,
                tool_log=log_path,
                command=self._command(spec, attempt.env),
                max_size=max_size,
                reserved=reserved,
                exclude=[ref.logical_path for ref in spec.inputs],
            )
        except OSError as exc:
            self._debug_unavailable(spec, exc)
            return

    def _command(self, spec: ActionSpec, env: Mapping[str, str] | None) -> dict[str, object]:
        """command.json: what ran, with which env and inputs (`ebs debug`/`reproduce`, P1-10)."""
        return {
            "action_id": spec.action_id,
            "key": None if spec.key is None else str(spec.key),
            "argv": list(spec.argv),
            "env": dict(env or {}),
            "inputs": [
                {"path": r.logical_path, "kind": r.kind, "id": None if r.id is None else str(r.id)}
                for r in spec.inputs
            ],
            "host": self._host,
            "slurm_job_id": self._caller_env.get("SLURM_JOB_ID") or None,
        }

    def _debug_unavailable(self, spec: ActionSpec, exc: OSError) -> None:
        log.warning("debug area unavailable", action_id=spec.action_id, error=str(exc))
        self._err.write(
            f"ebs-runner: {spec.action_id}: debug area unavailable, nothing kept there: {exc}\n"
        )

    def _drop_scratch(self, req: RunRequest, scratch: Scratch) -> None:
        if req.keep_scratch:
            self._err.write(f"ebs-runner: scratch kept at {scratch.root}\n")
            return
        try:
            scratch.remove()
        except Interrupted:  # the first SIGTERM landed mid-removal; repeats are now ignored
            scratch.remove()

    def _lease_hook(
        self, req: RunRequest, spec: ActionSpec, contents: Mapping[str, Digest]
    ) -> Callable[[], None] | None:
        """What the heartbeat calls (R5): the injected hook, else this action's own GC lease.

        The driver's lease (P1-09 `LeaseKeeper`) lives in the driver; the runner holds a lease
        of its own on the inputs it staged, which large inputs are read through as links into
        the CAS, so they stay GC roots while the tool runs even if the driver stalls. It is
        acquired on the first beat and renewed on each later one (re-acquired if it expired);
        it lapses after `LEASE_TTL_S` or when the build finishes.
        """
        if self._renew_lease is not None:
            return self._renew_lease
        store, build = self._store, req.build
        digests = sorted(set(contents.values()), key=str)
        if store is None or build is None or not digests:
            return None
        held: list[LeaseId] = []

        def renew() -> None:
            if held:
                try:
                    store.renew_lease(build, held[0], LEASE_TTL_S)
                    return
                except MetadataError:  # expired or dropped: acquire a new one
                    held.clear()
            held.append(store.acquire_lease(spec.domain, build, digests, LEASE_TTL_S))

        return renew

    def _touch_inputs(self, spec: ActionSpec, contents: Mapping[str, Digest]) -> None:
        """Record the use of the staged inputs for GC (best effort: never fails the action)."""
        if self._store is None or not contents:
            return
        try:
            self._store.touch(spec.domain, sorted(set(contents.values()), key=str))
        except MetadataError as exc:
            log.warning("cannot record input access", action_id=spec.action_id, error=str(exc))

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


def _log_head(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(_LOG_HEAD).decode("utf-8", errors="replace")
    except OSError:
        return ""


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
    p.add_argument("--array-map", help="array map digest; runs actions[$SLURM_ARRAY_TASK_ID]")
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
        if (args.action is None) == (args.array_map is None):
            raise UsageError("give exactly one of --action and --array-map")
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
        cas = FsCAS(settings.cas_root_for(domain), domain)
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
        action = args.action
        if action is None:
            action = array_action(cas, args.array_map, plan, environ.get("SLURM_ARRAY_TASK_ID"))
        request = RunRequest(
            plan, action, None if args.build is None else BuildId(args.build),
            keep_scratch=args.keep_scratch,
        )  # fmt: skip
        with _signal_handlers():
            return runner.run(request)
    except RunnerError as exc:
        sys.stderr.write(f"ebs-runner: {exc}\n")
        return exc.exit_code
    except MetadataError as exc:
        sys.stderr.write(f"ebs-runner: metadata store unavailable: {exc}\n")
        sys.stderr.write(format_infra_line("other", f"metadata store unavailable: {exc}"))
        return EXIT_INFRA
    except (ConfigError, EbsError) as exc:
        sys.stderr.write(f"ebs-runner: {exc}\n")
        return EXIT_USAGE
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def array_action(cas: CAS, array_map: str, plan: Digest, task: str | None) -> str:
    """The action of array task `task` in the executor's array map (interfaces.md § 9).

    The map is `{"v": 1, "plan": …, "actions": [<task 0>, …]}`; `ebs.exec.slurm.arrays` writes
    it, but the runner may not import `ebs.exec`, so this reads it on its own.
    """
    try:
        digest = Digest.parse(array_map)
    except DigestError as exc:
        raise UsageError(f"--array-map: {exc}") from exc
    if task is None or not task.isdigit():
        raise UsageError(f"--array-map needs $SLURM_ARRAY_TASK_ID (got {task!r})")
    if not cas.has(digest):
        raise UsageError(f"array map {digest} is not in CAS domain {cas.domain!r}")
    with cas.open(digest) as f:
        try:
            doc = json.loads(f.read())
        except ValueError as exc:
            raise UsageError(f"array map {digest} is not JSON: {exc}") from exc
    actions = doc.get("actions") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("v") != 1
        or not isinstance(actions, list)
        or not all(isinstance(a, str) for a in actions)
    ):
        raise UsageError(
            f"array map {digest} is not a version 1 map; was it written by another ebs?"
        )
    if doc.get("plan") != str(plan):
        raise UsageError(f"array map {digest} is for plan {doc.get('plan')}, not {plan}")
    index = int(task)
    if index >= len(actions):
        raise UsageError(f"array task {index} is not in array map {digest} ({len(actions)} tasks)")
    return str(actions[index])


def _open_store(section: Mapping[str, object]) -> MetadataStore:
    """The HTTP client for an http(s) URL (cluster), PostgreSQL directly for development (R7)."""
    from ebs.meta.factory import open_metadata_store  # only when posting: keeps plain runs light

    return open_metadata_store(section)


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
