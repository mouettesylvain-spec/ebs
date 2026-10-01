"""Validation on write (R8), the SQL schema mirroring it, and store configuration (R9)."""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import ConfigError, DigestError, MetadataError
from ebs.meta.api import (
    DIGEST_SQL_PATTERN,
    ActionRow,
    BuildCreate,
    BuildId,
    Event,
    ResultManifest,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.models import metadata
from ebs.meta.pg import MetadataConfig, PgMetadataStore

K = hash_bytes(b"k")


def _result(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "v": 1,
        "action_key": str(K),
        "status": "passed",
        "exit_code": 0,
        "inputs": {"a.sv": str(K)},
        "outputs": {"rep": {"digest": str(K), "id": str(K), "type": "file", "size": 1}},
        "log": None,
        "summary": {"errors": 0},
        "resources": {"max_rss_kb": 1, "cpu_s": 1, "wall_s": 1},
        "runner": {"version": "0.1", "host": "node-a", "slurm_job_id": None},
    }
    return {**base, **overrides}


def test_result_manifest_json_round_trip() -> None:
    r = ResultManifest.model_validate(_result())
    assert r.action_key == K
    assert isinstance(r.outputs["rep"].id, Digest)
    assert ResultManifest.model_validate(r.to_json()) == r
    assert r.to_json()["action_key"] == str(K)


@pytest.mark.parametrize(
    "overrides",
    [
        {"action_key": "sha256:XYZ"},
        {"action_key": "md5:" + "0" * 32},
        {"v": 2},
        {"status": "infra_failed"},  # infra failures never produce a manifest
        {"exit_code": "0"},
        {"exit_code": True},
        {"summary": {"ratio": 0.5}},
        {"summary": {"flag": True}},
        {"inputs": {"": str(K)}},
        {"inputs": {"/abs/path": str(K)}},
        {"outputs": {"rep": {"digest": str(K), "id": str(K), "type": "dir", "size": 1}}},
        {"outputs": {"rep": {"digest": str(K), "id": str(K), "type": "file", "size": -1}}},
        {"resources": {"max_rss_kb": 1, "cpu_s": 1.5, "wall_s": 1}},
        {"surprise": 1},
    ],
    ids=lambda o: ",".join(f"{k}={v!r}"[:40] for k, v in o.items()),
)
def test_result_manifest_rejects(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResultManifest.model_validate(_result(**overrides))


def _store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore(clock=FakeClock())


def _unchecked_result(**fields: object) -> ResultManifest:
    good = ResultManifest.model_validate(_result())
    return ResultManifest.model_construct(**{**dict(good), **fields})


def _build() -> BuildCreate:
    return BuildCreate(
        uuid=uuid4(),
        domain="test",
        project="demo",
        plan_digest=K,
        flow_repo="r",
        flow_commit="c",
        flow_dirty=False,
        user_name="u",
        ci_job=None,
        cache_mode="read",
    )


def _with_build(fn: Callable[[InMemoryMetadataStore, BuildId], None]) -> Callable[[], None]:
    def run() -> None:
        s = _store()
        b = s.create_build(_build())
        s.add_actions(b, [ActionRow(action_id="a", step="lint")])
        fn(s, b)

    return run


# Values smuggled past the constructors (model_construct) must still be refused on write.
WRITE_CASES: dict[str, Callable[[], object]] = {
    "cache_put bad key type": lambda: _store().cache_put("test", "sha256:x", _result()),  # type: ignore[arg-type]
    "cache_put key not matching manifest": lambda: _store().cache_put(
        "test", hash_bytes(b"other"), ResultManifest.model_validate(_result())
    ),
    "cache_put float in summary": lambda: _store().cache_put(
        "test", K, _unchecked_result(summary={"ratio": 0.5})
    ),
    "cache_put bad digest string": lambda: _store().cache_put(
        "test", K, _unchecked_result(log="sha256:nothex")
    ),
    "cache_put non-NFC summary": lambda: _store().cache_put(
        "test", K, _unchecked_result(summary={"note": "é"})
    ),
    "cache_put bad domain": lambda: _store().cache_put(
        "a b", K, ResultManifest.model_validate(_result())
    ),
    "create_build bad domain": lambda: _store().create_build(
        BuildCreate.model_construct(**{**dict(_build()), "domain": "../x"})
    ),
    "create_build bad cache mode": lambda: _store().create_build(
        BuildCreate.model_construct(**{**dict(_build()), "cache_mode": "sometimes"})
    ),
    "add_actions empty id": _with_build(
        lambda s, b: s.add_actions(b, [ActionRow.model_construct(action_id="", step="x")])
    ),
    "add_actions bad key": _with_build(
        lambda s, b: s.add_actions(
            b, [ActionRow.model_construct(action_id="z", step="x", key="sha256:0")]
        )
    ),
    "add_actions bad step": _with_build(
        lambda s, b: s.add_actions(b, [ActionRow.model_construct(action_id="z", step="Bad Step")])
    ),
    "record_result float": _with_build(
        lambda s, b: s.record_result(b, "a", _unchecked_result(exit_code=1.0))
    ),
    "emit float in data": _with_build(
        lambda s, b: s.emit(
            Event.model_construct(
                v=1, ts=FakeClock().now(), build=b, type="running", action_id=None, data={"x": 0.1}
            )
        )
    ),
    "emit unknown type": _with_build(
        lambda s, b: s.emit(
            Event.model_construct(
                v=1, ts=FakeClock().now(), build=b, type="exploded", action_id=None, data={}
            )
        )
    ),
    "touch bad digest": lambda: _store().touch("test", ["sha256:0"]),  # type: ignore[list-item]
}


# R8
@pytest.mark.parametrize("case", sorted(WRITE_CASES))
def test_validation_on_write(case: str) -> None:
    with pytest.raises(MetadataError):
        WRITE_CASES[case]()


# R8: the SQL CHECK constraints mirror the Python digest rule.
@pytest.mark.parametrize(
    "value",
    [
        "sha256:" + "0" * 64,
        "blake3:" + "f" * 64,
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 65,
        "sha256:" + "A" * 64,
        "sha1:" + "0" * 64,
        "SHA256:" + "0" * 64,
        "sha256" + "0" * 64,
        " sha256:" + "0" * 64,
        "sha256:" + "0" * 64 + "\n",
    ],
)
def test_digest_sql_pattern_matches_python_rule(value: str) -> None:
    try:
        Digest.parse(value)
        python_ok = True
    except DigestError:
        python_ok = False
    # PostgreSQL `~` searches and its `$` matches only at the very end (Python's also matches
    # before a final newline), so emulate it with `\Z`. The integration test checks the real one.
    sql_like = DIGEST_SQL_PATTERN.removesuffix("$") + r"\Z"
    assert (re.search(sql_like, value) is not None) == python_ok


DIGEST_COLUMNS = {
    ("builds", "plan_digest"),
    ("actions", "key"),
    ("actions", "result_key"),
    ("action_cache", "key"),
    ("blobs", "digest"),
    ("provenance_edges", "action_key"),
    ("provenance_edges", "object_id"),
    ("provenance_edges", "content_digest"),
    ("toolchains", "id"),
    ("toolchains", "fingerprint"),
}


# R8
def test_every_digest_column_has_a_check() -> None:
    found = set()
    for table in metadata.sorted_tables:
        for c in table.constraints:
            if isinstance(c, sa.CheckConstraint) and DIGEST_SQL_PATTERN in str(c.sqltext):
                col = str(c.sqltext).split(" ", 1)[0]
                found.add((table.name, col))
    assert found == DIGEST_COLUMNS


# R9
def test_config_from_mapping() -> None:
    cfg = MetadataConfig.from_mapping(
        {"url": "postgresql+psycopg://u:secret@db.example.invalid/ebs", "pool_size": 3}
    )
    assert (cfg.pool_size, cfg.max_overflow) == (3, 5)
    store = PgMetadataStore.from_config(cfg, clock=FakeClock())
    try:
        assert store.pool_size == 3
        assert "secret" not in repr(store)
    finally:
        store.close()


# R9
@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({}, "url"),
        ({"url": "sqlite:///x.db"}, "postgresql"),
        ({"url": "postgresql+psycopg://h/db", "pool_size": 0}, "pool_size"),
        ({"url": "postgresql+psycopg://h/db", "colour": 1}, "colour"),
    ],
)
def test_config_errors(section: dict[str, object], match: str) -> None:
    with pytest.raises(ConfigError, match=match) as info:
        MetadataConfig.from_mapping(section)
    assert "[metadata]" in str(info.value)
