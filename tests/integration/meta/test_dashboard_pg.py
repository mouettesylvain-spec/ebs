"""Dashboard reads of the PostgreSQL store (P2-06); the same contract runs in memory."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta.pg import PgMetadataStore
from tests.helpers.dashboard import (
    check_domain_stats,
    check_list_builds,
    check_result_summaries,
)
from tests.helpers.pg import url_string

pytestmark = pytest.mark.integration


def _store(pg_url: URL, clock: FakeClock) -> PgMetadataStore:
    return PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=clock)


# R1
def test_list_builds_pg(pg_url: URL) -> None:
    clock = FakeClock()
    store = _store(pg_url, clock)
    try:
        check_list_builds(store, clock)
    finally:
        store.close()


# R4
def test_domain_stats_pg(pg_url: URL) -> None:
    clock = FakeClock()
    store = _store(pg_url, clock)
    try:
        check_domain_stats(store, clock)
    finally:
        store.close()


# R2
def test_result_summaries_pg(pg_url: URL) -> None:
    store = _store(pg_url, FakeClock())
    try:
        check_result_summaries(store)
    finally:
        store.close()
