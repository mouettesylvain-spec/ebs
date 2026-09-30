"""Flow model: validated, immutable Pydantic models of `flow.yaml` (docs/design/interfaces.md § 3).

The models describe the YAML exactly as written: `${…}` references are kept verbatim (their syntax
is checked here, resolution is `ebs.flow.interp`, P0-05) and resource literals are parsed into
typed values (bytes, seconds, cpu count). Resource strings holding a `${…}` reference are kept as
strings and parsed once the reference is resolved (`parse_memory`, `parse_duration`).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from typing import Annotated, Any, NoReturn

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

__all__ = [
    "NAME_PATTERN",
    "SUPPORTED_VERSIONS",
    "DebugDef",
    "Flow",
    "ImportDef",
    "MatrixDef",
    "OutputDef",
    "Resources",
    "StepDef",
    "ToolchainRef",
    "check_ref_syntax",
    "format_duration",
    "format_memory",
    "parse_duration",
    "parse_memory",
]

SUPPORTED_VERSIONS = (1,)

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
"""Step and output names (R4): lowercase letter, then up to 62 lowercase letters, digits or `_`."""

IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
"""Names referenced as `${params.X}`, `${env.X}`, `${imports.X}`: inputs, params, env, imports."""

KIND_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
PROJECT_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
LICENSE_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$"
IMPORT_FROM_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$"

_NAME_RE = re.compile(NAME_PATTERN, re.ASCII)
_IDENT_RE = re.compile(IDENT_PATTERN, re.ASCII)
_KIND_RE = re.compile(KIND_PATTERN, re.ASCII)

_MAX_JSON_INT = 2**53 - 1  # canonical JSON range (interfaces.md § 1)


def _fail(error_type: str, detail: str, **ctx: object) -> NoReturn:
    # The message goes through the context so braces in it (`${…}`) are never format fields.
    raise PydanticCustomError(error_type, "{detail}", {"detail": detail, **ctx})


# --- `${…}` reference syntax (R7) --------------------------------------------------------------

_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_NAME = r"[a-z][a-z0-9_]{0,62}"
_VALUE = r"[^,\[\]{}$\s=]+"
_ROWSEL = rf"{_ID}={_VALUE}(?:,{_ID}={_VALUE})*"
_REF_BODY_RE = re.compile(
    rf"""
      row\.{_ID}
    | params\.{_ID}
    | env\.{_ID}
    | imports\.{_ID}(?:/[^\s{{}}$]+)?
    | steps\.{_NAME}\.outputs\.{_NAME}(?:\[\*\]|\[{_ROWSEL}\])?
    """,
    re.VERBOSE | re.ASCII,
)
_REF_FORMS = "${row.X}, ${params.X}, ${env.X}, ${imports.X[/glob]} or ${steps.S.outputs.O[*|k=v,…]}"


def check_ref_syntax(text: str) -> str | None:
    """Return why `text` holds a malformed `${…}` reference, or None if every reference parses.

    The string is scanned left to right: `$${` is an escaped literal `${`, and every other `${`
    opens a reference that must be closed by the next `}` and match the grammar in
    interfaces.md § 3. References are not resolved.
    """
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$${", i):
            i += 3
        elif text.startswith("${", i):
            end = text.find("}", i + 2)
            if end < 0:
                return (
                    f"unterminated reference at offset {i} in {text!r}: close it with '}}', "
                    "or write '$${' for a literal '${'"
                )
            if not _REF_BODY_RE.fullmatch(text, i + 2, end):
                return (
                    f"malformed reference {text[i : end + 1]!r}: expected {_REF_FORMS}; "
                    "write '$${' for a literal '${' (e.g. a shell variable)"
                )
            i = end + 1
        else:
            i += 1
    return None


def _check_template(value: str) -> str:
    error = check_ref_syntax(value)
    if error is not None:
        _fail("flow_ref", error)
    return value


Template = Annotated[StrictStr, AfterValidator(_check_template)]
"""A string that may hold `${…}` references (syntax-checked, kept verbatim)."""


def _is_deferred(value: str) -> bool:
    return "${" in value.replace("$${", "")


def _rel_path_check(what: str) -> Callable[[str], str]:
    # Deferred parts (`${row.x}`) are checked again after interpolation (P0-05).
    def check(value: str) -> str:
        parts = value.split("/")
        if value == "" or value.startswith("/") or ".." in parts:
            _fail(
                "flow_path",
                f"{what} {value!r} must be relative and stay inside the action directory "
                "(no leading '/', no '..')",
            )
        return value

    return check


RelPath = Annotated[Template, AfterValidator(_rel_path_check("output path"))]
ConfigFilePath = Annotated[Template, AfterValidator(_rel_path_check("config file path"))]


def _pattern_check(what: str, regex: re.Pattern[str], hint: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not regex.fullmatch(value):
            _fail("flow_name", f"{what} {value!r} must match {regex.pattern} ({hint})")
        return value

    return check


StepName = Annotated[
    StrictStr,
    AfterValidator(_pattern_check("step name", _NAME_RE, "lowercase letter, then a-z, 0-9 or '_'")),
    WithJsonSchema({"type": "string", "pattern": NAME_PATTERN}),
]
OutputName = Annotated[
    StrictStr,
    AfterValidator(
        _pattern_check("output name", _NAME_RE, "lowercase letter, then a-z, 0-9 or '_'")
    ),
    WithJsonSchema({"type": "string", "pattern": NAME_PATTERN}),
]
Ident = Annotated[
    StrictStr,
    AfterValidator(_pattern_check("name", _IDENT_RE, "letter or '_', then letters, digits, '_'")),
    WithJsonSchema({"type": "string", "pattern": IDENT_PATTERN}),
]
EnvName = Annotated[
    StrictStr,
    AfterValidator(
        _pattern_check(
            "environment variable name", _IDENT_RE, "letter or '_', then letters, digits, '_'"
        )
    ),
    WithJsonSchema({"type": "string", "pattern": IDENT_PATTERN}),
]
Kind = Annotated[
    StrictStr,
    AfterValidator(
        _pattern_check("kind", _KIND_RE, "dotted lowercase identifiers, e.g. 'questa.sim'")
    ),
    WithJsonSchema({"type": "string", "pattern": KIND_PATTERN}),
]
ProjectName = Annotated[StrictStr, StringConstraints(pattern=PROJECT_PATTERN)]
LicenseName = Annotated[StrictStr, StringConstraints(pattern=LICENSE_PATTERN)]
LicenseCount = Annotated[StrictInt, Field(gt=0)]
ParamInt = Annotated[StrictInt, Field(ge=-_MAX_JSON_INT, le=_MAX_JSON_INT)]


# --- resources (R5) ----------------------------------------------------------------------------

_MEMORY_RE = re.compile(r"([1-9][0-9]*)([KMGT])(iB)?", re.ASCII)
_MEMORY_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_DURATION_SUFFIX_RE = re.compile(r"([0-9]+)([smhd])", re.ASCII)
_DURATION_SLURM_RE = re.compile(r"(?:([0-9]+)-)?([0-9]+)(?::([0-9]{2}))?(?::([0-9]{2}))?", re.ASCII)
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

MEMORY_PATTERN = r"^[1-9][0-9]*[KMGT](iB)?$"
DURATION_PATTERN = r"^([0-9]+[smhd]|[0-9]+-[0-9]{1,2}(:[0-9]{2}){0,2}|[0-9]+(:[0-9]{2}){1,2})$"
_DEFERRED_PATTERN = r"\$\{"


def parse_memory(text: str) -> int:
    """Parse `8G`, `512M`, `1T`, `64K` (binary units; `iB` suffix optional) into bytes.

    Bare numbers are rejected: SLURM would read them as MiB, which is easy to get wrong.
    """
    m = _MEMORY_RE.fullmatch(text)
    if m is None:
        raise ValueError(
            f"invalid memory {text!r}: expected a positive integer with a binary unit "
            "K, M, G or T (e.g. 512M, 8G, 1T; 8GiB is also accepted)"
        )
    return int(m[1]) * _MEMORY_UNITS[m[2]]


def format_memory(nbytes: int) -> str:
    """Inverse of `parse_memory` for the values it produces (multiples of 1 KiB)."""
    for unit in ("T", "G", "M", "K"):
        if nbytes % _MEMORY_UNITS[unit] == 0:
            return f"{nbytes // _MEMORY_UNITS[unit]}{unit}"
    raise ValueError(f"memory {nbytes} bytes is not a multiple of 1K")


def parse_duration(text: str) -> int:
    """Parse a wall time into seconds.

    Accepted: `45s`, `30m`, `2h`, `1d`, and the SLURM forms `MM:SS`, `HH:MM:SS`, `D-HH`,
    `D-HH:MM`, `D-HH:MM:SS`. Bare numbers (SLURM minutes) are rejected as ambiguous, and so are
    zero durations and out-of-range minute/second/hour fields.
    """
    total = _duration_seconds(text)
    if total is None or total <= 0:
        raise ValueError(
            f"invalid time {text!r}: expected a positive duration such as 45s, 30m, 2h, 1d, "
            "MM:SS, HH:MM:SS or D-HH[:MM[:SS]]"
        )
    return total


def _duration_seconds(text: str) -> int | None:
    m = _DURATION_SUFFIX_RE.fullmatch(text)
    if m is not None:
        return int(m[1]) * _DURATION_UNITS[m[2]]
    m = _DURATION_SLURM_RE.fullmatch(text)
    if m is None:
        return None
    days, first, second, third = m.groups()
    rest = [int(v) for v in (second, third) if v is not None]
    if any(v >= 60 for v in rest):
        return None
    if days is not None:
        hours, minutes, seconds = int(first), *[*rest, 0, 0][:2]
        if hours >= 24:
            return None
        return ((int(days) * 24 + hours) * 60 + minutes) * 60 + seconds
    if not rest:
        return None  # bare number
    if len(rest) == 1:
        return int(first) * 60 + rest[0]  # MM:SS
    return (int(first) * 60 + rest[0]) * 60 + rest[1]  # HH:MM:SS


def format_duration(seconds: int) -> str:
    """Inverse of `parse_duration`, using the largest unit that divides `seconds`."""
    for unit in ("d", "h", "m", "s"):
        if seconds % _DURATION_UNITS[unit] == 0:
            return f"{seconds // _DURATION_UNITS[unit]}{unit}"
    raise AssertionError("unreachable: every integer is a multiple of 1 s")  # pragma: no cover


def _deferred(value: str) -> str:
    return _check_template(value)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _memory(value: object) -> int | str:
    if isinstance(value, str):
        if _is_deferred(value):
            return _deferred(value)
        try:
            return parse_memory(value)
        except ValueError as exc:
            _fail("flow_memory", str(exc))
    if _is_int(value):
        _fail(
            "flow_memory",
            f"invalid memory {value}: bare numbers are ambiguous; add a binary unit K, M, G or T "
            f"(e.g. {value}M)",
        )
    _fail("flow_memory", f"invalid memory {value!r}: expected a string such as 512M, 8G or 1T")


def _duration(value: object) -> int | str:
    if isinstance(value, str):
        if _is_deferred(value):
            return _deferred(value)
        try:
            return parse_duration(value)
        except ValueError as exc:
            _fail("flow_time", str(exc))
    if _is_int(value):
        _fail(
            "flow_time",
            f"invalid time {value}: bare numbers are ambiguous; add a unit s, m, h or d "
            f"(e.g. {value}m)",
        )
    _fail("flow_time", f"invalid time {value!r}: expected a string such as 30m, 2h or 1-00:00:00")


def _cpus(value: object) -> int | str:
    if isinstance(value, str) and _is_deferred(value):
        return _deferred(value)
    if isinstance(value, int) and _is_int(value) and value > 0:
        return value
    _fail("flow_cpus", f"cpus must be a positive integer or a ${{…}} reference, got {value!r}")


def _deferred_or(pattern_schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [pattern_schema, {"type": "string", "pattern": _DEFERRED_PATTERN}]}


Memory = Annotated[
    int | str,
    PlainValidator(_memory),
    PlainSerializer(lambda v: format_memory(v) if isinstance(v, int) else v, return_type=str),
    WithJsonSchema(_deferred_or({"type": "string", "pattern": MEMORY_PATTERN})),
]
"""Bytes, or a deferred `${…}` string."""

Duration = Annotated[
    int | str,
    PlainValidator(_duration),
    PlainSerializer(lambda v: format_duration(v) if isinstance(v, int) else v, return_type=str),
    WithJsonSchema(_deferred_or({"type": "string", "pattern": DURATION_PATTERN})),
]
"""Seconds, or a deferred `${…}` string."""

Cpus = Annotated[
    int | str,
    PlainValidator(_cpus),
    WithJsonSchema(_deferred_or({"type": "integer", "minimum": 1})),
]
"""CPU count, or a deferred `${…}` string."""


# --- models ------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)


def _one_of_required(*names: str) -> dict[str, Any]:
    return {"oneOf": [{"required": [name]} for name in names]}


def _exactly_one(model: BaseModel, *names: str) -> None:
    by_alias = {(f.alias or attr): attr for attr, f in type(model).model_fields.items()}
    present = [name for name in names if getattr(model, by_alias[name]) is not None]
    if len(present) != 1:
        options = ", ".join(repr(n) for n in names[:-1]) + f" or {names[-1]!r}"
        found = ", ".join(repr(n) for n in present) or "none"
        _fail("flow_exclusive", f"exactly one of {options} is required (found: {found})")


class Resources(_Model):
    """Scheduler resources. Excluded from action keys (architecture.md § Hashing)."""

    cpus: Cpus | None = None
    mem: Memory | None = None
    time: Duration | None = None


class ToolchainRef(_Model):
    """A toolchain the flow uses: module name + version, license features, default resources."""

    module: Annotated[StrictStr, StringConstraints(min_length=1)]
    licenses: dict[LicenseName, LicenseCount] = {}
    resources: Resources = Resources()


class ImportDef(_Model):
    """Another team's release, consumed by channel or exact version (pinned in `flow.lock`)."""

    model_config = ConfigDict(json_schema_extra=_one_of_required("channel", "version"))

    from_: Annotated[StrictStr, StringConstraints(pattern=IMPORT_FROM_PATTERN)] = Field(
        alias="from"
    )
    channel: StrictStr | None = None
    version: StrictStr | None = None

    @model_validator(mode="after")
    def _channel_xor_version(self) -> ImportDef:
        _exactly_one(self, "channel", "version")
        return self


class MatrixDef(_Model):
    """Expands a step over table rows: one table, a cartesian product (`cross`) or `zip`."""

    model_config = ConfigDict(json_schema_extra=_one_of_required("table", "cross", "zip"))

    table: Template | None = None
    cross: Annotated[tuple[Template, ...], Field(min_length=2)] | None = None
    zip_: Annotated[tuple[Template, ...], Field(min_length=2)] | None = Field(
        default=None, alias="zip"
    )
    filter: dict[StrictStr, Template | tuple[Template, ...]] = {}
    id: Annotated[tuple[StrictStr, ...], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> MatrixDef:
        _exactly_one(self, "table", "cross", "zip")
        return self

    @property
    def tables(self) -> tuple[str, ...]:
        """The table paths, in declaration order."""
        if self.table is not None:
            return (self.table,)
        return self.cross if self.cross is not None else self.zip_ or ()


class OutputDef(_Model):
    """A declared output: a file or a directory relative to the action's working directory.

    `deterministic: false` makes consumers key on the producer's action key instead of the
    output bytes (architecture.md § Nondeterministic outputs).
    """

    model_config = ConfigDict(json_schema_extra=_one_of_required("file", "dir"))

    file: RelPath | None = None
    dir: RelPath | None = None
    deterministic: StrictBool = True
    optional: StrictBool = False

    @model_validator(mode="after")
    def _file_xor_dir(self) -> OutputDef:
        _exactly_one(self, "file", "dir")
        return self


def _short_output(value: object) -> object:
    return {"file": value} if isinstance(value, str) else value


OutputField = Annotated[
    OutputDef, BeforeValidator(_short_output, json_schema_input_type=str | OutputDef)
]
"""`name: path` is short for `name: {file: path}` (deterministic, required)."""


class DebugDef(_Model):
    """Files copied to the debug area: always on failure, on success only if `on_success`."""

    collect: tuple[Template, ...] = ()
    max_size: Memory | None = None
    on_success: StrictBool = False


class StepDef(_Model):
    """One step of the flow; `kind` selects the rule (checked per kind in P0-12)."""

    kind: Kind
    toolchain: StrictStr | None = None
    matrix: MatrixDef | None = None
    inputs: dict[Ident, Template | tuple[Template, ...]] = {}
    params: dict[Ident, Template | ParamInt | StrictBool] = {}
    outputs: dict[OutputName, OutputField] = {}
    resources: Resources = Resources()
    licenses: dict[LicenseName, LicenseCount] = {}
    env: dict[EnvName, Template] = {}
    script: Template | None = None
    command: tuple[Template, ...] | None = None
    workdir: Template | None = None
    target: Template | None = None
    debug: DebugDef | None = None
    config_files: dict[ConfigFilePath, Template] = {}

    @model_validator(mode="after")
    def _script_or_command(self) -> StepDef:
        if self.script is not None and self.command is not None:
            _fail("flow_exclusive", "'script' and 'command' are mutually exclusive")
        return self


def _version(value: object) -> int:
    if type(value) is int and value in SUPPORTED_VERSIONS:
        return value
    supported = ", ".join(str(v) for v in SUPPORTED_VERSIONS)
    _fail("flow_version", f"unsupported flow version {value!r}; supported versions: {supported}")


Version = Annotated[
    int,
    PlainValidator(_version),
    WithJsonSchema({"type": "integer", "enum": list(SUPPORTED_VERSIONS)}),
]


class Flow(_Model):
    """A whole `flow.yaml`. Step order is the declaration order."""

    model_config = ConfigDict(
        title="EBS flow",
        json_schema_extra={"description": "EBS flow description (flow.yaml), version 1."},
    )

    version: Version
    project: ProjectName
    domain: ProjectName
    imports: dict[Ident, ImportDef] = {}
    toolchains: dict[Ident, ToolchainRef] = {}
    steps: dict[StepName, StepDef] = {}

    @model_validator(mode="after")
    def _toolchains_declared(self) -> Flow:
        for name, step in self.steps.items():
            if step.toolchain is None or step.toolchain in self.toolchains:
                continue
            detail = f"unknown toolchain {step.toolchain!r}"
            close = difflib.get_close_matches(step.toolchain, self.toolchains, n=1, cutoff=0.75)
            if close:
                detail += f"; did you mean {close[0]!r}?"
            elif self.toolchains:
                detail += f"; declared toolchains: {', '.join(sorted(self.toolchains))}"
            else:
                detail += "; no toolchains are declared under 'toolchains:'"
            _fail("flow_ref", detail, path=("steps", name, "toolchain"))
        return self
