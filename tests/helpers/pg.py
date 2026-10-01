"""PostgreSQL fixtures for integration and contract tests (docs/design/testing.md § Fakes).

The server comes from `EBS_TEST_PG_URL` (any database on it; the tests create their own) or, if
unset, from a `testcontainers` PostgreSQL 17 container per session. Each test session (and each
xdist worker) gets a fresh database migrated to head; `pg_url` truncates all tables per test.
Migration tests use `fresh_database` for an empty database of their own.

Import the fixtures into a conftest.py (`from tests.helpers.pg import pg_url  # noqa: F401`).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url

from ebs.meta import migrations
from ebs.meta.models import SCHEMA, metadata

_MISSING = (
    "missing dependency: PostgreSQL (set EBS_TEST_PG_URL=postgresql+psycopg://user:pw@host/db "
    "or make Docker available for testcontainers)"
)


def _psycopg_url(raw: str) -> URL:
    url = make_url(raw)
    return url.set(drivername="postgresql+psycopg")


def url_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[URL]:
    """Admin URL of a PostgreSQL server the tests may create databases on."""
    raw = os.environ.get("EBS_TEST_PG_URL")
    if raw:
        yield _psycopg_url(raw)
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev extra always installs it
        pytest.skip(_MISSING)
    try:
        container = PostgresContainer("postgres:17", driver="psycopg")
        container.start()
    except Exception as exc:  # docker missing or not running
        pytest.skip(f"{_MISSING}: {exc}")
    try:
        yield _psycopg_url(container.get_connection_url())
    finally:
        container.stop()


@contextmanager
def fresh_database(server: URL) -> Iterator[URL]:
    """Create an empty database on `server`, yield its URL, drop it afterwards."""
    name = f"ebs_test_{secrets.token_hex(6)}"
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        try:
            yield server.set(database=name)
        finally:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def pg_migrated(pg_server: URL) -> Iterator[URL]:
    """A database of this session migrated to head."""
    with fresh_database(pg_server) as url:
        migrations.upgrade(url_string(url))
        yield url


@pytest.fixture(scope="session")
def _pg_truncate_engine(pg_migrated: URL) -> Iterator[sa.Engine]:
    engine = sa.create_engine(pg_migrated, poolclass=sa.pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_url(pg_migrated: URL, _pg_truncate_engine: sa.Engine) -> URL:
    """URL of the session database with every ebs table emptied."""
    tables = ", ".join(f'"{SCHEMA}"."{t.name}"' for t in metadata.sorted_tables)
    with _pg_truncate_engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return pg_migrated
