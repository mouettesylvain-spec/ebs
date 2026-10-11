"""The runner calls `RulePlugin.classify` once, after the tool exits and before it collects the
outputs, with the outputs that exist; a rule may finalize an output there (interfaces.md § 7).
`questa.sim` relies on this to turn its stub `result.json` into the final document (P1-08)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.plan.types import OutputSpec
from ebs.rules.api import PASSED, Classification
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import ShellRule
from ebs.runner.main import EXIT_OK, Runner
from tests.helpers.runner import HOST, Harness, settings, shell_spec


class FinalizingShell(ShellRule):
    """`shell`, but classify rewrites the `r` output and records what it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, str]] = []

    def classify(
        self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]
    ) -> Classification:
        self.calls.append({name: path.read_text() for name, path in outputs.items()})
        outputs["r"].write_text("final\n")
        return PASSED


@dataclass
class FinalizingHarness(Harness):
    rule: FinalizingShell | None = None

    def runner(self) -> Runner:
        assert self.rule is not None
        return Runner(
            cas=self.cas,
            store=self.store,
            rules=RuleRegistry([self.rule]),
            settings=settings(self.tmp_path, **self.settings_overrides),
            caller_env=self.caller_env,
            host=HOST,
            clock=FakeClock(),
            out=self.out,
            err=self.err,
        )


# P1-08 R3 (questa.sim's result.json depends on this order)
def test_classify_runs_once_before_outputs_are_collected(cas: FsCAS, tmp_path: Path) -> None:
    rule = FinalizingShell()
    h = FinalizingHarness(tmp_path, cas, rule=rule)
    spec = shell_spec(
        "echo stub > r.txt",
        outputs=[
            OutputSpec("r", "r.txt", "file", deterministic=True),
            OutputSpec("o", "o.txt", "file", deterministic=True, optional=True),
        ],
    )
    code, build = h.run(spec)
    assert code == EXIT_OK
    assert rule.calls == [{"r": "stub\n"}]  # once, with the existing outputs only
    result = h.result(build)
    assert result is not None
    with cas.open(result.outputs["r"].digest) as f:
        assert f.read() == b"final\n"  # what classify wrote is what was collected
