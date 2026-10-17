"""Create releases from a finished build's outputs (P2-02 R1, R2).

`ebs release create NAME VERSION --build B --output step.output[selector]…` selects outputs the
way flows reference them (`${steps.compile.outputs.worklib[lib=core]}`):

- `step.output`: the step's only action (a matrix step needs a selector);
- `step.output[*]`: every action of the step;
- `step.output[col=value,…]`: the actions whose matrix row has these values (one or more).

Each selected action's recorded result gives the output's digest, id, type and size; the release
holds it as `<action id>.<output>`. The store checks the selection again against the build (an
HTTP caller is not trusted), so the rules hold for every client. Releases are immutable; `yank`
marks one as withdrawn and never deletes it (I19).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias
from urllib.parse import unquote

from ebs.core.errors import ReleaseError
from ebs.meta.api import (
    RELEASE_NAME_PATTERN,
    VERSION_PATTERN,
    BuildId,
    MetadataBackend,
    MetadataStore,
    ReleaseCreate,
    ReleaseOutput,
    ReleaseView,
    release_output_name,
)

__all__ = [
    "OutputSelection",
    "ReleaseBackend",
    "Selected",
    "check_name",
    "check_version",
    "create_release",
    "parse_selection",
    "select_outputs",
]

_SELECTION: Final = re.compile(
    r"^(?P<step>[A-Za-z_][A-Za-z0-9_-]*)\.(?P<output>[A-Za-z_][A-Za-z0-9_-]*)"
    r"(?:\[(?P<sel>\*|[^\[\]=,]+=[^\[\]=,]*(?:,[^\[\]=,]+=[^\[\]=,]*)*)\])?$"
)
_ACTION_ID: Final = re.compile(r"^(?P<step>[^\[]+)(?:\[(?P<pairs>.*)\])?$")


ReleaseBackend: TypeAlias = MetadataBackend
"""A store with builds and releases: the PostgreSQL, in-memory and HTTP stores."""


@dataclass(frozen=True, slots=True)
class OutputSelection:
    """One `--output` argument."""

    text: str
    step: str
    output: str
    selector: Literal["*"] | tuple[tuple[str, str], ...] | None


def check_version(version: str) -> str:
    """`version` if it is a valid release version (D4), else ReleaseError."""
    if re.fullmatch(VERSION_PATTERN, version) is None:
        raise ReleaseError(
            f"invalid release version {version!r}: use 1-64 letters, digits, '.', '_', '+' or "
            "'-', starting with a letter or digit (CalVer YYYY.MM.N is recommended, e.g. "
            "2026.09.3)"
        )
    return version


def check_name(name: str) -> str:
    if re.fullmatch(RELEASE_NAME_PATTERN, name) is None:
        raise ReleaseError(
            f"invalid release name {name!r}: use `team/product` or `product`, each part lower-"
            "case letters, digits, '.', '_' or '-' (e.g. rtl-team/cpu)"
        )
    return name


def parse_selection(text: str) -> OutputSelection:
    m = _SELECTION.fullmatch(text)
    if m is None:
        raise ReleaseError(
            f"invalid --output {text!r}: expected step.output, step.output[*] or "
            "step.output[column=value,…]"
        )
    sel = m["sel"]
    selector: Literal["*"] | tuple[tuple[str, str], ...] | None
    if sel is None:
        selector = None
    elif sel == "*":
        selector = "*"
    else:
        selector = tuple((k, v) for k, _, v in (pair.partition("=") for pair in sel.split(",")))
    return OutputSelection(text, m["step"], m["output"], selector)


def _row(action_id: str) -> dict[str, str]:
    """The matrix row values in an action id `step[k=v,…]` (percent-encoded)."""
    m = _ACTION_ID.fullmatch(action_id)
    if m is None or m["pairs"] is None:
        return {}
    pairs = (p.partition("=") for p in m["pairs"].split(",") if p)
    return {unquote(k): unquote(v) for k, _, v in pairs}


@dataclass(frozen=True, slots=True)
class Selected:
    outputs: dict[str, ReleaseOutput]
    not_passed: list[str]  # "<action id> (<state>, <result status>)"


def select_outputs(
    store: MetadataStore, build: BuildId, selections: Sequence[OutputSelection]
) -> Selected:
    """The outputs the selections name, with the results the build recorded for them, and the
    selected actions that did not pass."""
    if not selections:
        raise ReleaseError("select at least one output with --output step.output[…]")
    by_step: dict[str, list[tuple[str, dict[str, str]]]] = {}
    rows = {row.action_id: row for row in store.list_actions(build)}
    not_passed: list[str] = []
    for row in rows.values():
        by_step.setdefault(row.step, []).append((row.action_id, _row(row.action_id)))
    chosen: dict[str, ReleaseOutput] = {}
    for sel in selections:
        actions = sorted(by_step.get(sel.step, []))
        if not actions:
            known = ", ".join(sorted(by_step)) or "none"
            raise ReleaseError(
                f"--output {sel.text}: build {build} has no step {sel.step!r} (steps: {known})"
            )
        if sel.selector is None:
            if len(actions) > 1:
                raise ReleaseError(
                    f"--output {sel.text}: step {sel.step!r} has {len(actions)} actions; use "
                    f"{sel.step}.{sel.output}[*] for all of them or "
                    f"{sel.step}.{sel.output}[column=value] to pick some"
                )
            picked = [a for a, _ in actions]
        elif sel.selector == "*":
            picked = [a for a, _ in actions]
        else:
            wanted = dict(sel.selector)
            picked = [a for a, v in actions if all(v.get(k) == x for k, x in wanted.items())]
            if not picked:
                ids = ", ".join(a for a, _ in actions)
                raise ReleaseError(
                    f"--output {sel.text}: no action of step {sel.step!r} matches; its actions: "
                    f"{ids}"
                )
        for action_id in picked:
            result = store.get_result(build, action_id)
            recorded = None if result is None else result.outputs.get(sel.output)
            if recorded is None:
                raise ReleaseError(
                    f"--output {sel.text}: action {action_id!r} of build {build} has no "
                    f"recorded output {sel.output!r} (it did not run to completion, or the "
                    "output is optional and was not produced)"
                )
            assert result is not None
            state = rows[action_id].state
            label = f"{action_id} ({state}, {result.status})"
            if (state not in ("done", "cached") or result.status != "passed") and (
                label not in not_passed
            ):
                not_passed.append(label)
            chosen[release_output_name(action_id, sel.output)] = ReleaseOutput(
                action_id=action_id,
                output=sel.output,
                digest=recorded.digest,
                id=recorded.id,
                type=recorded.type,
                size=recorded.size,
            )
    return Selected(chosen, not_passed)


def create_release(
    store: ReleaseBackend,
    *,
    name: str,
    version: str,
    build: BuildId,
    outputs: Sequence[str],
    by: str,
    notes: str = "",
    allow_failed: str | None = None,
) -> ReleaseView:
    """Release the selected outputs of `build` as `name version` in the build's domain."""
    check_name(name)
    check_version(version)
    if allow_failed is not None and not allow_failed.strip():
        raise ReleaseError("--allow-failed needs a reason, which is stored with the release")
    view = store.get_build(build)
    if view.status == "running":
        raise ReleaseError(f"build {build} is still running; release it once it has finished")
    if store.get_release(view.domain, name, version) is not None:
        raise ReleaseError(
            f"release {name} {version} already exists in domain {view.domain!r}; releases are "
            "immutable, so publish a new version"
        )
    selected = select_outputs(store, build, [parse_selection(o) for o in outputs])
    if selected.not_passed and allow_failed is None:
        raise ReleaseError(
            f"these actions of build {build} did not pass: {', '.join(selected.not_passed)}; "
            "release only passed outputs, or give --allow-failed REASON (stored with the release)"
        )
    spec = ReleaseCreate(
        domain=view.domain,
        name=name,
        version=version,
        build=build,
        outputs=selected.outputs,
        notes=notes,
        allow_failed=allow_failed,
    )
    return store.create_release(spec, by=by)
