"""The build loop: cache lookups, submission, polling, refinement, retries (P0-15).

The driver owns the DAG (architecture.md "SLURM execution…"): an action is looked up in the
action cache, and submitted on a miss, only once every producer has succeeded and its key is
known. Cache hits cost no executor call. Results arrive through `executor.poll` (state) and the
MetadataStore (`get_result`: the manifest the runner posted); their output ids feed
`Planner.refine`, which completes downstream keys (deterministic outputs give early cutoff).
Infrastructure failures are retried by a `RetryPolicy` and never cached (I12).

Action states follow `ebs.meta.api.TRANSITIONS`. `skipped` = never started, because a producer
did not succeed or the build stopped after a failure (no `keep_going`); `cancelled` = the user
interrupted the build.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol, TypeAlias

from ebs.cas.api import CAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, ExecutorError
from ebs.core.log import get_logger
from ebs.driver.cache_policy import CachePolicy
from ebs.driver.events import EventLog
from ebs.driver.retry import RetryPolicy
from ebs.exec.api import Executor, JobHandle, JobStatus, SubmitBatch
from ebs.meta.api import (
    ActionState,
    BuildId,
    CacheMode,
    InfraReason,
    MetadataStore,
    ResultManifest,
)
from ebs.plan.planfile import encode
from ebs.plan.types import ActionOutputInput, ActionSpec, Plan

_log = get_logger(__name__)

FinalState: TypeAlias = Literal["done", "failed", "infra_failed", "cached", "skipped", "cancelled"]

MAX_MISSED_POLLS = 10
"""Polls in a row that may omit a submitted job before the driver counts it as lost."""


@dataclass(frozen=True, slots=True)
class DriverConfig:
    cache_mode: CacheMode = "read"
    rerun_failed: bool = False
    keep_going: bool = False
    max_retries: int = 2  # used when no RetryPolicy is given
    max_batch: int = 1000  # actions per SubmitBatch (P1-02: one job array)
    poll_min_s: float = 0.5
    poll_max_s: float = 10.0

    def __post_init__(self) -> None:
        if self.max_batch < 1:
            raise ConfigError(f"max_batch must be at least 1, got {self.max_batch}")
        if not 0 < self.poll_min_s <= self.poll_max_s:
            raise ConfigError(
                f"poll interval must satisfy 0 < poll_min_s <= poll_max_s, got "
                f"{self.poll_min_s} and {self.poll_max_s}"
            )
        if self.max_retries < 0:
            raise ConfigError(f"max_retries must be >= 0, got {self.max_retries}")


class Refiner(Protocol):
    """`ebs.plan.planner.Planner` (only `refine` is needed)."""

    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan: ...


class Scheduler:
    """Runs one build's actions to a final state. Single-threaded; `request_cancel` is the only
    method that may be called from elsewhere (a signal handler)."""

    def __init__(
        self,
        plan: Plan,
        *,
        plan_digest: Digest,
        refiner: Refiner,
        cas: CAS,
        store: MetadataStore,
        executor: Executor,
        build: BuildId,
        events: EventLog,
        clock: Clock,
        config: DriverConfig,
        retry: RetryPolicy,
    ) -> None:
        self._plan = plan
        self._plan_digest: Digest | None = plan_digest  # None: refined since last stored
        self._refiner = refiner
        self._cas = cas
        self._store = store
        self._executor = executor
        self._build = build
        self._events = events
        self._clock = clock
        self._config = config
        self._policy = CachePolicy(config.cache_mode, config.rerun_failed)
        self._retry = retry
        self._order = [a.action_id for a in plan.actions]
        self._producers: dict[str, set[str]] = {a: set() for a in self._order}
        for producer, _, consumer in plan.edges:
            self._producers[consumer].add(producer)
        self._has_consumers = {producer for producer, _, _ in plan.edges}
        self._final: dict[str, FinalState] = {}
        self._ok: set[str] = set()  # succeeded: done, or cached with a passed result
        self.failures: set[str] = set()  # failed, cached failure, any final infra failure
        self.infra_exhausted: set[str] = set()  # infra failures whose retries were used up
        self._store_state: dict[str, ActionState] = {a: "queued" for a in self._order}
        self._inflight: dict[JobHandle, str] = {}
        self._last: dict[JobHandle, JobStatus] = {}
        self._attempts: dict[str, int] = {}  # failed attempts (infrastructure)
        self._retry_at: dict[str, float] = {}  # action -> monotonic time it may be resubmitted
        self._missed_polls: dict[JobHandle, int] = {}
        self._stopping = False
        self._cancel_requested = False
        self._wake = threading.Event()  # set by request_cancel: ends a real-clock wait early
        self.cancelled = False

    # --- public ---------------------------------------------------------------------------------

    @property
    def plan(self) -> Plan:
        """The plan as refined so far."""
        return self._plan

    def request_cancel(self) -> None:
        """Ask the loop to cancel submitted work and stop (safe from a signal handler)."""
        self._cancel_requested = True
        self._wake.set()

    def _cancelling(self) -> bool:
        # A method, not the attribute: a signal handler may set it between two checks.
        return self._cancel_requested

    def run(self) -> dict[str, FinalState]:
        """Drive every action to a final state; returns them in plan order."""
        interval = self._config.poll_min_s
        while True:
            if self._cancelling():
                self._cancel()
                break
            self._schedule()
            if not self._inflight:
                if len(self._final) == len(self._order):
                    break
                if not self._retry_at:
                    self._stuck()
                self._sleep(max(0.0, min(self._retry_at.values()) - self._clock.monotonic()))
                continue
            wait = interval
            if self._retry_at:
                wait = min(wait, max(0.0, min(self._retry_at.values()) - self._clock.monotonic()))
            self._sleep(wait)
            if self._cancelling():  # Ctrl-C while sleeping: cancel before polling
                continue
            changed = self._poll()
            interval = (
                self._config.poll_min_s if changed else min(interval * 2, self._config.poll_max_s)
            )
        return {a: self._final[a] for a in self._order}

    def _sleep(self, seconds: float) -> None:
        # Python signal handlers do not interrupt time.sleep (PEP 475): on the real clock wait on
        # an Event that request_cancel sets, so Ctrl-C acts at once even during a long backoff.
        if isinstance(self._clock, SystemClock):
            self._wake.wait(seconds)
        else:
            self._clock.sleep(seconds)

    def abort(self) -> None:
        """Best effort after an unexpected error: cancel whatever is still submitted."""
        if self._inflight:
            try:
                self._executor.cancel(list(self._inflight))
            except Exception:
                _log.warning("driver.abort_cancel_failed", jobs=len(self._inflight))

    # --- scheduling -----------------------------------------------------------------------------

    def _schedule(self) -> None:
        """Skip what cannot run, serve cache hits, submit ready misses."""
        busy = set(self._inflight.values())
        ready: list[ActionSpec] = []
        now = self._clock.monotonic()
        for action_id in self._order:
            if action_id in self._final or action_id in busy:
                continue
            producers = self._producers[action_id]
            if self._stopping and action_id in self._retry_at:
                self._abandon_retry(action_id)
                continue
            blocked = sorted(p for p in producers if p in self._final and p not in self._ok)
            if blocked or self._stopping:
                self._skip(action_id, blocked)
                continue
            if not producers <= self._ok:
                continue
            due = self._retry_at.get(action_id)
            if due is not None:
                if due <= now:
                    ready.append(self._plan.action(action_id))  # a retry never reads the cache
                continue
            spec = self._plan.action(action_id)
            if spec.key is None:
                self._missing_input(spec)
                continue
            if self._policy.reads and self._try_cache(spec, spec.key):
                continue
            ready.append(spec)
        if self._stopping:  # a failure found during this pass: start nothing new (R5)
            self._stop_sweep(busy)
            return
        for spec in ready:
            if self._retry_at.pop(spec.action_id, None) is not None:
                self._set_state(spec.action_id, "queued")  # infra_failed -> queued: the retry
        for batch in self._batches(ready):
            self._submit(batch)

    def _stop_sweep(self, busy: set[str]) -> None:
        """Finish everything not running: actions passed over or made ready earlier in the pass
        that found the failure would otherwise wait forever."""
        for action_id in self._order:
            if action_id in self._final or action_id in busy:
                continue
            if action_id in self._retry_at:
                self._abandon_retry(action_id)
            else:
                producers = self._producers[action_id]
                blocked = sorted(p for p in producers if p in self._final and p not in self._ok)
                self._skip(action_id, blocked)

    def _try_cache(self, spec: ActionSpec, key: Digest) -> bool:
        hit = self._store.cache_get(spec.domain, key)
        if hit is None or not self._policy.accept(hit):
            return False
        action_id = spec.action_id
        self._store.record_result(self._build, action_id, hit)  # per-build manifest + provenance
        self._set_state(action_id, "cached", key=key, result_key=hit.action_key)
        self._events.emit("cache_hit", action_id, key=str(key), status=hit.status)
        self._final[action_id] = "cached"
        if hit.status == "passed":
            self._succeeded(spec, hit)
        else:
            self._failed(action_id)
        return True

    def _batches(self, ready: list[ActionSpec]) -> Iterable[list[ActionSpec]]:
        """Same step, resources and licenses together (in plan order), at most max_batch each."""
        groups: dict[tuple[object, ...], list[ActionSpec]] = {}
        for spec in ready:
            group_key = (
                spec.step,
                spec.resources.model_dump_json(),
                tuple(sorted(spec.licenses.items())),
            )
            groups.setdefault(group_key, []).append(spec)
        size = self._config.max_batch
        for specs in groups.values():
            for start in range(0, len(specs), size):
                yield specs[start : start + size]

    def _submit(self, specs: list[ActionSpec]) -> None:
        batch = SubmitBatch(
            plan=self._stored_plan(),
            domain=self._plan.domain,
            build=self._build,
            actions=tuple(specs),
        )
        try:
            handles = self._executor.submit(batch)
        except ExecutorError as exc:
            _log.warning("driver.submit_failed", step=batch.step, error=str(exc))
            for spec in specs:
                self._infra_failed(spec.action_id, "other", detail=f"submit failed: {exc}")
            return
        if len(handles) != len(specs):
            if handles:
                self._executor.cancel(handles)  # do not leak what it did start
            raise ExecutorError(
                f"executor {self._executor.name!r} returned {len(handles)} handles for "
                f"{len(specs)} actions; it must return one handle per action, in order"
            )
        for spec, handle in zip(specs, handles, strict=True):
            self._inflight[handle] = spec.action_id
            self._events.emit(
                "submitted",
                spec.action_id,
                job_id=handle.job_id,
                attempt=self._attempts.get(spec.action_id, 0) + 1,
                batch_size=len(specs),
            )

    def _stored_plan(self) -> Digest:
        """The digest of the current (refined) plan, stored in the CAS for the runners."""
        if self._plan_digest is None:
            self._plan_digest = self._cas.put_bytes(encode(self._plan))
        return self._plan_digest

    # --- polling --------------------------------------------------------------------------------

    def _poll(self) -> bool:
        handles = list(self._inflight)
        statuses = self._executor.poll(handles)
        changed = False
        for handle in handles:
            status = statuses.get(handle)
            if status is None:
                status = self._missing_status(handle)
                if status is None:
                    continue
            if status == self._last.get(handle):
                continue
            changed = True
            self._last[handle] = status
            self._apply(handle, status)
        return changed

    def _missing_status(self, handle: JobHandle) -> JobStatus | None:
        """An executor that keeps omitting a job has lost it: an infrastructure failure."""
        misses = self._missed_polls[handle] = self._missed_polls.get(handle, 0) + 1
        if misses < MAX_MISSED_POLLS:
            return None
        _log.warning("driver.job_lost", job=handle.job_id, action=handle.action_id)
        return JobStatus(state="infra_failed", infra_reason="other")

    def _apply(self, handle: JobHandle, status: JobStatus) -> None:
        action_id = self._inflight[handle]
        key = self._plan.action(action_id).key
        if status.state == "pending":
            self._set_state(action_id, "pending", key=key, pending_reason=status.pending_reason)
            self._events.emit("pending", action_id, reason=status.pending_reason or "other")
        elif status.state == "running":
            self._ensure_running(action_id, key)
            self._events.emit("running", action_id, job_id=handle.job_id)
        elif status.state == "done":
            del self._inflight[handle]
            self._done(action_id, key, status)
        elif status.state == "infra_failed":
            del self._inflight[handle]
            self._infra_failed(
                action_id, status.infra_reason or "other", exit_code=status.exit_code
            )
        else:  # cancelled without our asking (e.g. by an admin): an infrastructure failure
            del self._inflight[handle]
            self._infra_failed(action_id, "other", detail="job cancelled outside ebs")

    def _done(self, action_id: str, key: Digest | None, status: JobStatus) -> None:
        result = self._store.get_result(self._build, action_id)
        if result is None:
            self._infra_failed(
                action_id, "runner_crash", detail="the job ended but no result was posted"
            )
            return
        self._ensure_running(action_id, key)
        state: FinalState = "done" if result.status == "passed" else "failed"
        self._set_state(action_id, state, result_key=result.action_key)
        self._final[action_id] = state
        self._events.emit(
            "finished",
            action_id,
            state=state,
            exit_code=result.exit_code,
            job_exit=status.exit_code,
        )
        if state == "done":
            self._succeeded(self._plan.action(action_id), result)
        else:
            self._failed(action_id)

    def _infra_failed(
        self,
        action_id: str,
        reason: InfraReason,
        *,
        exit_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self._set_state(action_id, "infra_failed", infra_reason=reason)
        data: dict[str, object] = {"reason": reason}
        if exit_code is not None:
            data["exit_code"] = exit_code
        if detail is not None:
            data["detail"] = detail[:1000]
        self._events.emit("infra_failed", action_id, **data)
        attempt = self._attempts[action_id] = self._attempts.get(action_id, 0) + 1
        spec = self._plan.action(action_id)
        decision = None if self._stopping else self._retry.decide(spec, attempt, reason)
        if decision is None:
            if not self._stopping:
                self.infra_exhausted.add(action_id)  # retries really used up: build exits 3
            self._final[action_id] = "infra_failed"
            self._events.emit("finished", action_id, state="infra_failed", attempts=attempt)
            self._failed(action_id)
            return
        # The action stays infra_failed until it is resubmitted (or abandoned if the build stops).
        if decision.resources is not None and decision.resources != spec.resources:
            self._replace(dataclasses.replace(spec, resources=decision.resources))
        self._retry_at[action_id] = self._clock.monotonic() + decision.delay_s
        self._events.emit(
            "retrying", action_id, attempt=attempt + 1, delay_ms=round(decision.delay_s * 1000)
        )

    # --- outcomes -------------------------------------------------------------------------------

    def _succeeded(self, spec: ActionSpec, result: ResultManifest) -> None:
        self._ok.add(spec.action_id)
        declared = {o.name for o in spec.outputs}
        produced = {
            (spec.action_id, name): out.id
            for name, out in result.outputs.items()
            if name in declared
        }
        if produced and spec.action_id in self._has_consumers:  # else nothing to refine
            self._plan = self._refiner.refine(self._plan, produced)
            self._plan_digest = None

    def _failed(self, action_id: str) -> None:
        self.failures.add(action_id)
        if not self._config.keep_going and not self._stopping:
            self._stopping = True
            _log.info("driver.stopping", after=action_id, running=len(self._inflight))

    def _abandon_retry(self, action_id: str) -> None:
        """The build stopped while this action waited for a retry: its last attempt stands."""
        del self._retry_at[action_id]
        self._final[action_id] = "infra_failed"
        self.failures.add(action_id)
        self._events.emit(
            "finished",
            action_id,
            state="infra_failed",
            attempts=self._attempts[action_id],
            reason="retry abandoned: build stopped",
        )

    def _skip(self, action_id: str, blocked_by: list[str]) -> None:
        self._set_state(action_id, "skipped")
        self._final[action_id] = "skipped"
        self._retry_at.pop(action_id, None)
        reason = f"blocked by {', '.join(blocked_by)}" if blocked_by else "build stopped"
        self._events.emit("finished", action_id, state="skipped", reason=reason)

    def _missing_input(self, spec: ActionSpec) -> None:
        """Every producer succeeded, yet an input id is unknown: an optional output is missing."""
        missing = [
            f"{r.source.action_id}.{r.source.output}"
            for r in spec.inputs
            if r.id is None and isinstance(r.source, ActionOutputInput)
        ]
        self._set_state(spec.action_id, "skipped")
        self._final[spec.action_id] = "skipped"
        self._events.emit(
            "finished",
            spec.action_id,
            state="skipped",
            reason=f"inputs not produced: {', '.join(missing)}; make the producer write them "
            "or do not consume an optional output",
        )
        self._failed(spec.action_id)

    def _replace(self, spec: ActionSpec) -> None:
        self._plan = dataclasses.replace(
            self._plan,
            actions=tuple(spec if a.action_id == spec.action_id else a for a in self._plan.actions),
        )
        self._plan_digest = None

    # --- cancellation ---------------------------------------------------------------------------

    def _cancel(self) -> None:
        self.cancelled = True
        handles = list(self._inflight)
        if handles:
            self._executor.cancel(handles)
            final = self._executor.poll(handles)  # jobs that finished meanwhile keep their result
            for handle in handles:
                status = final.get(handle)
                if status is not None and status.state == "done":
                    action_id = self._inflight.pop(handle)
                    self._done(action_id, self._plan.action(action_id).key, status)
        for action_id in self._order:
            if action_id not in self._final:
                if self._store_state[action_id] == "infra_failed":  # waiting for a retry
                    self._set_state(action_id, "queued")
                self._set_state(action_id, "cancelled")
                self._final[action_id] = "cancelled"
                self._events.emit("finished", action_id, state="cancelled")
        self._inflight.clear()
        self._retry_at.clear()

    # --- store bookkeeping ----------------------------------------------------------------------

    def _ensure_running(self, action_id: str, key: Digest | None) -> None:
        if self._store_state[action_id] in {"queued", "pending"}:
            self._set_state(action_id, "running", key=key)

    def _set_state(self, action_id: str, state: ActionState, **fields: object) -> None:
        clean = {k: v for k, v in fields.items() if v is not None}
        self._store.set_action_state(self._build, action_id, state, **clean)
        self._store_state[action_id] = state

    def _stuck(self) -> NoReturn:
        waiting = [a for a in self._order if a not in self._final]
        raise RuntimeError(  # a driver bug (exit 4), not a flow error
            f"internal error: the driver cannot make progress on {waiting[:10]}; their producers "
            "finished but they are still not ready. Please report this with the build's plan.json"
        )
