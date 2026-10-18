"""`ebs why` over a large provenance graph on PostgreSQL (P2-04 R5).

A synthetic graph of 20 action levels, 500 actions each, every action reading 4 outputs of the
level below and writing one: 10,000 actions and 50,000 edges in one domain. `why` from one
top-level output reaches nearly all of it (fan-in 4 saturates the levels within a few hops),
and must return in under a second (best of 5 runs).
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import ActionRow, BuildCreate
from ebs.meta.models import provenance_edges
from ebs.meta.pg import PgMetadataStore
from ebs.release import provenance as prov
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

LEVELS, WIDTH, FAN_IN = 20, 500, 4
D = "perf"
BUDGET_S = 1.0


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


def _key(level: int, i: int) -> Digest:
    return hash_bytes(f"key {level} {i}".encode())


def _obj(level: int, i: int) -> Digest:
    return hash_bytes(f"object {level} {i}".encode())  # level 0: sources


def _populate(pg: PgMetadataStore) -> int:
    """The graph, written in bulk (the store's write path is not what R5 measures)."""
    rng = random.Random(4)
    build = pg.create_build(
        BuildCreate(
            uuid=uuid4(), domain=D, project="perf", plan_digest=hash_bytes(b"plan"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice",
            cache_mode="write",
        )
    )  # fmt: skip
    pg.add_actions(build, [
        ActionRow(action_id=f"s{level}[i={i}]", step=f"s{level}", key=_key(level, i),
                  rule="questa.sim" if level == LEVELS else "shell", params={"i": i})
        for level in range(1, LEVELS + 1)
        for i in range(WIDTH)
    ])  # fmt: skip
    edges = []
    for level in range(1, LEVELS + 1):
        for i in range(WIDTH):
            key = str(_key(level, i))
            for n, j in enumerate(rng.sample(range(WIDTH), FAN_IN)):
                edges.append({"domain": D, "action_key": key, "direction": "in",
                              "logical_path": f"in{n}", "object_id": str(_obj(level - 1, j)),
                              "content_digest": None})  # fmt: skip
            out = str(_obj(level, i))
            edges.append({"domain": D, "action_key": key, "direction": "out",
                          "logical_path": "out", "object_id": out,
                          "content_digest": out})  # fmt: skip
    with pg.engine.begin() as conn:
        conn.execute(provenance_edges.insert(), edges)
    # Deployed tables have planner statistics (autovacuum); a bulk load needs them explicitly,
    # or the first walk is planned for an empty table.
    with pg.engine.connect() as conn:
        conn.execute(sa.text("ANALYZE ebs.provenance_edges, ebs.actions, ebs.builds"))
        conn.commit()
    return len(edges)


# R5
@pytest.mark.slow
@pytest.mark.perf
def test_why_large_graph_under_a_second(pg: PgMetadataStore) -> None:
    assert _populate(pg) == LEVELS * WIDTH * (FAN_IN + 1) == 50_000
    pg.provenance_upstream(D, ids=[_obj(1, 0)], keys=[], depth=1)  # warm the pool
    # The best of 5: the budget is the query's cost, not other load on a shared test host.
    times = []
    for _ in range(5):
        start = time.perf_counter()
        lineage = prov.why(pg, D, ids=[_obj(LEVELS, 0)])
        times.append(time.perf_counter() - start)
    elapsed = min(times)
    [graph] = lineage.graphs
    steps = {a.step for a in graph.actions}
    assert steps == {f"s{level}" for level in range(1, LEVELS + 1)}  # all 20 levels walked
    assert len(graph.actions) > (LEVELS - 5) * WIDTH * 0.9  # the levels saturate
    assert not lineage.truncated
    assert len(prov.sources(lineage)) > WIDTH * 0.9
    assert elapsed < BUDGET_S, f"why took {elapsed:.2f} s over {len(graph.edges)} edges"
