"""The metadata service over PostgreSQL: bearer tokens, readiness, revision 0005 (P1-06)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta import migrations
from ebs.meta.service.readiness import PgReadiness, migration_heads
from ebs.meta.service.tokens import PgTokenStore, hash_token
from tests.helpers.pg import fresh_database, url_string

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(pg_url: URL) -> Iterator[sa.Engine]:
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    yield eng
    eng.dispose()


# R3: tokens are stored hashed; revoked and expired ones stop working.
def test_pg_tokens(engine: sa.Engine) -> None:
    clock = FakeClock()
    store = PgTokenStore(engine, clock=clock)
    token = store.add("ci-cpu", kind="ci", team="rv32x")
    soon = store.add("svc-gc", kind="service", expires_at=clock.now() + timedelta(hours=1))
    info = store.lookup(hash_token(token))
    assert info is not None
    assert (info.principal, info.kind, info.team) == ("ci-cpu", "ci", "rv32x")
    with engine.connect() as conn:
        stored = {r[0] for r in conn.execute(sa.text("SELECT hash FROM ebs.api_tokens"))}
    assert stored == {hash_token(token), hash_token(soon)}  # never the token itself
    assert store.lookup(hash_token("unknown")) is None
    assert store.lookup(hash_token(soon)) is not None
    clock.advance(3601)
    assert store.lookup(hash_token(soon)) is None
    assert store.revoke("ci-cpu") == 1
    assert store.revoke("ci-cpu") == 0
    assert store.lookup(hash_token(token)) is None


@pytest.mark.parametrize(
    ("column", "value"), [("kind", "admin"), ("hash", "plaintext-token")], ids=["kind", "hash"]
)
def test_api_tokens_checks(engine: sa.Engine, column: str, value: str) -> None:
    row = {"principal": "p", "kind": "ci", "hash": hash_token("t"), "created_at": FakeClock().now()}
    row[column] = value
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ebs.api_tokens (principal, kind, hash, created_at) "
                "VALUES (:principal, :kind, :hash, :created_at)"
            ),
            row,
        )


# R6
def test_readiness(pg_server: URL, pg_url: URL, engine: sa.Engine) -> None:
    assert PgReadiness(engine).check() is None
    assert "0007_releases" in migration_heads()
    with fresh_database(pg_server) as empty:
        fresh = sa.create_engine(empty, poolclass=sa.pool.NullPool)
        try:
            reason = PgReadiness(fresh).check()
            assert reason is not None
            assert "not migrated" in reason
            migrations.upgrade(url_string(empty), "0004_toolchain_registry")
            reason = PgReadiness(fresh).check()
            assert reason is not None
            assert "0004_toolchain_registry" in reason
            assert "run the migrations" in reason
            migrations.upgrade(url_string(empty))
            assert PgReadiness(fresh).check() is None
        finally:
            fresh.dispose()
    unreachable = sa.create_engine(pg_url.set(host="127.0.0.1", port=9), poolclass=sa.pool.NullPool)
    reason = PgReadiness(unreachable).check()
    assert reason is not None
    assert "not reachable" in reason


def test_downgrade_0005(pg_server: URL) -> None:
    with fresh_database(pg_server) as url:
        migrations.upgrade(url_string(url))
        migrations.downgrade(url_string(url), "0004_toolchain_registry")
        eng = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            with eng.connect() as conn:
                assert "api_tokens" not in sa.inspect(conn).get_table_names(schema="ebs")
        finally:
            eng.dispose()
