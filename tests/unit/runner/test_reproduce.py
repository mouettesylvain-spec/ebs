"""`ebs.runner.reproduce` details the CLI tests do not reach (P1-10 R2, R5)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.flow.model import Resources
from ebs.plan.types import ActionOutputInput, FlowInfo, InputRef, OutputSpec, Plan
from ebs.runner.errors import UsageError
from ebs.runner.reproduce import MissingInputsError, build_sandbox, find_missing, run_sandbox
from tests.helpers.runner import DOMAIN, settings, shell_spec, source_file


def _plan(*specs: object) -> Plan:
    return Plan(
        ebs_version="test", domain=DOMAIN, project="demo", flow=FlowInfo(), toolchains={},
        actions=tuple(specs), edges=(),  # type: ignore[arg-type]
    )  # fmt: skip


# R5: an input whose producer never ran is reported with the others, in user terms.
def test_unknown_input_ids_are_missing(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", DOMAIN)
    gen = shell_spec(
        "echo x > out.txt\n", action_id="gen", outputs=[OutputSpec("out", "out.txt", "file")]
    )
    src = source_file(cas, "a.txt", b"a\n")
    use = shell_spec(
        "cat out.txt\n",
        action_id="use",
        inputs=[src, InputRef("out.txt", "file", ActionOutputInput("gen", "out"), None)],
    )
    plan = _plan(gen, use)
    contents, missing = find_missing(plan, use, cas, None)
    assert contents == {"a.txt": src.id}
    assert [(m.logical_path, m.id) for m in missing] == [("out.txt", None)]
    assert "never ran" in missing[0].reason
    with pytest.raises(MissingInputsError, match="id unknown"):
        build_sandbox(
            plan, use, cas=cas, store=None, settings=settings(tmp_path), caller_env={},
            dest=tmp_path / "d",
        )  # fmt: skip
    assert not (tmp_path / "d").exists()


# R2: like the runner, an unexpanded time limit is refused rather than run without a timeout.
def test_unexpanded_time_refused(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", DOMAIN)
    spec = shell_spec("true\n")
    plan = _plan(spec)
    sandbox = build_sandbox(
        plan, spec, cas=cas, store=None, settings=settings(tmp_path), caller_env={},
        dest=tmp_path / "d",
    )  # fmt: skip
    odd = dataclasses.replace(spec, resources=Resources.model_construct(time="${row.timeout}"))
    with pytest.raises(UsageError, match="unexpanded time limit"):
        run_sandbox(odd, sandbox, settings(tmp_path))
