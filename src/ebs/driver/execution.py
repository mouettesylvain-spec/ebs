"""Pilot or direct submission, chosen per step (P3-06 R4).

A step says `resources.execution: auto | direct | pilot` (unset means `auto`). `auto` sends its
actions to the pilot pool when they usually run shorter than `[pilot].short_s` (the median wall
time of the step's last successful runs in this work dir's usage log, given at least
`MIN_SAMPLES` of them), or when a batch has more than `[pilot].threshold` actions; otherwise one
job per action (or a job array) is cheaper to schedule and needs no queue.

`RoutingExecutor` is the executor the scheduler sees: it routes each batch to the direct or the
pilot executor, keeps the choice of a step for its later batches (retries), and sends polls and
cancels to whichever executor handed the handle out. It carries the direct executor's name, so a
resumed driver re-attaches the direct jobs it recorded (`adopt`); pilot actions are not recorded
with a job id and run again after a resume.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from ebs.core.errors import ExecutorError
from ebs.core.log import get_logger
from ebs.driver.resources_tuning import MIN_SAMPLES
from ebs.exec.api import Executor, JobHandle, JobStatus, SubmitBatch
from ebs.flow.model import Execution
from ebs.meta.api import BuildId

__all__ = ["Route", "RoutingExecutor", "choose_execution", "expected_runtime_s"]

_log = get_logger(__name__)

Route = Literal["direct", "pilot"]


def expected_runtime_s(wall_s: Sequence[int]) -> int | None:
    """The (upper) median of `wall_s`, or None with fewer than `MIN_SAMPLES` runs."""
    if len(wall_s) < MIN_SAMPLES:
        return None
    return sorted(wall_s)[len(wall_s) // 2]


def choose_execution(
    mode: Execution,
    *,
    count: int,
    expected_runtime_s: int | None,
    threshold: int,
    short_s: int,
) -> Route:
    """Where a step's batch of `count` actions goes (see the module docstring)."""
    if mode != "auto":
        return mode
    if count > threshold:
        return "pilot"
    if expected_runtime_s is not None and expected_runtime_s < short_s:
        return "pilot"
    return "direct"


class RoutingExecutor:
    """Sends each batch to `direct` or `pilot` (`choose_execution`); one choice per step."""

    def __init__(
        self,
        direct: Executor,
        pilot: Executor,
        *,
        history: Mapping[str, Sequence[int]],
        threshold: int,
        short_s: int,
    ) -> None:
        if direct.name == pilot.name:
            raise ExecutorError(f"the direct and pilot executors are both named {direct.name!r}")
        self.name = direct.name
        self.direct, self.pilot = direct, pilot
        self._by_name: dict[str, Executor] = {direct.name: direct, pilot.name: pilot}
        self._routes: dict[Route, Executor] = {"direct": direct, "pilot": pilot}
        self._history = history
        self._threshold = threshold
        self._short_s = short_s
        self._chosen: dict[str, Route] = {}

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        route = self._chosen.get(batch.step)
        if route is None:
            mode = batch.actions[0].resources.execution or "auto"
            runtime = expected_runtime_s(self._history.get(batch.step, ()))
            route = choose_execution(
                mode,
                count=len(batch.actions),
                expected_runtime_s=runtime,
                threshold=self._threshold,
                short_s=self._short_s,
            )
            self._chosen[batch.step] = route
            _log.info(
                "driver.execution", step=batch.step, mode=mode, route=route, runtime_s=runtime
            )
        return self._routes[route].submit(batch)

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        out: dict[JobHandle, JobStatus] = {}
        for executor, group in self._groups(handles):
            out.update(executor.poll(group))
        return out

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        for executor, group in self._groups(handles):
            executor.cancel(group)

    def adopt(self, build: BuildId, handles: Sequence[JobHandle]) -> None:
        """Re-attach jobs of a previous driver (P2-08); only the direct executor records them."""
        direct = self._routes["direct"]
        adopt = getattr(direct, "adopt", None)
        if adopt is None:
            raise ExecutorError(f"executor {direct.name!r} cannot re-attach jobs")
        adopt(build, handles)

    def _groups(self, handles: Sequence[JobHandle]) -> list[tuple[Executor, list[JobHandle]]]:
        groups: dict[str, list[JobHandle]] = {}
        for h in handles:
            if h.executor not in self._by_name:
                raise ExecutorError(
                    f"handle {h} names unknown executor {h.executor!r}; expected one of "
                    f"{sorted(self._by_name)}"
                )
            groups.setdefault(h.executor, []).append(h)
        return [(self._by_name[name], group) for name, group in groups.items()]
