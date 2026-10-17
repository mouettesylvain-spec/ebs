"""I20 matrix (P2-01 R2) on PostgreSQL; the cases are in tests/helpers/authz_matrix.py."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta.pg import PgMetadataStore
from tests.helpers.authz_matrix import covers_every_route, run_matrix
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


# R1, R2: every OpenAPI route is in the matrix (or domain-free).
def test_matrix_covers_every_route(pg: PgMetadataStore) -> None:
    covers_every_route(pg)


# R2 (I20)
def test_authz_matrix(pg: PgMetadataStore) -> None:
    run_matrix(pg)
