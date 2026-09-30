"""Tests for `ebs.flow.loader` (task P0-04 R1-R4, R7, R8)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ebs.core.errors import FlowError
from ebs.flow.loader import MAX_NODES, _allowed_keys, _unknown_key, load_flow
from ebs.flow.model import Flow, OutputDef

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "flows"
VALID = sorted((FIXTURES / "valid").glob("*.yaml"))
INVALID = sorted((FIXTURES / "invalid").glob("*.yaml"))
EXPECT_RE = re.compile(r"^# expect: (?P<text>.+) @ (?P<line>\d+):(?P<col>\d+)$")


def _expectation(path: Path) -> tuple[str, int, int]:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    m = EXPECT_RE.match(first)
    assert m, f"{path.name}: first line must be '# expect: <substring> @ line:col'"
    return m["text"], int(m["line"]), int(m["col"])


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "flow.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# R1, R2 (Done when: >= 12 invalid fixtures)
def test_fixture_counts() -> None:
    # "Done when": at least 12 invalid fixtures covering distinct error kinds.
    assert len(INVALID) >= 12
    assert any(p.name == "architecture_example.yaml" for p in VALID)


# R1
@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid_fixtures_load(path: Path) -> None:
    flow = load_flow(path)
    assert isinstance(flow, Flow)
    assert flow.version == 1
    # model_dump speaks the YAML dialect again (aliases, "8G", "30m"), so it re-validates.
    assert Flow.model_validate(flow.model_dump()) == flow


# R1, R5, R6
def test_architecture_example_contents() -> None:
    flow = load_flow(FIXTURES / "valid" / "architecture_example.yaml")
    assert flow.project == "rv32x-cpu"
    assert flow.domain == "cpu-nda"
    assert list(flow.steps) == ["compile", "elab", "sim", "lint_legacy"]
    assert flow.imports["rtl"].from_ == "rtl-team/cpu"
    assert flow.imports["rtl"].channel == "stable"
    assert flow.toolchains["questa"].licenses == {"msimhdlsim": 1}
    compile_ = flow.steps["compile"]
    assert compile_.kind == "questa.compile"
    assert compile_.matrix is not None
    assert compile_.matrix.table == "libs.csv"
    # `${…}` strings are kept verbatim (resolution is P0-05).
    assert compile_.inputs["srcs"] == "${imports.rtl}/**/*.sv"
    assert compile_.outputs["worklib"] == OutputDef(dir="work/${row.lib}", deterministic=False)
    assert compile_.resources.cpus == 2
    assert compile_.resources.mem == 8 * 1024**3
    assert compile_.resources.time == 30 * 60
    sim = flow.steps["sim"]
    assert sim.resources.time == "${row.timeout}"  # deferred
    assert sim.outputs["result"] == OutputDef(file="result.json")
    assert sim.outputs["cov"] == OutputDef(file="cov.ucdb", optional=True)
    assert sim.debug is not None
    assert sim.debug.collect == ("transcript", "*.log")
    assert sim.debug.max_size == 2 * 1024**3
    legacy = flow.steps["lint_legacy"]
    assert (legacy.kind, legacy.workdir, legacy.target) == ("make", "flows/lint", "lint")


# R2, R3, R4, R5, R6, R7, R8
@pytest.mark.parametrize("path", INVALID, ids=[p.stem for p in INVALID])
def test_invalid_fixtures(path: Path) -> None:
    text, line, col = _expectation(path)
    with pytest.raises(FlowError) as info:
        load_flow(path)
    err = info.value
    assert text in err.message, f"{text!r} not in {err.message!r}"
    assert (err.file, err.line, err.col) == (str(path), line, col), str(err)
    assert str(err).startswith(f"{path}:{line}:{col}: ")


# R2
def test_did_you_mean_nested(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nproject: p\ndomain: d\nsteps:\n  a:\n    kind: shell\n"
        "    outputs: { r: { file: x, determinstic: false } }\n",
    )
    with pytest.raises(FlowError, match=r"did you mean 'deterministic'\?") as info:
        load_flow(path)
    assert "steps.a.outputs.r.determinstic" in info.value.message


# R2
def test_all_errors_are_listed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nproject: p\ndomain: d\nsteps:\n  a:\n    kind: shell\n"
        "    resources: { mem: 1X, time: 1y }\n",
    )
    with pytest.raises(FlowError) as info:
        load_flow(path)
    assert (info.value.line, info.value.col) == (7, 23)  # the first error by position
    assert "invalid memory '1X'" in info.value.message
    assert "7:33: steps.a.resources.time: invalid time '1y'" in info.value.message


# R2
def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="cannot read flow file"):
        load_flow(tmp_path / "nope.yaml")


# R2
def test_top_level_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="must be a mapping") as info:
        load_flow(_write(tmp_path, "- a\n- b\n"))
    assert (info.value.line, info.value.col) == (1, 1)


# Contract: load_flow(path, *, lock=None)
def test_lock_argument_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "version: 1\nproject: p\ndomain: d\n")
    assert load_flow(path, lock=tmp_path / "flow.lock").steps == {}


# --- R8: anchors, aliases, merge keys, tags ----------------------------------------------------


# R8
def test_anchors_and_merge() -> None:
    flow = load_flow(FIXTURES / "valid" / "anchors_merge.yaml")
    a, b, c = flow.steps["a"], flow.steps["b"], flow.steps["c"]
    assert b.kind == "shell"  # merged from the anchor
    assert b.command == ("false",)  # explicit key overrides the merge
    assert (b.resources.cpus, b.resources.mem, b.resources.time) == (8, 512 * 1024**2, 300)
    assert c == a  # plain alias


# R8, R2
def test_merge_error_points_into_merged_map(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: 1\nproject: p\ndomain: d\nsteps:\n"
        "  a: &base\n    kind: shell\n    resources: { mem: 8Q }\n"
        "  b:\n    <<: *base\n    command: [x]\n",
    )
    with pytest.raises(FlowError, match="invalid memory") as info:
        load_flow(path)
    assert info.value.line == 7


# R8
@pytest.mark.parametrize(
    "value",
    [
        "!!python/object/apply:os.system ['id']",
        "!!python/name:os.system",
        "!!binary aGVsbG8=",
        "!!set { a, b }",
        "!!omap [ a: 1 ]",
        "!custom { a: 1 }",
        "!<tag:example.com,2026:x> y",
    ],
)
def test_unsafe_tag_rejected(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"version: 1\nproject: p\ndomain: d\nsteps:\n  a: {value}\n")
    with pytest.raises(FlowError, match=r"YAML tag .* is not allowed") as info:
        load_flow(path)
    assert (info.value.line, info.value.col) == (5, 6)


# R8
def test_standard_tags_allowed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "version: !!int 1\nproject: !!str p\ndomain: d\n"
        "steps: !!map\n  a:\n    kind: shell\n    params: { n: !!str 5, b: !!bool true }\n",
    )
    assert load_flow(path).steps["a"].params == {"n": "5", "b": True}


# R8
def test_yaml_12_booleans(tmp_path: Path) -> None:
    # YAML 1.2: `yes`/`on` are strings, not booleans (no Norway problem in params).
    path = _write(
        tmp_path,
        "version: 1\nproject: p\ndomain: d\nsteps:\n  a:\n    kind: shell\n"
        "    params: { country: NO, flag: yes, n: 0x10 }\n",
    )
    assert load_flow(path).steps["a"].params == {"country": "NO", "flag": "yes", "n": 16}


# --- YAML dialect edge cases -------------------------------------------------------------------

_HEAD = "version: 1\nproject: p\ndomain: d\nsteps:\n  a:\n"  # the step body starts on line 6


# R8
@pytest.mark.parametrize(
    ("body", "message", "line"),
    [
        ("    kind: shell\n    command: &c [x, *c]\n", "recursive YAML alias", 7),
        ("    kind: shell\n    params: { n: !!int abc }\n", "invalid integer 'abc'", 7),
        ("    <<: [1]\n    kind: shell\n", "'<<' merge key needs a mapping", 6),
        ("    <<: plain\n    kind: shell\n", "'<<' merge key needs a mapping", 6),
        ("    kind: shell\n    env: { ~: x }\n", "mapping keys must be strings, got null", 7),
        ("    kind: shell\n    env: { true: x }\n", "got bool true", 7),
        ("    kind: shell\n    env:\n      ? [a]\n      : x\n", "got a collection", 8),
    ],
)
def test_yaml_dialect_errors(tmp_path: Path, body: str, message: str, line: int) -> None:
    with pytest.raises(FlowError, match=re.escape(message)) as info:
        load_flow(_write(tmp_path, _HEAD + body))
    assert info.value.line == line


# R8
def test_integer_forms(tmp_path: Path) -> None:
    body = "    kind: shell\n    params: { a: 0o17, b: 0x1F, c: -5, d: +7, e: 1_000, f: 0b101 }\n"
    params = load_flow(_write(tmp_path, _HEAD + body)).steps["a"].params
    assert params == {"a": 15, "b": 31, "c": -5, "d": 7, "e": 1000, "f": 5}


# R8
def test_merge_list_precedence(tmp_path: Path) -> None:
    text = (
        "version: 1\nproject: p\ndomain: d\nsteps:\n"
        "  a: &a { kind: shell, workdir: from_a, target: from_a }\n"
        "  b: &b { kind: make, workdir: from_b }\n"
        "  c: { <<: [*a, *b], target: own }\n"
    )
    c = load_flow(_write(tmp_path, text)).steps["c"]
    # Explicit keys win; among merge sources the earlier one wins.
    assert (c.kind, c.workdir, c.target) == ("shell", "from_a", "own")


# R2
def test_syntax_error_without_context(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="YAML syntax error: mapping values are not allowed") as e:
        load_flow(_write(tmp_path, "version: 1\nproject: p: q\n"))
    assert (e.value.line, e.value.col) == (2, 11)


# R2
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("    kind: ~\n", "steps.a.kind: expected a string, got null"),
        ("    kind: true\n", "steps.a.kind: expected a string, got boolean true"),
        ("    kind: [x]\n", "steps.a.kind: expected a string, got a list"),
        ("    kind: { x: 1 }\n", "steps.a.kind: expected a string, got a mapping"),
        ("    kind: shell\n    env: [x]\n", "steps.a.env: expected a mapping, got a list"),
        ("    kind: shell\n    debug: 3\n", "steps.a.debug: expected a mapping, got integer 3"),
    ],
)
def test_type_errors_in_yaml_terms(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(FlowError, match=re.escape(message)) as info:
        load_flow(_write(tmp_path, _HEAD + body))
    assert info.value.line == 6 + body.count("\n") - 1


# R2
def test_pydantic_message_passthrough(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="project: String should match pattern") as info:
        load_flow(_write(tmp_path, "version: 1\nproject: Bad Name\ndomain: d\n"))
    assert (info.value.line, info.value.col) == (2, 10)


# R2
def test_unknown_key_in_optional_model(tmp_path: Path) -> None:
    body = "    kind: shell\n    matrix: { table: t.csv, tabel: u.csv }\n"
    with pytest.raises(FlowError, match=r"steps.a.matrix.tabel: .*did you mean 'table'\?") as e:
        load_flow(_write(tmp_path, _HEAD + body))
    assert (e.value.line, e.value.col) == (7, 29)


# R2, R8
@pytest.mark.parametrize(
    ("resources", "position"),
    [
        # The explicit key wins over the merged one, so the error points at it.
        ("{ mem: 1Q,\n      <<: { mem: 8G } }", (7, 23)),
        ("{ <<: { mem: 8G },\n      mem: 1Q }", (8, 12)),
        # Among merge sources the earlier one wins.
        ("{ <<: [{ mem: 1Q }, { mem: 8G }] }", (7, 30)),
        ("{ <<: [{ mem: 8G }, { mem: 1Q }], cpus: 1 }", None),
    ],
)
def test_merge_error_position_follows_winning_value(
    tmp_path: Path, resources: str, position: tuple[int, int] | None
) -> None:
    path = _write(tmp_path, _HEAD + f"    kind: shell\n    resources: {resources}\n")
    if position is None:
        assert load_flow(path).steps["a"].resources.mem == 8 * 1024**3
        return
    with pytest.raises(FlowError, match="invalid memory '1Q'") as info:
        load_flow(path)
    assert (info.value.line, info.value.col) == position


# R8
def test_duplicate_merge_key(tmp_path: Path) -> None:
    body = "    <<: { kind: shell }\n    <<: { kind: make }\n"
    with pytest.raises(FlowError, match="duplicate '<<' merge key") as info:
        load_flow(_write(tmp_path, _HEAD + body))
    assert (info.value.line, info.value.col) == (7, 5)


# R8
@pytest.mark.parametrize("version", ["1.1", "1.0"])
def test_yaml_11_directive_rejected(tmp_path: Path, version: str) -> None:
    # Under YAML 1.1, `yes` would silently become a boolean in params (and in action keys).
    text = f"%YAML {version}\n---\n" + _HEAD + "    kind: shell\n    params: { a: yes }\n"
    with pytest.raises(FlowError, match=f"unsupported '%YAML {version}' directive") as info:
        load_flow(_write(tmp_path, text))
    assert (info.value.line, info.value.col) == (1, 1)


# R8
def test_yaml_12_directive_allowed(tmp_path: Path) -> None:
    text = "%YAML 1.2\n---\n" + _HEAD + "    kind: shell\n    params: { a: yes }\n"
    assert load_flow(_write(tmp_path, text)).steps["a"].params == {"a": "yes"}


# R8
def test_alias_expansion_bounded(tmp_path: Path) -> None:
    levels = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 8):
        prev = f"*a{i - 1}"
        levels.append(f"a{i}: &a{i} [{', '.join([prev] * 10)}]")
    text = "version: 1\nproject: p\ndomain: d\nsteps:\n  s:\n    kind: shell\n    params:\n"
    text += "".join(f"      {line}\n" for line in levels)
    with pytest.raises(FlowError, match=f"more than {MAX_NODES} YAML nodes"):
        load_flow(_write(tmp_path, text))


# R6
def test_config_file_path_message(tmp_path: Path) -> None:
    body = "    kind: shell\n    config_files: { /etc/x.ini: y }\n"
    with pytest.raises(FlowError, match=r"config file path '/etc/x\.ini' must be relative"):
        load_flow(_write(tmp_path, _HEAD + body))


# R2
def test_allowed_keys_walk() -> None:
    assert "kind" in _allowed_keys(("steps", "a"))
    assert "module" in _allowed_keys(("toolchains", "q"))
    assert "deterministic" in _allowed_keys(("steps", "a", "outputs", "r"))
    assert _allowed_keys(("steps", "a", "nope")) == []
    assert _allowed_keys(("steps", "a", "kind")) == []
    assert _allowed_keys(("steps", "a", "command", 0)) == []
    assert _allowed_keys(("steps", "a", "command", 0, "x")) == []
    assert _unknown_key(("steps", "a", "kind", "x")) == "unknown key 'x'"
