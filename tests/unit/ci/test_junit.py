"""JUnit XML for GitLab merge requests (P2-07 R2)."""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path

import xmlschema

from ebs.cas.fs import FsCAS
from ebs.ci.junit import CACHED_PROPERTY, LOG_TAIL_BYTES, collect_cases, to_xml
from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import ResourceUsage, ResultManifest, RunnerInfo
from ebs.plan.types import ActionSpec, ParamValue
from tests.helpers.driver import Node, make_plan

XSD = Path(__file__).parents[2] / "fixtures" / "ci" / "junit.xsd"


def _sim(spec: ActionSpec, **params: ParamValue) -> ActionSpec:
    return dataclasses.replace(spec, step="sim", params=params)


def _result(
    spec: ActionSpec, *, passed: bool, log: Digest | None = None, **summary: str | int
) -> ResultManifest:
    return ResultManifest(
        action_key=hash_bytes(spec.action_id.encode()),
        status="passed" if passed else "failed",
        exit_code=0 if passed else 1,
        inputs={},
        outputs={},
        log=log,
        summary=summary,
        resources=ResourceUsage(max_rss_kb=0, cpu_s=1, wall_s=42),
        runner=RunnerInfo(version="0.1.0", host="node-a"),
    )


class _Logs:
    def __init__(self, blobs: Mapping[bytes, bytes] | None = None) -> None:
        self.blobs = {hash_bytes(v): v for v in (blobs or {}).values()}

    def __call__(self, d: Digest) -> bytes | None:
        return self.blobs.get(d)


def _scenario(
    tmp_path: Path,
) -> tuple[list[ActionSpec], dict[str, str], dict[str, ResultManifest | None], _Logs]:
    nodes = {"compile": Node()} | {f"sim[{i}]": Node(step="sim") for i in range(5)}
    plan = make_plan(FsCAS(tmp_path / "cas", "test"), nodes)
    compile_, *sims = plan.actions
    actions = [
        compile_,  # not a test: no testcase
        _sim(sims[0], test="smoke", seed=1),
        _sim(sims[1], test="random", seed=7),
        _sim(sims[2], test="random", seed=8),
        _sim(sims[3], test="dma"),
        _sim(sims[4], test="irq", seed=3),
    ]
    noisy = b"x" * (3 * LOG_TAIL_BYTES) + b"\x1b[31m\x00bad\x07 UVM_ERROR @ 10ns: tb.dut boom\n"
    logs = _Logs({b"fail": noisy})
    log = hash_bytes(noisy)
    ids = [a.action_id for a in actions]
    states = dict(zip(ids, ["done", "done", "failed", "cached", "infra_failed", "skipped"],
                      strict=True))  # fmt: skip
    results: dict[str, ResultManifest | None] = {
        ids[0]: _result(actions[0], passed=True),
        ids[1]: _result(actions[1], passed=True),
        ids[2]: _result(
            actions[2],
            passed=False,
            log=log,
            signature="UVM_ERROR @ <T>: tb.dut boom",
            first_error="UVM_ERROR @ 10ns: tb.dut boom",
        ),
        ids[3]: _result(actions[3], passed=False, signature="cached failure sig"),
        ids[4]: None,
        ids[5]: None,
    }
    return actions, states, results, logs


# R2: one testcase per sim action; classname = step, name = test[seed]; failure message = the
# first error signature; system-out = bounded log tail; cached results carry a property.
def test_mapping(tmp_path: Path) -> None:
    actions, states, results, logs = _scenario(tmp_path)
    cases = collect_cases(actions, states, results.get, logs, redact=lambda s: s)
    assert [(c.classname, c.name) for c in cases] == [
        ("sim", "smoke[1]"),
        ("sim", "random[7]"),
        ("sim", "random[8]"),
        ("sim", "dma"),
        ("sim", "irq[3]"),
    ]
    root = ET.fromstring(to_xml(cases, suite="demo"))
    tcs = root.findall("./testsuite/testcase")
    assert len(tcs) == 5
    by_name = {tc.get("name"): tc for tc in tcs}
    assert by_name["smoke[1]"].find("failure") is None
    assert by_name["smoke[1]"].get("time") == "42.000"
    failed = by_name["random[7]"].find("failure")
    assert failed is not None
    assert failed.get("message") == "UVM_ERROR @ <T>: tb.dut boom"
    out = by_name["random[7]"].findtext("system-out")
    assert out is not None
    assert out.endswith("UVM_ERROR @ 10ns: tb.dut boom\n")
    assert len(out.encode()) <= LOG_TAIL_BYTES
    for bad in ("\x00", "\x1b", "\x07"):
        assert bad not in out
    # a cached failure is still a failure, marked as cached
    cached = by_name["random[8]"]
    assert cached.find("failure") is not None
    assert cached.find("failure").get("message") == "cached failure sig"  # type: ignore[union-attr]
    props = {p.get("name"): p.get("value") for p in cached.findall("./properties/property")}
    assert props == {CACHED_PROPERTY: "true"}
    assert by_name["smoke[1]"].find("properties") is None
    # infra failure (never cached) is an error; a skipped action is skipped
    assert by_name["dma"].find("error") is not None
    assert by_name["irq[3]"].find("skipped") is not None
    suite = root.find("testsuite")
    assert suite is not None
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"),
            suite.get("skipped")) == ("5", "2", "1", "1")  # fmt: skip


# R2: a failure without a signature falls back to the first error line, then the exit code.
def test_failure_message_fallbacks(tmp_path: Path) -> None:
    actions, states, results, logs = _scenario(tmp_path)
    sim = actions[2]
    for result, expected in [
        (_result(sim, passed=False, first_error="boom here"), "boom here"),
        (_result(sim, passed=False), "exit code 1"),
    ]:
        results[sim.action_id] = result
        cases = collect_cases([sim], states, results.get, logs, redact=lambda s: s)
        assert cases[0].message == expected


# R2: the document validates against the JUnit XSD GitLab's parser follows, also with hostile text.
def test_schema_valid(tmp_path: Path) -> None:
    actions, states, results, logs = _scenario(tmp_path)
    nasty = _sim(actions[1], test="a<b>&\"c'\x01", seed=0)
    results[nasty.action_id] = _result(nasty, passed=False, signature="<&>\x0b\ud800")
    states[nasty.action_id] = "failed"
    cases = collect_cases([*actions, nasty], states, results.get, logs, redact=lambda s: s)
    schema = xmlschema.XMLSchema(XSD)
    data = to_xml(cases, suite="demo & co")
    schema.validate(data.decode())
    schema.validate(to_xml([], suite="empty").decode())  # a build without tests


# R2 R5: log tails and messages go through the redactor.
def test_redacts_text(tmp_path: Path) -> None:
    actions, states, results, logs = _scenario(tmp_path)
    cases = collect_cases(
        actions, states, results.get, logs, redact=lambda s: s.replace("boom", "***")
    )
    xml = to_xml(cases, suite="demo").decode()
    assert "boom" not in xml
    assert "***" in xml


# R2 R5: the cut keeps a whole first line, and a secret straddling the cut is still masked.
def test_log_tail_cut(tmp_path: Path) -> None:
    actions, states, results, _ = _scenario(tmp_path)
    sim = actions[2]
    line = b"y" * (LOG_TAIL_BYTES - 6) + b"\n"
    whole = b"z" * 100 + b"\n" + line + b"tail\n"  # the window starts right after a newline
    secret = "s3cr3t-" * 10
    straddle = b"q" * LOG_TAIL_BYTES + secret.encode() + b"w" * (LOG_TAIL_BYTES - 20)
    expectations: list[tuple[bytes, Callable[[str], bool]]] = [
        (whole, lambda out: out == line.decode() + "tail\n"),
        (straddle, lambda out: "s3cr3t" not in out and "w" * 10 in out),
    ]
    for data, check in expectations:
        results[sim.action_id] = _result(sim, passed=False, log=hash_bytes(data))
        cases = collect_cases(
            [sim], states, results.get, _Logs({b"k": data}),
            redact=lambda s: s.replace(secret, "[MASKED]"),
        )  # fmt: skip
        assert check(cases[0].system_out), cases[0].system_out[:80]
        assert len(cases[0].system_out.encode()) <= LOG_TAIL_BYTES
