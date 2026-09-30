"""Tests for `ebs.flow.model` (task P0-04 R4-R7)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from ebs.flow.model import (
    Flow,
    MatrixDef,
    OutputDef,
    Resources,
    StepDef,
    check_ref_syntax,
    format_duration,
    format_memory,
    parse_duration,
    parse_memory,
)

KIB, MIB, GIB, TIB = 1024, 1024**2, 1024**3, 1024**4


def _flow(**steps: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "project": "p", "domain": "d", "steps": steps}


# --- R5: resources -----------------------------------------------------------------------------


# R5
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8G", 8 * GIB),
        ("512M", 512 * MIB),
        ("1T", TIB),
        ("64K", 64 * KIB),
        ("256MiB", 256 * MIB),
        ("2GiB", 2 * GIB),
    ],
)
def test_memory_parse(text: str, expected: int) -> None:
    assert parse_memory(text) == expected
    assert Resources.model_validate({"mem": text}).mem == expected


# R5
@pytest.mark.parametrize(
    "text", ["8Q", "0G", "", "8", "4096", "-1G", "1.5G", "8 G", "G", "8g", "8GB", "08G", "8Gi"]
)
def test_memory_parse_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="invalid memory"):
        parse_memory(text)
    with pytest.raises(ValidationError, match="invalid memory"):
        Resources.model_validate({"mem": text})


# R5
@pytest.mark.parametrize("value", [4096, True, 1.5, ["8G"]])
def test_memory_non_string_rejected(value: object) -> None:
    with pytest.raises(ValidationError, match="invalid memory"):
        Resources.model_validate({"mem": value})
    with pytest.raises(ValidationError, match="invalid time"):
        Resources.model_validate({"time": value})


# R5
def test_resources_null_means_unset() -> None:
    assert Resources.model_validate({"mem": None, "time": None, "cpus": None}) == Resources()


# R5
def test_format_inverse() -> None:
    assert [format_memory(n) for n in (1024, 3 * MIB, 5 * GIB, 2 * TIB, 3072)] == [
        "1K",
        "3M",
        "5G",
        "2T",
        "3K",
    ]
    with pytest.raises(ValueError, match="not a multiple of 1K"):
        format_memory(1000)
    assert [format_duration(s) for s in (45, 120, 7200, 172800, 90)] == [
        "45s",
        "2m",
        "2h",
        "2d",
        "90s",
    ]


# R5
def test_resources_dump_round_trips() -> None:
    res = Resources.model_validate({"cpus": 2, "mem": "8GiB", "time": "90:00"})
    assert res.model_dump() == {"cpus": 2, "mem": "8G", "time": "90m"}
    assert Resources.model_validate(res.model_dump()) == res


# R5
@given(n=st.integers(1, 10**6), unit=st.sampled_from(["K", "M", "G", "T"]), iec=st.booleans())
def test_memory_parse_property(n: int, unit: str, iec: bool) -> None:
    power = "KMGT".index(unit) + 1
    assert parse_memory(f"{n}{unit}{'iB' if iec else ''}") == n * 1024**power


# R5
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", 1800),
        ("2h", 7200),
        ("45s", 45),
        ("1d", 86400),
        ("1-00:00:00", 86400),
        ("2-12", 2 * 86400 + 12 * 3600),
        ("1-02:30", 86400 + 2 * 3600 + 30 * 60),
        ("01:30:00", 5400),
        ("90:00", 5400),
        ("100:00:00", 100 * 3600),
    ],
)
def test_time_parse(text: str, expected: int) -> None:
    assert parse_duration(text) == expected
    assert Resources.model_validate({"time": text}).time == expected


# R5
@pytest.mark.parametrize(
    "text",
    [
        "90x",
        "0m",
        "",
        "30",
        "1.5h",
        "-1h",
        "1-",
        "01:60:00",
        "00:00:61",
        "1-24",
        "00:00:00",
        "0-00:00",
        "1h30m",
        "30 m",
        "30M",
        "a:b",
    ],
)
def test_time_parse_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="invalid time"):
        parse_duration(text)
    with pytest.raises(ValidationError, match="invalid time"):
        Resources.model_validate({"time": text})


# R5
@given(d=st.integers(0, 400), h=st.integers(0, 23), m=st.integers(0, 59), s=st.integers(0, 59))
def test_time_parse_property(d: int, h: int, m: int, s: int) -> None:
    total = ((d * 24 + h) * 60 + m) * 60 + s
    text = f"{d}-{h:02d}:{m:02d}:{s:02d}"
    if total == 0:
        with pytest.raises(ValueError, match="invalid time"):
            parse_duration(text)
    else:
        assert parse_duration(text) == total


# R5
@given(n=st.integers(1, 10**6), unit=st.sampled_from(["s", "m", "h", "d"]))
def test_time_suffix_property(n: int, unit: str) -> None:
    assert parse_duration(f"{n}{unit}") == n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# R5
@pytest.mark.parametrize("value", [1, 4, 128])
def test_cpus_valid(value: int) -> None:
    assert Resources.model_validate({"cpus": value}).cpus == value


# R5
@pytest.mark.parametrize("value", [0, -2, True, "4", 1.0, "four"])
def test_cpus_invalid(value: object) -> None:
    with pytest.raises(ValidationError, match="cpus must be a positive integer"):
        Resources.model_validate({"cpus": value})


# R5, R7
def test_resources_deferred() -> None:
    res = Resources.model_validate(
        {"cpus": "${row.cpus}", "mem": "${row.mem}", "time": "${row.timeout}"}
    )
    assert (res.cpus, res.mem, res.time) == ("${row.cpus}", "${row.mem}", "${row.timeout}")
    with pytest.raises(ValidationError, match="malformed reference"):
        Resources.model_validate({"time": "${row.}"})


# R5
def test_resources_default_empty() -> None:
    assert Resources() == Resources(cpus=None, mem=None, time=None)


# --- R6: outputs -------------------------------------------------------------------------------


# R6
def test_output_short_and_long_form() -> None:
    step = StepDef.model_validate(
        {
            "kind": "shell",
            "outputs": {
                "result": "result.json",
                "cov": {"file": "cov.ucdb", "optional": True},
                "lib": {"dir": "work/${row.lib}", "deterministic": False},
            },
        }
    )
    assert step.outputs["result"] == OutputDef(
        file="result.json", dir=None, deterministic=True, optional=False
    )
    assert step.outputs["cov"] == OutputDef(file="cov.ucdb", optional=True)
    assert step.outputs["lib"] == OutputDef(dir="work/${row.lib}", deterministic=False)
    assert step.outputs["lib"].file is None


# R6
@pytest.mark.parametrize(
    "value",
    [{"file": "a", "dir": "b"}, {"optional": True}, {}, {"file": None, "dir": None}],
)
def test_output_exactly_one_of_file_dir(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one of 'file' or 'dir'"):
        OutputDef.model_validate(value)


# R6
@pytest.mark.parametrize("value", [1, True, ["a"], None])
def test_output_bad_short_form(value: object) -> None:
    with pytest.raises(ValidationError):
        StepDef.model_validate({"kind": "shell", "outputs": {"r": value}})


# R6
@pytest.mark.parametrize("path", ["/abs", "../up", "a/../../b", "", "a/.."])
def test_output_path_must_stay_inside(path: str) -> None:
    with pytest.raises(ValidationError, match="must be relative"):
        OutputDef.model_validate({"file": path})


# R6, R2
@pytest.mark.parametrize("value", [{"file": "a", "deterministic": "no"}, {"dir": "a", "x": 1}])
def test_output_long_form_strict(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OutputDef.model_validate(value)


# --- R4: names ---------------------------------------------------------------------------------


# R4
@pytest.mark.parametrize("name", ["a", "sim", "lint_legacy", "a1", "a" * 63])
def test_valid_names(name: str) -> None:
    flow = Flow.model_validate(_flow(**{name: {"kind": "shell", "outputs": {name: "x"}}}))
    assert name in flow.steps
    assert name in flow.steps[name].outputs


# R4
@pytest.mark.parametrize("name", ["", "A", "Sim", "1a", "_a", "a-b", "a.b", "a" * 64, "é"])
def test_invalid_step_names(name: str) -> None:
    with pytest.raises(ValidationError, match="step name"):
        Flow.model_validate(_flow(**{name: {"kind": "shell"}}))


# R4
@pytest.mark.parametrize("name", ["", "A", "1a", "a-b", "a" * 64])
def test_invalid_output_names(name: str) -> None:
    with pytest.raises(ValidationError, match="output name"):
        StepDef.model_validate({"kind": "shell", "outputs": {name: "x"}})


# --- R7: `${…}` syntax -------------------------------------------------------------------------


# R7
@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        "$HOME and $1",
        "cost: $5",
        "$${literal}",
        "echo $${HOME}",
        "${row.x}",
        "${row.timeout_s}",
        "a${params.b}c${env.C_1}",
        "${imports.rtl}",
        "${imports.rtl}/**/*.sv",
        "${imports.rtl/**/*.sv}",
        "${steps.a.outputs.b}",
        "${steps.a.outputs.b[*]}",
        "${steps.a.outputs.b[seed=1]}",
        "${steps.a.outputs.b[seed=1,test=smoke-2]}",
        "${row.a}${row.b}",
    ],
)
def test_ref_syntax_valid(text: str) -> None:
    assert check_ref_syntax(text) is None


# R7
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("${", "unterminated reference"),
        ("${row.x", "unterminated reference"),
        ("ok ${row.x} then ${", "unterminated reference"),
        ("${}", "malformed reference '${}'"),
        ("${row}", "malformed reference '${row}'"),
        ("${row.}", "malformed reference '${row.}'"),
        ("${row.1x}", "malformed reference"),
        ("${HOME}", "write '$${' for a literal '${'"),
        ("${steps.a}", "malformed reference"),
        ("${steps.a.outputs}", "malformed reference"),
        ("${steps.a.outputs.b[]}", "malformed reference"),
        ("${steps.a.outputs.b[seed]}", "malformed reference"),
        ("${steps.a.outputs.b[seed=]}", "malformed reference"),
        ("${steps.a.outputs.b[*][*]}", "malformed reference"),
        ("${steps.A.outputs.b}", "malformed reference"),
        ("${imports.rtl/}", "malformed reference"),
        ("${ row.x }", "malformed reference"),
        ("${params.a.b}", "malformed reference"),
        ("${env.X-Y}", "malformed reference"),
        ("${row.x${row.y}}", "malformed reference"),
    ],
)
def test_ref_syntax_invalid(text: str, message: str) -> None:
    error = check_ref_syntax(text)
    assert error is not None
    assert message in error


# R7
@pytest.mark.parametrize(
    "step",
    [
        {"kind": "shell", "script": "${bad}"},
        {"kind": "shell", "command": ["ok", "${bad}"]},
        {"kind": "shell", "inputs": {"a": "${bad}"}},
        {"kind": "shell", "inputs": {"a": ["x", "${bad}"]}},
        {"kind": "shell", "params": {"a": "${bad}"}},
        {"kind": "shell", "env": {"A": "${bad}"}},
        {"kind": "shell", "outputs": {"a": "${bad}"}},
        {"kind": "shell", "outputs": {"a": {"dir": "${bad}"}}},
        {"kind": "shell", "config_files": {"a.ini": "${bad}"}},
        {"kind": "shell", "workdir": "${bad}"},
        {"kind": "make", "target": "${bad}"},
        {"kind": "shell", "debug": {"collect": ["${bad}"]}},
        {"kind": "shell", "matrix": {"table": "${bad}"}},
        {"kind": "shell", "licenses": {"a": "${bad}"}},
    ],
)
def test_ref_syntax_checked_in_every_template_field(step: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match=r"malformed reference|valid integer|positive"):
        StepDef.model_validate(step)


# --- misc model rules --------------------------------------------------------------------------


# R2 (extra='forbid', frozen=True)
def test_models_are_frozen_and_forbid_extra() -> None:
    flow = Flow.model_validate(_flow(a={"kind": "shell"}))
    with pytest.raises(ValidationError):
        flow.project = "other"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Flow.model_validate({**_flow(), "extra": 1})


# R3
@pytest.mark.parametrize("version", [0, 2, "1", True, None])
def test_version_must_be_1(version: object) -> None:
    with pytest.raises(ValidationError, match="supported versions: 1"):
        Flow.model_validate({**_flow(), "version": version})


# R2
def test_steps_default_empty() -> None:
    assert Flow.model_validate({"version": 1, "project": "p", "domain": "d"}).steps == {}


# R2
@pytest.mark.parametrize("value", [{"a": 1.5}, {"a": None}, {"a": [1]}, {"a": {"b": 1}}])
def test_params_scalar_only(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StepDef.model_validate({"kind": "shell", "params": value})


# R2
def test_params_types_kept() -> None:
    step = StepDef.model_validate({"kind": "shell", "params": {"s": "x", "i": 3, "b": False}})
    assert step.params == {"s": "x", "i": 3, "b": False}


# R2
def test_script_and_command_are_exclusive() -> None:
    # P0-12 checks per kind which one is required; both at once is always wrong.
    with pytest.raises(ValidationError, match="'script' and 'command' are mutually exclusive"):
        StepDef.model_validate({"kind": "shell", "script": "x", "command": ["y"]})


# R2
@pytest.mark.parametrize(
    "matrix",
    [{}, {"table": "a", "cross": ["b", "c"]}, {"cross": ["b", "c"], "zip": ["d", "e"]}],
)
def test_matrix_exactly_one_mode(matrix: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one of 'table', 'cross' or 'zip'"):
        MatrixDef.model_validate(matrix)


# R2
@pytest.mark.parametrize("mode", ["cross", "zip"])
def test_matrix_list_modes_need_two_tables(mode: str) -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        MatrixDef.model_validate({mode: ["a.csv"]})
    assert MatrixDef.model_validate({mode: ["a.csv", "b.csv"]}).tables == ("a.csv", "b.csv")


# R2
def test_matrix_tables() -> None:
    assert MatrixDef.model_validate({"table": "t.csv"}).tables == ("t.csv",)
    assert MatrixDef.model_validate({"cross": ["a", "b"]}).tables == ("a", "b")


# R2
def test_matrix_zip_alias() -> None:
    m = MatrixDef.model_validate({"zip": ["a.csv", "b.csv"], "id": ["x"], "filter": {"x": "1"}})
    assert m.zip_ == ("a.csv", "b.csv")
    assert m.id == ("x",)
    assert m.filter == {"x": "1"}


# R2
@pytest.mark.parametrize(
    "value",
    [{"from": "t/p"}, {"from": "t/p", "channel": "s", "version": "1"}],
)
def test_import_channel_xor_version(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one of 'channel' or 'version'"):
        Flow.model_validate({**_flow(), "imports": {"rtl": value}})


# R2
def test_unknown_toolchain() -> None:
    with pytest.raises(ValidationError, match="unknown toolchain 'q'; no toolchains are declared"):
        Flow.model_validate(_flow(a={"kind": "shell", "toolchain": "q"}))
    flow = {**_flow(a={"kind": "shell", "toolchain": "zzz"})}
    flow["toolchains"] = {"questa": {"module": "questa/1"}, "lint": {"module": "lint/2"}}
    with pytest.raises(ValidationError, match="declared toolchains: lint, questa"):
        Flow.model_validate(flow)
    flow["steps"] = {"a": {"kind": "shell", "toolchain": "lint"}}
    assert Flow.model_validate(flow).steps["a"].toolchain == "lint"


# R2
@pytest.mark.parametrize("kind", ["", "Shell", "questa.", ".sim", "questa..sim", "a b"])
def test_invalid_kind(kind: str) -> None:
    with pytest.raises(ValidationError, match="kind"):
        StepDef.model_validate({"kind": kind})


# R2
@pytest.mark.parametrize("name", ["1X", "A-B", "", "A B"])
def test_invalid_env_name(name: str) -> None:
    with pytest.raises(ValidationError, match="environment variable name"):
        StepDef.model_validate({"kind": "shell", "env": {name: "v"}})


# R2
@pytest.mark.parametrize("count", [0, -1, True])
def test_invalid_license_count(count: object) -> None:
    with pytest.raises(ValidationError):
        StepDef.model_validate({"kind": "shell", "licenses": {"feat": count}})
