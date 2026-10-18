"""Pure parts of `ebs.release.provenance` (P2-04 R3) and the HTTP start limit (R5)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest

from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.meta.api import PROVENANCE_START_MAX, BuildId, ProvenanceQuery
from ebs.meta.client import HttpMetadataStore
from ebs.release.provenance import TestRun, junit_xml, latest

T = datetime(2026, 10, 1, tzinfo=UTC)


def _run(build: int, *, seed: object = 1, at: datetime | None = T, status: str | None = "passed"
         ) -> TestRun:  # fmt: skip
    return TestRun(
        domain="d", build=BuildId(build), action_id=f"sim[{build}]", step="sim", rule="questa.sim",
        test="t", seed=seed, status=status, state="done", cached=False,  # type: ignore[arg-type]
        toolchain=None, finished_at=at, project="p",
    )  # fmt: skip


# R3: ties on the finish time go to the newer build; a run without a finish time loses; seeds
# 1 and "1" are different rows.
def test_latest_ties_and_unfinished() -> None:
    a, b = _run(1), _run(2)
    assert [r.build for r in latest([a, b])] == [2]
    assert [r.build for r in latest([b, a])] == [2]
    done, open_ = _run(1), _run(9, at=None, status=None)
    assert [r.build for r in latest([done, open_])] == [1]
    assert [r.build for r in latest([open_, done])] == [1]
    assert len(latest([_run(1, seed=1), _run(2, seed="1")])) == 2


# R3: JUnit: a failed result is a failure, a run without a result is skipped.
def test_junit_failures_and_skips() -> None:
    xml = junit_xml([_run(1), _run(2, status="failed"), _run(3, status=None)], suite="r 1")
    suite = ET.fromstring(xml)
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("3", "1", "1")
    cases = suite.findall("testcase")
    assert [c.find("failure") is not None for c in cases] == [False, True, False]
    assert [c.find("skipped") is not None for c in cases] == [False, False, True]


# A walk over HTTP starts from at most PROVENANCE_START_MAX ids, refused before any request.
def test_http_walk_start_limit() -> None:
    ids = [hash_bytes(str(i).encode()) for i in range(PROVENANCE_START_MAX + 1)]
    ProvenanceQuery.model_validate({"ids": [str(d) for d in ids[:-1]]})  # the limit itself is fine
    store = HttpMetadataStore("http://ebs-meta.invalid", credentials=None)
    with pytest.raises(MetadataError, match="at most"):
        store.provenance_upstream("d", ids=ids, keys=[], depth=1)
