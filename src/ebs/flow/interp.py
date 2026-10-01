"""`${…}` interpolation: parse templates and render them against a resolver (task P0-05).

Grammar: docs/design/interfaces.md § 3. Rendering is pure string substitution: it never evaluates
code, never reads the calling process's environment and never re-scans inserted values, so a row
value holding `${…}` or shell syntax is inserted verbatim.

`Scope` resolves the references local to one step instance (`row.*`, `params.*`, `env.*`) and
hands `imports.*` and `steps.*` to an outer `Resolver` supplied by the planner (P0-08).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal as TypingLiteral
from typing import Protocol, TypeAlias

from ebs.core.errors import FlowError

__all__ = [
    "Literal",
    "ParamValue",
    "Ref",
    "RefKind",
    "Resolver",
    "Scope",
    "SourceLocation",
    "Template",
    "parse_template",
    "render",
]

RefKind: TypeAlias = TypingLiteral["row", "params", "env", "imports", "steps"]
Selector: TypeAlias = "TypingLiteral['*'] | tuple[tuple[str, str], ...]"
ParamValue: TypeAlias = "str | int | bool | tuple[str, ...]"
"""A resolved param: literals keep their YAML type; a sole list-valued reference is a tuple."""

_KINDS: tuple[RefKind, ...] = ("row", "params", "env", "imports", "steps")

# Same grammar as `ebs.flow.model.check_ref_syntax`; tests keep the two in agreement.
_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_NAME = r"[a-z][a-z0-9_]{0,62}"
_VALUE = r"[^,\[\]{}$\s=]+"
_ROWSEL = rf"{_ID}={_VALUE}(?:,{_ID}={_VALUE})*"
_REF_RE = re.compile(
    rf"""
      (?P<local>row|params|env)\.(?P<name>{_ID})
    | imports\.(?P<imp>{_ID})(?:/(?P<glob>[^\s{{}}$]+))?
    | steps\.(?P<step>{_NAME})\.outputs\.(?P<out>{_NAME})(?:\[(?P<sel>\*|{_ROWSEL})\])?
    """,
    re.VERBOSE | re.ASCII,
)
_FORMS = "${row.X}, ${params.X}, ${env.X}, ${imports.X[/glob]} or ${steps.S.outputs.O[*|k=v,…]}"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a template comes from, for error messages: `flow.yaml: steps.sim.params.test`."""

    step: str | None = None
    field: str | None = None
    file: str | None = None

    def __str__(self) -> str:
        path = ".".join(p for p in (f"steps.{self.step}" if self.step else None, self.field) if p)
        return path or "template"


_NOWHERE = SourceLocation()


@dataclass(frozen=True, slots=True)
class Literal:
    """Literal text of a template (escapes already applied: `$${` became `${`)."""

    text: str


@dataclass(frozen=True, slots=True)
class Ref:
    """One `${…}` reference.

    `name` is the column, param, env var, import or step name. `glob` is the path after an import
    (`${imports.rtl/**/*.sv}`); `output` and `selector` belong to `steps.` refs, where the
    selector is `"*"` (all instances) or the sorted-as-written `(column, value)` pairs.
    """

    kind: RefKind
    name: str
    glob: str | None = None
    output: str | None = None
    selector: Selector | None = None

    @property
    def text(self) -> str:
        """The reference as written in a flow."""
        if self.kind == "imports":
            body = f"imports.{self.name}" + (f"/{self.glob}" if self.glob is not None else "")
        elif self.kind == "steps":
            body = f"steps.{self.name}.outputs.{self.output}"
            if self.selector == "*":
                body += "[*]"
            elif self.selector is not None:
                body += "[" + ",".join(f"{k}={v}" for k, v in self.selector) + "]"
        else:
            body = f"{self.kind}.{self.name}"
        return "${" + body + "}"


@dataclass(frozen=True, slots=True)
class Template:
    """A parsed template: literal and reference parts, adjacent literals merged.

    `str(template)` writes it back in flow syntax, so `parse_template(str(t)) == t`.
    """

    parts: tuple[Literal | Ref, ...]

    @property
    def refs(self) -> tuple[Ref, ...]:
        return tuple(p for p in self.parts if isinstance(p, Ref))

    def __str__(self) -> str:
        return "".join(
            p.text.replace("${", "$${") if isinstance(p, Literal) else p.text for p in self.parts
        )


class Resolver(Protocol):
    """Resolves one reference to a string, or to a list for multi-valued refs (`[*]` fan-in)."""

    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]: ...


def _error(where: SourceLocation, message: str) -> FlowError:
    return FlowError(f"{where}: {message}", file=where.file)


def _suggest(name: str, choices: Sequence[str]) -> str:
    close = difflib.get_close_matches(name, choices, n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else ""


def _parse_ref(body: str, text: str, where: SourceLocation) -> Ref:
    m = _REF_RE.fullmatch(body)
    if m is None:
        prefix = body.split(".", 1)[0]
        hint = _suggest(prefix, _KINDS) if prefix not in _KINDS else ""
        raise _error(
            where,
            f"malformed reference {'${' + body + '}'!r} in {text!r}: expected {_FORMS}{hint}; "
            "write '$${' for a literal '${' (e.g. a shell variable)",
        )
    if m["local"] is not None:
        kind: RefKind = m["local"]  # type: ignore[assignment]  # regex alternatives
        return Ref(kind=kind, name=m["name"])
    if m["imp"] is not None:
        return Ref(kind="imports", name=m["imp"], glob=m["glob"])
    sel = m["sel"]
    selector: Selector | None
    if sel is None:
        selector = None
    elif sel == "*":
        selector = "*"
    else:
        selector = tuple(
            (key, value) for key, _, value in (pair.partition("=") for pair in sel.split(","))
        )
    return Ref(kind="steps", name=m["step"], output=m["out"], selector=selector)


def parse_template(s: str, *, where: SourceLocation = _NOWHERE) -> Template:
    """Split `s` into literal and reference parts; raises FlowError on malformed references.

    Scanned left to right: `$${` is an escaped literal `${`; every other `${` opens a reference
    closed by the next `}`.
    """
    parts: list[Literal | Ref] = []
    buf: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s.startswith("$${", i):
            buf.append("${")
            i += 3
        elif s.startswith("${", i):
            end = s.find("}", i + 2)
            if end < 0:
                raise _error(
                    where,
                    f"unterminated reference at offset {i} in {s!r}: close it with '}}', "
                    "or write '$${' for a literal '${'",
                )
            if buf:
                parts.append(Literal("".join(buf)))
                buf = []
            parts.append(_parse_ref(s[i + 2 : end], s, where))
            i = end + 1
        else:
            nxt = s.find("$", i + 1)
            nxt = n if nxt < 0 else nxt
            buf.append(s[i:nxt])
            i = nxt
    if buf:
        parts.append(Literal("".join(buf)))
    return Template(tuple(parts))


def render(t: Template, resolver: Resolver, *, where: SourceLocation = _NOWHERE) -> str | list[str]:
    """Substitute every reference.

    A template that is exactly one reference returns whatever the resolver returns, so a
    list-valued ref (`${steps.c.outputs.w[*]}`) renders a list. A list inside text is an error.
    """
    if len(t.parts) == 1 and isinstance(t.parts[0], Ref):
        value = resolver.resolve(t.parts[0], where=where)
        return list(value) if isinstance(value, list) else value
    out: list[str] = []
    for part in t.parts:
        if isinstance(part, Literal):
            out.append(part.text)
            continue
        value = resolver.resolve(part, where=where)
        if isinstance(value, list):
            raise _error(
                where,
                f"{part.text} is a list ({len(value)} items) and cannot be joined with other "
                f"text in {str(t)!r}; use the reference alone as the whole value",
            )
        out.append(value)
    return "".join(out)


def _text(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Scope:
    """Resolver for one step instance: its row, its params and its declared env.

    Params and env may reference each other (and the row); they are resolved on first use with
    cycle detection. `imports.*` and `steps.*` go to `outer`, or are errors without it.
    """

    def __init__(
        self,
        *,
        step: str,
        row: Mapping[str, str] | None,
        params: Mapping[str, str | int | bool],
        env: Mapping[str, str],
        outer: Resolver | None = None,
        file: str | None = None,
    ) -> None:
        self._step = step
        self._row = row
        self._raw_params = params
        self._raw_env = env
        self._outer = outer
        self._file = file
        self._params: dict[str, ParamValue] = {}
        self._env: dict[str, str] = {}
        self._stack: list[str] = []

    def with_outer(self, outer: Resolver | None) -> Scope:
        """The same scope (sharing already resolved values) with another outer resolver."""
        clone = Scope(
            step=self._step,
            row=self._row,
            params=self._raw_params,
            env=self._raw_env,
            outer=outer,
            file=self._file,
        )
        clone._params = self._params
        clone._env = self._env
        return clone

    def location(self, field: str) -> SourceLocation:
        return SourceLocation(step=self._step, field=field, file=self._file)

    def params(self) -> dict[str, ParamValue]:
        """All params, resolved, in declaration order."""
        return {name: self._param(name) for name in self._raw_params}

    def env(self) -> dict[str, str]:
        """All declared env vars, resolved, in declaration order."""
        return {name: self._env_var(name) for name in self._raw_env}

    def render(self, text: str, *, field: str) -> str | list[str]:
        where = self.location(field)
        return render(parse_template(text, where=where), self, where=where)

    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
        if ref.kind == "row":
            return self._row_value(ref.name, where)
        if ref.kind == "params":
            if ref.name not in self._raw_params:
                raise _error(
                    where,
                    f"{ref.text}: step {self._step!r} has no param {ref.name!r}"
                    f"{_suggest(ref.name, list(self._raw_params))}; "
                    f"declared params: {', '.join(sorted(self._raw_params)) or 'none'}",
                )
            value = self._param(ref.name)
            return list(value) if isinstance(value, tuple) else _text(value)
        if ref.kind == "env":
            if ref.name not in self._raw_env:
                raise _error(
                    where,
                    f"{ref.text}: {ref.name!r} is not declared in the 'env' of step "
                    f"{self._step!r}{_suggest(ref.name, list(self._raw_env))}; only declared "
                    "env vars can be referenced (the calling environment is never read). Declared: "
                    f"{', '.join(sorted(self._raw_env)) or 'none'}",
                )
            return self._env_var(ref.name)
        if self._outer is None:
            raise _error(
                where,
                f"{ref.text} is resolved by the planner and cannot be used here",
            )
        return self._outer.resolve(ref, where=where)

    def _row_value(self, column: str, where: SourceLocation) -> str:
        ref = "${row." + column + "}"
        if self._row is None:
            raise _error(
                where,
                f"{ref}: step {self._step!r} has no matrix, so there is no row to read; "
                "add 'matrix: {table: …}' or use a param",
            )
        if column not in self._row:
            available = sorted(self._row)
            raise _error(
                where,
                f"{ref}: the matrix has no column {column!r}; "
                f"available columns: {', '.join(available) or 'none'}"
                f"{_suggest(column, available)}",
            )
        return self._row[column]

    def _enter(self, key: str) -> None:
        if key in self._stack:
            cycle = " -> ".join([*self._stack[self._stack.index(key) :], key])
            raise _error(
                self.location(key), f"reference cycle {cycle}; break it with a literal value"
            )
        self._stack.append(key)

    def _param(self, name: str) -> ParamValue:
        if name in self._params:
            return self._params[name]
        raw = self._raw_params[name]
        if not isinstance(raw, str):
            self._params[name] = raw
            return raw
        self._enter(f"params.{name}")
        try:
            value = self.render(raw, field=f"params.{name}")
        finally:
            self._stack.pop()
        resolved: ParamValue = tuple(value) if isinstance(value, list) else value
        self._params[name] = resolved
        return resolved

    def _env_var(self, name: str) -> str:
        if name in self._env:
            return self._env[name]
        self._enter(f"env.{name}")
        try:
            value = self.render(self._raw_env[name], field=f"env.{name}")
        finally:
            self._stack.pop()
        if isinstance(value, list):
            raise _error(
                self.location(f"env.{name}"),
                f"env.{name} resolved to a list ({len(value)} items); env values must be strings",
            )
        self._env[name] = value
        return value
