"""Output ids and the ResultManifest the runner posts (interfaces.md § 6)."""

from __future__ import annotations

from collections.abc import Mapping

from ebs.core.digest import Digest
from ebs.meta.api import OutputResult, ResourceUsage, ResultManifest, RunnerInfo
from ebs.plan.keys import nondeterministic_output_id
from ebs.plan.types import ActionSpec, OutputSpec
from ebs.runner.run import ToolRun

__all__ = ["make_manifest", "output_id", "resource_usage"]


def output_id(out: OutputSpec, content: Digest, key: Digest) -> Digest:
    """The id passed downstream (I14): the content digest, or for a `deterministic: false`
    output the id derived from the producer's action key, so new bytes change no consumer key.
    """
    return content if out.deterministic else nondeterministic_output_id(key, out.name)


def resource_usage(run: ToolRun) -> ResourceUsage:
    """Measured usage in whole units (canonical JSON has no floats)."""
    return ResourceUsage(
        max_rss_kb=max(0, run.max_rss_kb),
        cpu_s=max(0, round(run.cpu_s)),
        wall_s=max(0, round(run.wall_s)),
    )


def make_manifest(
    spec: ActionSpec,
    *,
    key: Digest,
    passed: bool,
    run: ToolRun,
    outputs: Mapping[str, OutputResult],
    log: Digest | None,
    summary: Mapping[str, str | int],
    runner: RunnerInfo,
) -> ResultManifest:
    inputs: dict[str, Digest] = {}
    for ref in spec.inputs:
        assert ref.id is not None  # resolve_contents refused unrefined inputs
        inputs[ref.logical_path] = ref.id
    return ResultManifest(
        action_key=key,
        status="passed" if passed else "failed",
        exit_code=run.exit_code,
        inputs=inputs,
        outputs=dict(outputs),
        log=log,
        summary=dict(summary),
        resources=resource_usage(run),
        runner=runner,
    )
