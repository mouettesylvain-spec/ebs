"""JUnit XML of a build's test actions, for GitLab merge requests (P2-07 R2).

A test action is one with a `test` param (a `questa.sim` row, or any step that declares one): one
`<testcase>` each, `classname` = its step, `name` = `test[seed]` (or `test` without a seed). A
failure's message is the first error signature the rule summarized (`signature`, else
`first_error`, else the exit code); `<system-out>` is the last `LOG_TAIL_BYTES` of its log. An
infrastructure failure is an `<error>`, an action that never ran is `<skipped>`, and a result
taken from the action cache carries the property `ebs.cached=true`. All text goes through
`redact` and loses the characters XML 1.0 cannot carry. The schema: tests/fixtures/ci/junit.xsd.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ebs.core.digest import Digest
from ebs.meta.api import ResultManifest
from ebs.plan.types import ActionSpec

__all__ = [
    "CACHED_PROPERTY",
    "LOG_READ_BYTES",
    "LOG_TAIL_BYTES",
    "CaseState",
    "TestCase",
    "collect_cases",
    "to_xml",
]

CACHED_PROPERTY: Final = "ebs.cached"
LOG_TAIL_BYTES: Final = 16 * 1024
"""Bound of each testcase's `<system-out>` (GitLab shows it in the MR widget)."""
LOG_READ_BYTES: Final = 2 * LOG_TAIL_BYTES
"""How much of a log `read_log` must return: redaction runs on more than the window."""

CaseState = Literal["passed", "failed", "error", "skipped"]

# Characters XML 1.0 cannot carry (and ANSI escape sequences, which only clutter the MR view).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NOT_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


@dataclass(frozen=True)
class TestCase:
    """One `<testcase>`."""

    __test__ = False  # not a pytest class

    classname: str
    name: str
    state: CaseState
    message: str | None = None
    system_out: str = ""
    cached: bool = False
    time_s: int = 0


def _clean(text: str) -> str:
    return _NOT_XML.sub("", _ANSI.sub("", text))


def _tail(text: str) -> str:
    """The last `LOG_TAIL_BYTES` of `text`, starting at a line start when the cut falls inside
    a line (and the window holds another line)."""
    raw = text.encode("utf-8")
    if len(raw) <= LOG_TAIL_BYTES:
        return text
    tail = raw[-LOG_TAIL_BYTES:]
    whole = raw[-LOG_TAIL_BYTES - 1 : -LOG_TAIL_BYTES] == b"\n"
    if not whole and b"\n" in tail[:-1]:
        tail = tail[tail.index(b"\n") + 1 :]
    return tail.decode("utf-8", errors="ignore")


def _case_name(spec: ActionSpec) -> str | None:
    test = spec.params.get("test")
    if test is None:
        return None
    seed = spec.params.get("seed")
    return str(test) if seed is None else f"{test}[{seed}]"


def _message(result: ResultManifest) -> str:
    for key in ("signature", "first_error"):
        value = result.summary.get(key)
        if isinstance(value, str) and value:
            return value
    return f"exit code {result.exit_code}"


def _state(state: str, result: ResultManifest | None) -> CaseState:
    if state in ("done", "cached", "failed") and result is not None:
        return "passed" if result.status == "passed" else "failed"
    if state in ("infra_failed", "failed", "done"):  # failed/done without a manifest: infra
        return "error"
    return "skipped"


def collect_cases(
    actions: Iterable[ActionSpec],
    states: Mapping[str, str],
    get_result: Callable[[str], ResultManifest | None],
    read_log: Callable[[Digest], bytes | None],
    *,
    redact: Callable[[str], str],
) -> list[TestCase]:
    """The testcases of a finished build's test actions, in plan order.

    `states`: final state per action id (the driver's `done`, `cached`, `failed`, …);
    `get_result`: an action's manifest, asked for test actions only; `read_log`: a log blob's
    bytes (its last `LOG_READ_BYTES` are enough), None if it is gone. The log is redacted
    before it is cut, so a secret straddling the cut is still masked.
    """
    cases = []
    for spec in actions:
        name = _case_name(spec)
        if name is None:
            continue
        state = states.get(spec.action_id, "skipped")
        result = get_result(spec.action_id)
        case_state = _state(state, result)
        message: str | None = None
        out = ""
        if case_state == "failed" and result is not None:
            message = _message(result)
        elif case_state == "error":
            message = "infrastructure failure (not cached; see `ebs logs`)"
        elif case_state == "skipped":
            message = f"not run ({state})"
        if result is not None and result.log is not None:
            data = read_log(result.log)
            out = "" if data is None else redact(data.decode("utf-8", errors="replace"))
        cases.append(
            TestCase(
                classname=redact(spec.step),
                name=redact(name),
                state=case_state,
                message=None if message is None else _clean(redact(message)),
                system_out=_tail(_clean(out)),
                cached=state == "cached",
                time_s=0 if result is None else result.resources.wall_s,
            )
        )
    return cases


def to_xml(cases: Sequence[TestCase], *, suite: str) -> bytes:
    """`<testsuites><testsuite name=suite>…` as UTF-8 bytes with an XML declaration."""
    counts = {s: sum(1 for c in cases if c.state == s) for s in ("failed", "error", "skipped")}
    total = str(sum(c.time_s for c in cases))
    attrs = {
        "tests": str(len(cases)),
        "failures": str(counts["failed"]),
        "errors": str(counts["error"]),
        "skipped": str(counts["skipped"]),
        "time": f"{total}.000",
    }
    root = ET.Element("testsuites", {"name": _clean(suite), **attrs})
    node = ET.SubElement(root, "testsuite", {"name": _clean(suite), **attrs})
    for case in cases:
        tc = ET.SubElement(
            node,
            "testcase",
            {
                "classname": _clean(case.classname),
                "name": _clean(case.name),
                "time": f"{case.time_s}.000",
            },
        )
        if case.cached:
            props = ET.SubElement(tc, "properties")
            ET.SubElement(props, "property", {"name": CACHED_PROPERTY, "value": "true"})
        tag = {"failed": "failure", "error": "error", "skipped": "skipped"}.get(case.state)
        if tag is not None:
            ET.SubElement(tc, tag, {"message": case.message or ""})
        if case.system_out:
            ET.SubElement(tc, "system-out").text = case.system_out
    ET.indent(root)
    data: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return data + b"\n"
