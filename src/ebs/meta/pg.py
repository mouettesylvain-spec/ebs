"""PostgreSQL MetadataStore (interfaces.md § 6, data-model.md).

Settings come from the `[metadata]` config section (R9). Each public method runs in one short
transaction on a pooled connection, so no transaction ever spans a driver wait. Timestamps come
from the injected clock, not from the database, so behaviour is testable with a FakeClock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, ClassVar, Final

import psycopg
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import get_logger
from ebs.meta._common import (
    aggregate_stats,
    attribution_digests,
    cache_refs,
    check_blobs,
    check_build_filter,
    check_by,
    check_digest,
    check_driver_owner,
    check_event_query,
    check_exportable,
    check_final_plan,
    check_final_status,
    check_lease_ttl,
    check_pin,
    check_release,
    check_release_imports,
    check_result,
    check_run,
    check_set_channel,
    check_stats_window,
    check_toolchain,
    check_walk,
    edge_models,
    edge_rows,
    month_name,
    month_start,
    next_month,
    provenance_graph,
    release_matches,
    result_summary,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
)
from ebs.meta.api import (
    EVENT_PAGE_MAX,
    ActionRow,
    ActionState,
    BlobRef,
    BuildCreate,
    BuildFilter,
    BuildId,
    BuildStatus,
    BuildView,
    ChannelMove,
    ChannelView,
    DomainPolicy,
    DomainStats,
    DriverLease,
    Event,
    ExportRecord,
    GateCheck,
    GcAttribution,
    GcBlob,
    GcCacheEntry,
    GcLock,
    GcRoots,
    GcRun,
    LeaseId,
    ProvenanceAction,
    ProvenanceGraph,
    RegisterOutcome,
    ReleaseCreate,
    ReleaseId,
    ReleaseImport,
    ReleaseView,
    ResultManifest,
    ResultSummary,
    StoredEvent,
    Tombstone,
    ToolchainCreate,
    ToolchainRecord,
    check_domain,
)
from ebs.meta.models import (
    SCHEMA,
    action_cache,
    actions,
    blobs,
    builds,
    channel_history,
    channels,
    domains,
    driver_leases,
    events,
    exports,
    gc_locks,
    gc_runs,
    lease_objects,
    leases,
    pins,
    provenance_edges,
    release_imports,
    releases,
    tombstones,
    toolchain_manifests,
    toolchains,
)

__all__ = ["EVENTS_CHANNEL", "MetadataConfig", "PgMetadataStore"]

EVENTS_CHANNEL: Final = "ebs_events"
"""The NOTIFY channel of event wake-ups; the payload is `<domain> <build id>` (P2-05 R2)."""

_log = get_logger(__name__)

# Provenance walks (P2-04): one recursive CTE each, breadth-first in action levels. A key's level
# is its shortest distance from the start (the min over the paths found); UNION drops repeated
# (key, level) pairs, so a key is expanded at most once per level and the walk stops at `depth`.
# Each returns every edge of the walked keys with the key's level (one row with NULL edge
# fields for a key without edges), in the graph's order: byte order, like Python's sort.
# The joins use the edges' primary key and their (domain, object_id) index.
_WALK_SQL: Final = {
    "upstream": sa.text("""
WITH RECURSIVE walk(key, depth) AS (
    SELECT o.action_key, 1 FROM ebs.provenance_edges o
     WHERE o.domain = :domain AND o.direction = 'out'
       AND o.object_id = ANY(CAST(:ids AS text[]))
    UNION
    SELECT o.action_key, 1 FROM ebs.provenance_edges o
     WHERE o.domain = :domain AND o.direction = 'out'
       AND o.content_digest = ANY(CAST(:ids AS text[]))
    UNION
    SELECT k, 1 FROM unnest(CAST(:keys AS text[])) AS k
    UNION
    SELECT o.action_key, w.depth + 1
      FROM walk w
      JOIN ebs.provenance_edges i
        ON i.domain = :domain AND i.direction = 'in' AND i.action_key = w.key
      JOIN ebs.provenance_edges o
        ON o.domain = :domain AND o.direction = 'out' AND o.object_id = i.object_id
     WHERE w.depth < :depth
),
levels AS (SELECT key, min(depth) AS depth FROM walk GROUP BY key)
SELECT l.key, l.depth, e.direction, e.logical_path, e.object_id, e.content_digest
  FROM levels l
  LEFT JOIN ebs.provenance_edges e ON e.domain = :domain AND e.action_key = l.key
 ORDER BY l.key COLLATE "C", e.direction COLLATE "C", e.logical_path COLLATE "C"
"""),
    "downstream": sa.text("""
WITH RECURSIVE walk(key, depth) AS (
    SELECT i.action_key, 1 FROM ebs.provenance_edges i
     WHERE i.domain = :domain AND i.direction = 'in'
       AND i.object_id = ANY(CAST(:ids AS text[]))
    UNION
    SELECT k, 1 FROM unnest(CAST(:keys AS text[])) AS k
    UNION
    SELECT i.action_key, w.depth + 1
      FROM walk w
      JOIN ebs.provenance_edges o
        ON o.domain = :domain AND o.direction = 'out' AND o.action_key = w.key
      JOIN ebs.provenance_edges i
        ON i.domain = :domain AND i.direction = 'in' AND i.object_id = o.object_id
     WHERE w.depth < :depth
),
levels AS (SELECT key, min(depth) AS depth FROM walk GROUP BY key)
SELECT l.key, l.depth, e.direction, e.logical_path, e.object_id, e.content_digest
  FROM levels l
  LEFT JOIN ebs.provenance_edges e ON e.domain = :domain AND e.action_key = l.key
 ORDER BY l.key COLLATE "C", e.direction COLLATE "C", e.logical_path COLLATE "C"
"""),
}
# The keys one level past `keys`: whether `depth` cut the walk short.
_STEP_SQL: Final = {
    "upstream": sa.text("""
SELECT DISTINCT o.action_key FROM ebs.provenance_edges i
  JOIN ebs.provenance_edges o
    ON o.domain = :domain AND o.direction = 'out' AND o.object_id = i.object_id
 WHERE i.domain = :domain AND i.direction = 'in'
   AND i.action_key = ANY(CAST(:keys AS text[]))"""),
    "downstream": sa.text("""
SELECT DISTINCT i.action_key FROM ebs.provenance_edges o
  JOIN ebs.provenance_edges i
    ON i.domain = :domain AND i.direction = 'in' AND i.object_id = o.object_id
 WHERE o.domain = :domain AND o.direction = 'out'
   AND o.action_key = ANY(CAST(:keys AS text[]))"""),
}

# ActionRow columns; the stored manifest (`result`) is only read by `get_result`.
_ACTION_COLUMNS: Final = tuple(
    c.name for c in actions.columns if c.name not in {"build_id", "result"}
)
# Scalar results below carry explicit annotations: SQLAlchemy 2.1's stubs type
# `scalar_one_or_none()` of an untyped Core select as `None`.


class MetadataConfig(BaseModel):
    """The `[metadata]` section of ebs.toml for direct PostgreSQL access."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    url: Annotated[StrictStr, Field(min_length=1)]
    pool_size: Annotated[StrictInt, Field(ge=1)] = 5
    max_overflow: Annotated[StrictInt, Field(ge=0)] = 5
    pool_timeout_s: Annotated[StrictInt, Field(ge=1)] = 30
    connect_timeout_s: Annotated[StrictInt, Field(ge=1)] = 10

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> MetadataConfig:
        """Validate a parsed `[metadata]` table; raises ConfigError naming the bad key."""
        try:
            cfg = cls.model_validate(dict(section))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(section)'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ConfigError(f"invalid [metadata] configuration: {problems}") from None
        try:
            backend = make_url(cfg.url).get_backend_name()
        except sa.exc.ArgumentError as exc:
            raise ConfigError(f"invalid [metadata].url: {exc}") from None
        if backend != "postgresql":
            raise ConfigError(
                f"[metadata].url must be a postgresql URL "
                f"(postgresql+psycopg://user@host/db), got a {backend!r} URL"
            )
        return cfg


class PgMetadataStore:
    """MetadataStore backed by PostgreSQL through a SQLAlchemy connection pool."""

    APPLICATION_NAME: ClassVar[str] = "ebs-meta"

    def __init__(self, engine: sa.Engine, *, clock: Clock | None = None) -> None:
        self._engine = engine
        self._clock = clock or SystemClock()
        self._where = engine.url.render_as_string(hide_password=True)
        self._event_months: set[datetime] = set()  # partitions this process created or used

    @classmethod
    def from_config(
        cls, config: MetadataConfig | Mapping[str, object], *, clock: Clock | None = None
    ) -> PgMetadataStore:
        cfg = config if isinstance(config, MetadataConfig) else MetadataConfig.from_mapping(config)
        url = make_url(cfg.url).set(drivername="postgresql+psycopg")
        engine = sa.create_engine(
            url,
            pool_size=cfg.pool_size,
            max_overflow=cfg.max_overflow,
            pool_timeout=cfg.pool_timeout_s,
            pool_pre_ping=True,
            connect_args={
                "application_name": cls.APPLICATION_NAME,
                "connect_timeout": cfg.connect_timeout_s,
            },
        )
        return cls(engine, clock=clock)

    @property
    def engine(self) -> sa.Engine:
        """The pooled engine (the metadata service shares it for tokens and readiness)."""
        return self._engine

    @property
    def pool_size(self) -> int:
        pool = self._engine.pool
        return pool.size() if isinstance(pool, sa.pool.QueuePool) else 0

    def close(self) -> None:
        """Close all pooled connections."""
        self._engine.dispose()

    def __repr__(self) -> str:
        return f"PgMetadataStore({self._where})"

    @contextmanager
    def _tx(self, what: str) -> Iterator[sa.Connection]:
        """One short transaction; database errors become MetadataError."""
        try:
            with self._engine.begin() as conn:
                yield conn
        except sa.exc.IntegrityError as exc:
            raise MetadataError(f"{what}: rejected by the metadata database: {exc.orig}") from exc
        except psycopg.IntegrityError as exc:  # raised directly by COPY on the raw cursor
            raise MetadataError(f"{what}: rejected by the metadata database: {exc}") from exc
        except (sa.exc.SQLAlchemyError, psycopg.Error) as exc:
            _log.warning("metadata database error", operation=what, error=str(exc))
            raise MetadataError(
                f"{what}: metadata database error at {self._where}: {exc}. Check [metadata].url "
                "and that the database is reachable and migrated (ebs.meta.migrations)"
            ) from exc

    # --- action cache ---------------------------------------------------------------------------

    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None:
        check_domain(domain)
        check_digest(key, "cache_get key")
        now = self._clock.now()
        t = action_cache
        stmt = (
            sa.update(t)
            .where(t.c.domain == domain, t.c.key == str(key))
            .values(
                hits=t.c.hits + 1,
                last_access=sa.case(
                    (t.c.last_access <= touch_cutoff(now), sa.literal(now, t.c.last_access.type)),
                    else_=t.c.last_access,
                ),
            )
            .returning(t.c.result)
        )
        with self._tx(f"cache_get {domain}/{key}") as conn:
            payload: dict[str, object] | None = conn.execute(stmt).scalar_one_or_none()
        if payload is None:
            return None
        try:
            return ResultManifest.model_validate(payload)
        except ValueError as exc:
            raise MetadataError(
                f"action cache entry {domain}/{key} holds an invalid result manifest; delete "
                f"the entry so the action reruns: {exc}"
            ) from None

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        check_domain(domain)
        check_digest(key, "cache_put key")
        payload = check_result(key, result, f"cache_put {domain}/{key}").to_json()
        now = self._clock.now()
        with self._tx(f"cache_put {domain}/{key}") as conn:
            self._ensure_domain(conn, domain)
            inserted = conn.execute(
                pg_insert(action_cache)
                .values(
                    domain=domain,
                    key=str(key),
                    result=payload,
                    created_at=now,
                    last_access=now,
                    hits=0,
                )
                .on_conflict_do_nothing()
                .returning(action_cache.c.key)
            ).first()
        return inserted is not None

    def cache_replace_failed(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        check_domain(domain)
        check_digest(key, "cache_replace_failed key")
        payload = check_result(key, result, f"cache_replace_failed {domain}/{key}").to_json()
        now = self._clock.now()
        t = action_cache
        with self._tx(f"cache_replace_failed {domain}/{key}") as conn:
            replaced = conn.execute(
                sa.update(t)
                .where(
                    t.c.domain == domain,
                    t.c.key == str(key),
                    t.c.result["status"].astext == "failed",
                )
                .values(result=payload, created_at=now, last_access=now)
                .returning(t.c.key)
            ).first()
        return replaced is not None

    @staticmethod
    def _ensure_domain(conn: sa.Connection, domain: str) -> None:
        conn.execute(pg_insert(domains).values(name=domain).on_conflict_do_nothing())

    # --- builds ---------------------------------------------------------------------------------

    def create_build(self, b: BuildCreate) -> BuildId:
        spec = revalidate(b, BuildCreate, "create_build")
        with self._tx("create_build") as conn:
            self._ensure_domain(conn, spec.domain)
            build_id: int | None = conn.execute(
                pg_insert(builds)
                .values(
                    **spec.model_dump(),
                    status="running",
                    created_at=self._clock.now(),
                    pinned=False,
                )
                .on_conflict_do_nothing(index_elements=[builds.c.uuid])
                .returning(builds.c.id)
            ).scalar_one_or_none()
            if build_id is None:
                raise MetadataError(f"a build with uuid {spec.uuid} already exists")
        return BuildId(build_id)

    @staticmethod
    def _build_status(conn: sa.Connection, build: BuildId, *, lock: bool = False) -> str:
        stmt = sa.select(builds.c.status).where(builds.c.id == build)
        if lock:
            stmt = stmt.with_for_update(read=True)
        status: str | None = conn.execute(stmt).scalar_one_or_none()
        if status is None:
            raise MetadataError(f"no build with id {build}")
        return str(status)

    def add_actions(self, build: BuildId, actions_: Sequence[ActionRow]) -> None:
        rows = [revalidate(a, ActionRow, f"add_actions to build {build}") for a in actions_]
        seen: set[str] = set()
        for row in rows:
            if row.action_id in seen:
                raise MetadataError(f"add_actions: action id {row.action_id!r} appears twice")
            seen.add(row.action_id)
        now = self._clock.now()
        columns = ("build_id", *_ACTION_COLUMNS)
        records = []
        for row in rows:
            values = row.model_dump()
            if values["queued_at"] is None:
                values["queued_at"] = now
            if values["params"] is not None:
                values["params"] = Jsonb(values["params"])  # COPY does not adapt dicts
            records.append((build, *(values[c] for c in _ACTION_COLUMNS)))
        with self._tx(f"add_actions to build {build}") as conn:
            status = self._build_status(conn, build, lock=True)
            if status != "running":
                raise MetadataError(f"build {build} is finished ({status}); cannot add actions")
            if not records:
                return
            existing: str | None = conn.execute(
                sa.select(actions.c.action_id)
                .where(
                    actions.c.build_id == build,
                    actions.c.action_id == sa.any_(sa.literal(sorted(seen), ARRAY(sa.Text))),
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                raise MetadataError(
                    f"build {build} already has an action {existing!r}; action ids must be "
                    "unique within a build"
                )
            # COPY: 20,000 actions in well under the 5 s budget (R6); executemany is ~4x slower.
            # A concurrent add of the same ids passes the check above and fails here with a
            # unique violation, which `_tx` maps to MetadataError.
            copy_sql = f"COPY {actions.fullname} ({', '.join(columns)}) FROM STDIN"
            raw = conn.connection.driver_connection
            with raw.cursor() as cursor, cursor.copy(copy_sql) as copy:  # type: ignore[union-attr]
                for record in records:
                    copy.write_row(record)

    def _locked_action(
        self, conn: sa.Connection, build: BuildId, action_id: str
    ) -> tuple[str, ActionRow]:
        """(domain, row) of an action, locked for update."""
        stmt = (
            sa.select(builds.c.domain, *(actions.c[n] for n in _ACTION_COLUMNS))
            .join(builds, builds.c.id == actions.c.build_id)
            .where(actions.c.build_id == build, actions.c.action_id == action_id)
            .with_for_update(of=actions)
        )
        found = conn.execute(stmt).mappings().first()
        if found is None:
            self._build_status(conn, build)
            raise MetadataError(f"build {build} has no action {action_id!r}")
        return str(found["domain"]), ActionRow.model_validate(
            {n: found[n] for n in _ACTION_COLUMNS}
        )

    def _update_action(self, conn: sa.Connection, build: BuildId, row: ActionRow) -> None:
        values = row.model_dump()
        del values["action_id"]
        conn.execute(
            sa.update(actions)
            .where(actions.c.build_id == build, actions.c.action_id == row.action_id)
            .values(**values)
        )

    def set_action_state(
        self, build: BuildId, action_id: str, state: ActionState, **fields: object
    ) -> None:
        with self._tx(f"set_action_state {build}/{action_id}") as conn:
            _, row = self._locked_action(conn, build, action_id)
            self._update_action(conn, build, transition(row, state, fields, self._clock.now()))

    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None:
        now = self._clock.now()
        with self._tx(f"record_result {build}/{action_id}") as conn:
            domain, row = self._locked_action(conn, build, action_id)
            key = row.key if row.key is not None else result.action_key
            checked = check_result(key, result, f"record_result for action {action_id!r}")
            self._update_action(
                conn, build, row.model_copy(update={"key": key, "result_key": checked.action_key})
            )
            conn.execute(
                sa.update(actions)
                .where(actions.c.build_id == build, actions.c.action_id == action_id)
                .values(result=checked.to_json())
            )
            edges = [
                {
                    "domain": domain,
                    "action_key": str(key),
                    "direction": direction,
                    "logical_path": path,
                    "object_id": object_id,
                    "content_digest": content,
                }
                for direction, path, object_id, content in edge_rows(checked)
            ]
            if edges:
                conn.execute(pg_insert(provenance_edges).on_conflict_do_nothing(), edges)
            self._register(
                conn,
                domain,
                [
                    BlobRef(digest=o.digest, size=o.size, kind=o.type)
                    for o in checked.outputs.values()
                ],
                now,
            )

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        stmt = sa.select(actions.c.result).where(
            actions.c.build_id == build, actions.c.action_id == action_id
        )
        with self._tx(f"get_result {build}/{action_id}") as conn:
            found = conn.execute(stmt).first()
            if found is None:
                self._build_status(conn, build)
                raise MetadataError(f"build {build} has no action {action_id!r}")
        payload: dict[str, object] | None = found[0]
        return None if payload is None else stored_result(payload, build, action_id)

    def finish_build(
        self, build: BuildId, status: BuildStatus, *, final_plan: Digest | None = None
    ) -> None:
        check_final_status(status)
        final_plan = check_final_plan(final_plan)
        with self._tx(f"finish_build {build}") as conn:
            done = conn.execute(
                sa.update(builds)
                .where(builds.c.id == build, builds.c.status == "running")
                .values(
                    status=status,
                    finished_at=self._clock.now(),
                    final_plan_digest=None if final_plan is None else str(final_plan),
                )
                .returning(builds.c.id, builds.c.domain)
            ).first()
            if done is None:
                current = self._build_status(conn, build)
                raise MetadataError(f"build {build} is already finished ({current})")
            # A finished build holds no lease; a pin is a separate root.
            conn.execute(sa.delete(leases).where(leases.c.build_id == build))
            _notify(conn, str(done.domain), build)  # its event streams end (P2-05 R2)

    def record_release_imports(self, build: BuildId, imports: Sequence[ReleaseImport]) -> None:
        ri = release_imports
        with self._tx(f"record_release_imports {build}") as conn:
            domain: str | None = conn.execute(
                sa.select(builds.c.domain).where(builds.c.id == int(build)).with_for_update()
            ).scalar_one_or_none()
            if domain is None:
                raise MetadataError(f"no build with id {build}")
            ids = sorted({int(r.release) for r in imports})
            found = {int(v.id): v for v in self._select_releases(conn, releases.c.id.in_(ids))}
            recorded = {
                str(r.alias): int(r.release_id)
                for r in conn.execute(sa.select(ri).where(ri.c.build_id == int(build)))
            }
            new = check_release_imports(
                build, str(domain), imports, {i: found.get(i) for i in ids}, recorded
            )
            if new:
                conn.execute(
                    sa.insert(ri),
                    [
                        {"build_id": int(build), "alias": r.alias, "release_id": int(r.release)}
                        for r in new
                    ],
                )

    def list_release_imports(self, build: BuildId) -> list[ReleaseImport]:
        ri = release_imports
        with self._tx(f"list_release_imports {build}") as conn:
            self._build_status(conn, build)
            rows = conn.execute(
                sa.select(ri).where(ri.c.build_id == int(build)).order_by(ri.c.alias)
            ).all()
        return [
            ReleaseImport(alias=str(r.alias), release=ReleaseId(int(r.release_id))) for r in rows
        ]

    def get_build(self, build: BuildId) -> BuildView:
        with self._tx(f"get_build {build}") as conn:
            found = conn.execute(sa.select(builds).where(builds.c.id == build)).mappings().first()
            if found is None:
                raise MetadataError(f"no build with id {build}")
            counts = conn.execute(
                sa.select(actions.c.state, sa.func.count())
                .where(actions.c.build_id == build)
                .group_by(actions.c.state)
            ).all()
        return BuildView.model_validate(
            {**found, "action_counts": {str(s): int(n) for s, n in counts}}
        )

    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]:
        stmt = (
            sa.select(*(actions.c[n] for n in _ACTION_COLUMNS))
            .where(actions.c.build_id == build)
            .order_by(actions.c.action_id.collate("C"))  # code point order, like Python
        )
        if state is not None:
            stmt = stmt.where(actions.c.state == state)
        with self._tx(f"list_actions {build}") as conn:
            self._build_status(conn, build)
            found = conn.execute(stmt).mappings().all()
        return [ActionRow.model_validate(dict(r)) for r in found]

    # --- dashboard reads (P2-06) ----------------------------------------------------------------

    def list_builds(self, domain: str, flt: BuildFilter) -> list[BuildView]:
        checked = check_build_filter(domain, flt)
        stmt = sa.select(builds).where(builds.c.domain == domain)
        for column, value in (
            (builds.c.user_name, checked.user),
            (builds.c.status, checked.status),
            (builds.c.project, checked.project),
        ):
            if value is not None:
                stmt = stmt.where(column == value)
        if checked.since is not None:
            stmt = stmt.where(builds.c.created_at >= checked.since)
        if checked.until is not None:
            stmt = stmt.where(builds.c.created_at < checked.until)
        with self._tx(f"list_builds {domain}") as conn:
            found = conn.execute(stmt.order_by(builds.c.id.desc())).mappings().all()
            counts: dict[int, dict[str, int]] = {}
            if found:
                for build_id, state, n in conn.execute(
                    sa.select(actions.c.build_id, actions.c.state, sa.func.count())
                    .where(actions.c.build_id.in_([int(r["id"]) for r in found]))
                    .group_by(actions.c.build_id, actions.c.state)
                ).all():
                    counts.setdefault(int(build_id), {})[str(state)] = int(n)
        return [
            BuildView.model_validate({**r, "action_counts": counts.get(int(r["id"]), {})})
            for r in found
        ]

    def result_summaries(self, build: BuildId, *, step: str | None = None) -> list[ResultSummary]:
        r = actions.c.result
        stmt = (
            sa.select(
                actions.c.action_id,
                r["status"].astext,
                r["exit_code"],
                r["log"],
                r["summary"],
            )
            .where(actions.c.build_id == build, r.is_not(None))
            .order_by(actions.c.action_id.collate("C"))
        )
        if step is not None:
            stmt = stmt.where(actions.c.step == step)
        with self._tx(f"result_summaries {build}") as conn:
            self._build_status(conn, build)
            rows = conn.execute(stmt).all()
        return [
            result_summary(
                str(a), {"status": st, "exit_code": code, "log": log, "summary": summary}
            )
            for a, st, code, log, summary in rows
        ]

    def domain_stats(self, domain: str, *, since: datetime, until: datetime) -> DomainStats:
        check_stats_window(domain, since, until)
        window = sa.and_(
            builds.c.domain == domain, builds.c.created_at >= since, builds.c.created_at < until
        )
        chosen = sa.select(builds.c.id).where(window)
        ev, pend = events, events.alias("pend")
        waited = sa.exists().where(
            pend.c.build_id == ev.c.build_id,
            pend.c.action_id == ev.c.action_id,
            pend.c.type == "pending",
        )
        with self._tx(f"domain_stats {domain}") as conn:
            n_builds = int(
                conn.execute(
                    sa.select(sa.func.count()).select_from(builds).where(window)
                ).scalar_one()
            )
            rows = conn.execute(
                sa.select(actions.c.step, actions.c.state, actions.c.result["resources"]).where(
                    actions.c.build_id.in_(chosen)
                )
            ).all()
            found = conn.execute(
                sa.select(ev.c.build_id, ev.c.ts, ev.c.type, ev.c.action_id, ev.c.data).where(
                    ev.c.build_id.in_(chosen), ev.c.action_id.is_not(None), waited
                )
            ).all()
            now = self._clock.now()
        return aggregate_stats(
            domain,
            since=since,
            until=until,
            now=now,
            builds=n_builds,
            actions=[
                (str(step), str(state), None if usage is None else {"resources": usage})
                for step, state, usage in rows
            ],
            events=[
                Event.model_validate(
                    {
                        "ts": r.ts,
                        "build": int(r.build_id),
                        "type": r.type,
                        "action_id": r.action_id,
                        "data": r.data,
                    }
                )
                for r in found
            ],
        )

    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None:
        check_domain(domain)
        wanted = str(check_digest(object_id, "resolve_output object_id"))
        with self._tx(f"resolve_output {domain}") as conn:
            # Uses the (domain, object_id) index. An `out` edge is never overwritten (ON CONFLICT
            # DO NOTHING), so a nondeterministic id has one row: the first recorded bytes.
            content = conn.execute(
                sa.select(provenance_edges.c.content_digest)
                .where(
                    provenance_edges.c.domain == domain,
                    provenance_edges.c.object_id == wanted,
                    provenance_edges.c.direction == "out",
                )
                .limit(1)
            ).scalar_one_or_none()
        return None if content is None else Digest.parse(content)

    # --- access tracking -------------------------------------------------------------------------

    def touch(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        keys = sorted({str(check_digest(d, "touch digest")) for d in digests})
        if not keys:
            return
        now = self._clock.now()
        with self._tx(f"touch {domain}") as conn:
            conn.execute(
                sa.update(blobs)
                .where(
                    blobs.c.domain == domain,
                    blobs.c.digest == sa.any_(sa.literal(keys, ARRAY(sa.Text))),
                    blobs.c.last_access <= touch_cutoff(now),
                )
                .values(last_access=now)
            )

    @staticmethod
    def _register(conn: sa.Connection, domain: str, refs: Sequence[BlobRef], now: datetime) -> None:
        """Insert blob rows, touching known ones: storing an object again is a use (R1)."""
        if not refs:
            return
        rows = {
            str(r.digest): {
                "domain": domain,
                "digest": str(r.digest),
                "size": r.size,
                "kind": r.kind,
                "created_at": now,
                "last_access": now,
            }
            for r in refs
        }
        stmt = pg_insert(blobs)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[blobs.c.domain, blobs.c.digest],
                set_={"last_access": stmt.excluded.last_access},
                where=blobs.c.last_access <= touch_cutoff(now),
            ),
            [rows[k] for k in sorted(rows)],  # one lock order for concurrent writers
        )

    @staticmethod
    def _touch_rows(conn: sa.Connection, domain: str, keys: Iterable[str], now: datetime) -> None:
        """Touch known blob rows in this transaction: a new lease or pin is a use, so a GC claim
        racing with it (which locks the row first) fails (I15)."""
        wanted = sorted(set(keys))
        if wanted:
            conn.execute(
                sa.update(blobs)
                .where(
                    blobs.c.domain == domain,
                    blobs.c.digest == sa.any_(sa.literal(wanted, ARRAY(sa.Text))),
                    blobs.c.last_access <= touch_cutoff(now),
                )
                .values(last_access=now)
            )

    def register_blobs(self, domain: str, blobs_: Iterable[BlobRef]) -> None:
        check_domain(domain)
        refs = check_blobs(blobs_)
        if not refs:
            return
        with self._tx(f"register_blobs {domain}") as conn:
            self._ensure_domain(conn, domain)
            self._register(conn, domain, refs, self._clock.now())

    # --- leases and pins ------------------------------------------------------------------------

    @staticmethod
    def _add_lease_objects(conn: sa.Connection, lease: int, keys: set[str]) -> None:
        if keys:
            conn.execute(
                pg_insert(lease_objects).on_conflict_do_nothing(),
                [{"lease_id": lease, "digest": k} for k in sorted(keys)],
            )

    def acquire_lease(
        self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int
    ) -> LeaseId:
        check_domain(domain)
        check_lease_ttl(ttl_s)
        keys = {str(check_digest(d, "acquire_lease digest")) for d in digests}
        now = self._clock.now()
        with self._tx(f"acquire_lease for build {build}") as conn:
            found = conn.execute(
                sa.select(builds.c.domain, builds.c.status)
                .where(builds.c.id == build)
                .with_for_update(read=True)
            ).first()
            if found is None:
                raise MetadataError(f"no build with id {build}")
            if found.domain != domain:
                raise MetadataError(
                    f"acquire_lease: build {build} is in domain {found.domain!r}, not {domain!r}"
                )
            if found.status != "running":
                raise MetadataError(
                    f"acquire_lease: build {build} is finished ({found.status}); only running "
                    "builds hold leases"
                )
            lease: int = conn.execute(
                sa.insert(leases)
                .values(
                    domain=domain,
                    build_id=build,
                    expires_at=now + timedelta(seconds=ttl_s),
                    created_at=now,
                )
                .returning(leases.c.id)
            ).scalar_one()
            self._add_lease_objects(conn, lease, keys)
            self._touch_rows(conn, domain, keys, now)
        return LeaseId(lease)

    def renew_lease(
        self, build: BuildId, lease: LeaseId, ttl_s: int, *, add: Iterable[Digest] = ()
    ) -> None:
        check_lease_ttl(ttl_s)
        keys = {str(check_digest(d, "renew_lease digest")) for d in add}
        now = self._clock.now()
        with self._tx(f"renew_lease {lease}") as conn:
            renewed = conn.execute(
                sa.update(leases)
                .where(leases.c.id == lease, leases.c.build_id == build, leases.c.expires_at > now)
                .values(expires_at=now + timedelta(seconds=ttl_s))
                .returning(leases.c.id)
            ).first()
            if renewed is None:
                raise MetadataError(
                    f"renew_lease: build {build} holds no live lease {lease} (it expired or was "
                    "released); acquire a new lease"
                )
            self._add_lease_objects(conn, lease, keys)
            domain: str = conn.execute(
                sa.select(leases.c.domain).where(leases.c.id == lease)
            ).scalar_one()
            self._touch_rows(conn, domain, keys, now)

    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease:
        check_driver_owner(owner, "claim_driver")
        check_lease_ttl(ttl_s)
        now = self._clock.now()
        d = driver_leases
        with self._tx(f"claim_driver for build {build}") as conn:
            found = conn.execute(  # a share lock: finish_build waits for this claim
                sa.select(builds.c.status).where(builds.c.id == build).with_for_update(read=True)
            ).first()
            if found is None:
                raise MetadataError(f"no build with id {build}")
            if found.status != "running":
                raise MetadataError(
                    f"claim_driver: build {build} is finished ({found.status}); only running "
                    "builds have a driver"
                )
            stmt = pg_insert(d).values(
                build_id=build,
                owner=owner,
                expires_at=now + timedelta(seconds=ttl_s),
                claimed_at=now,
            )
            # Concurrent claims serialize on the row; each re-checks the condition on the
            # version the previous one wrote, so at most one owner wins an expired lease.
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[d.c.build_id],
                    set_={
                        "owner": stmt.excluded.owner,
                        "expires_at": stmt.excluded.expires_at,
                        "claimed_at": sa.case(
                            (d.c.owner == stmt.excluded.owner, d.c.claimed_at),
                            else_=stmt.excluded.claimed_at,
                        ),
                    },
                    where=sa.or_(d.c.expires_at <= now, d.c.owner == stmt.excluded.owner),
                )
            )
            row = conn.execute(
                sa.select(d.c.owner, d.c.expires_at).where(d.c.build_id == build)
            ).one()
        return DriverLease(build=build, owner=row.owner, expires_at=row.expires_at)

    def release_driver(self, build: BuildId, owner: str) -> None:
        check_driver_owner(owner, "release_driver")
        with self._tx(f"release_driver for build {build}") as conn:
            conn.execute(
                sa.delete(driver_leases).where(
                    driver_leases.c.build_id == build, driver_leases.c.owner == owner
                )
            )

    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None:
        by, reason = check_pin(by, reason)
        with self._tx(f"pin_build {build}") as conn:
            domain: str | None = conn.execute(
                sa.update(builds)
                .where(builds.c.id == build)
                .values(pinned=True)
                .returning(builds.c.domain)
            ).scalar_one_or_none()
            if domain is None:
                raise MetadataError(f"no build with id {build}")
            conn.execute(
                pg_insert(pins)
                .values(
                    domain=domain,
                    build_id=build,
                    pinned_by=by,
                    reason=reason,
                    created_at=self._clock.now(),
                )
                .on_conflict_do_nothing()
            )
            results: Iterable[object] = conn.execute(
                sa.select(actions.c.result).where(
                    actions.c.build_id == build, actions.c.result.is_not(None)
                )
            ).scalars()
            roots = set(self._plans_of(conn, build))
            for payload in results:
                roots |= {str(d) for d in attribution_digests(payload)}
            self._touch_rows(conn, domain, roots, self._clock.now())

    @staticmethod
    def _plans_of(conn: sa.Connection, build: BuildId) -> list[str]:
        row = conn.execute(
            sa.select(builds.c.plan_digest, builds.c.final_plan_digest).where(builds.c.id == build)
        ).one()
        return [str(d) for d in row if d is not None]

    def unpin_build(self, build: BuildId) -> bool:
        with self._tx(f"unpin_build {build}") as conn:
            found = conn.execute(
                sa.update(builds)
                .where(builds.c.id == build)
                .values(pinned=False)
                .returning(builds.c.id)
            ).first()
            if found is None:
                raise MetadataError(f"no build with id {build}")
            removed = conn.execute(
                sa.delete(pins).where(pins.c.build_id == build).returning(pins.c.build_id)
            ).first()
        return removed is not None

    # --- releases and channels (P2-02) -----------------------------------------------------------

    _RELEASE_COLUMNS: ClassVar[tuple[str, ...]] = (
        "id", "domain", "name", "version", "build_id", "outputs", "notes", "allow_failed",
        "exported_from", "created_at", "created_by", "yanked_at", "yanked_by", "yank_reason",
    )  # fmt: skip

    @classmethod
    def _release_view(cls, row: sa.RowMapping) -> ReleaseView:
        doc = {("build" if k == "build_id" else k): row[k] for k in cls._RELEASE_COLUMNS}
        try:
            return ReleaseView.model_validate(doc)
        except ValidationError as exc:
            raise MetadataError(f"release {row['id']} is stored invalid: {exc}") from None

    def _select_releases(
        self, conn: sa.Connection, *where: sa.ColumnElement[bool]
    ) -> list[ReleaseView]:
        rows = (
            conn.execute(
                sa.select(*(releases.c[c] for c in self._RELEASE_COLUMNS))
                .where(*where)
                .order_by(releases.c.name, releases.c.created_at, releases.c.id)
            )
            .mappings()
            .all()
        )
        return [self._release_view(r) for r in rows]

    def _insert_release(
        self, conn: sa.Connection, spec: ReleaseCreate, by: str, *, exported_from: int | None
    ) -> ReleaseView:
        self._ensure_domain(conn, spec.domain)
        dumped = spec.model_dump(mode="json")
        release_id: int | None = conn.execute(
            pg_insert(releases)
            .values(
                domain=spec.domain,
                name=spec.name,
                version=spec.version,
                build_id=spec.build,
                outputs=dumped["outputs"],
                notes=spec.notes,
                allow_failed=spec.allow_failed,
                exported_from=exported_from,
                created_at=self._clock.now(),
                created_by=by,
            )
            .on_conflict_do_nothing(
                index_elements=[releases.c.domain, releases.c.name, releases.c.version]
            )
            .returning(releases.c.id)
        ).scalar_one_or_none()
        if release_id is None:
            raise MetadataError(
                f"release {spec.name} {spec.version} already exists in domain {spec.domain!r}; "
                "releases are immutable, so publish a new version"
            )
        return self._select_releases(conn, releases.c.id == release_id)[0]

    def create_release(self, spec: ReleaseCreate, *, by: str) -> ReleaseView:
        checked = revalidate(spec, ReleaseCreate, "create_release")
        by = check_by(by, "create_release")
        build = checked.build
        with self._tx(f"create_release {checked.name} {checked.version}") as conn:
            self._build_status(conn, build, lock=True)  # finish_build waits for this release
            found = conn.execute(sa.select(builds).where(builds.c.id == build)).mappings().one()
            view = BuildView.model_validate({**found, "action_counts": {}})
            rows = (
                conn.execute(
                    sa.select(*(actions.c[n] for n in _ACTION_COLUMNS), actions.c.result).where(
                        actions.c.build_id == build
                    )
                )
                .mappings()
                .all()
            )
            actions_ = {
                str(r["action_id"]): ActionRow.model_validate({n: r[n] for n in _ACTION_COLUMNS})
                for r in rows
            }
            results = {
                str(r["action_id"]): stored_result(r["result"], build, str(r["action_id"]))
                for r in rows
                if r["result"] is not None
            }
            check_release(checked, view, actions_, results)
            return self._insert_release(conn, checked, by, exported_from=None)

    def get_release(self, domain: str, name: str, version: str) -> ReleaseView | None:
        check_domain(domain)
        with self._tx(f"get_release {name} {version}") as conn:
            found = self._select_releases(
                conn,
                releases.c.domain == domain,
                releases.c.name == name,
                releases.c.version == version,
            )
        return found[0] if found else None

    def get_release_by_id(self, release: ReleaseId) -> ReleaseView | None:
        with self._tx(f"get_release {release}") as conn:
            found = self._select_releases(conn, releases.c.id == int(release))
        return found[0] if found else None

    def list_releases(self, domain: str, *, name: str | None = None) -> list[ReleaseView]:
        check_domain(domain)
        where = [releases.c.domain == domain]
        if name is not None:
            where.append(releases.c.name == name)
        with self._tx(f"list_releases {domain}") as conn:
            return self._select_releases(conn, *where)

    def yank_release(self, release: ReleaseId, *, by: str, reason: str) -> ReleaseView:
        by = check_by(by, "yank_release")
        if not isinstance(reason, str) or not reason or len(reason) > 4096:
            raise MetadataError("yank_release: give a reason (1-4096 characters)")
        with self._tx(f"yank_release {release}") as conn:
            conn.execute(
                sa.update(releases)
                .where(releases.c.id == int(release), releases.c.yanked_at.is_(None))
                .values(yanked_at=self._clock.now(), yanked_by=by, yank_reason=reason)
            )
            found = self._select_releases(conn, releases.c.id == int(release))
        if not found:
            raise MetadataError(f"yank_release: no release with id {release}")
        return found[0]

    @staticmethod
    def _move(row: sa.RowMapping, domain: str, name: str, channel: str) -> ChannelMove:
        gate = (
            None
            if row["gate_flow"] is None
            else GateCheck(flow=str(row["gate_flow"]), build=BuildId(int(row["gate_build_id"])))
        )
        return ChannelMove(
            id=int(row["id"]),
            domain=domain,
            name=name,
            channel=channel,
            release=ReleaseId(int(row["release_id"])),
            version=str(row["version"]),
            moved_at=row["moved_at"],
            moved_by=str(row["moved_by"]),
            gate=gate,
        )

    def set_channel(
        self,
        domain: str,
        name: str,
        channel: str,
        release: ReleaseId,
        *,
        by: str,
        gate: GateCheck | None = None,
    ) -> ChannelMove:
        by = check_by(by, "set_channel")
        checked_gate = None if gate is None else revalidate(gate, GateCheck, "set_channel gate")
        now = self._clock.now()
        with self._tx(f"set_channel {name}:{channel}") as conn:
            # Lock the release row first: a concurrent yank either precedes this check or waits.
            conn.execute(
                sa.select(releases.c.id).where(releases.c.id == int(release)).with_for_update()
            )
            found = self._select_releases(conn, releases.c.id == int(release))
            view = found[0] if found else None
            check_set_channel(view, domain, name, channel)
            assert view is not None
            # Serialize moves of one channel: history order = commit order, and the pointer is
            # always the channel's last move.
            conn.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(f"ebs.channel:{domain}/{name}:{channel}", 0)
                    )
                )
            )
            move_id: int = conn.execute(
                sa.insert(channel_history)
                .values(
                    domain=domain,
                    name=name,
                    channel=channel,
                    release_id=view.id,
                    moved_at=now,
                    moved_by=by,
                    gate_flow=None if checked_gate is None else checked_gate.flow,
                    gate_build_id=None if checked_gate is None else checked_gate.build,
                )
                .returning(channel_history.c.id)
            ).scalar_one()
            values = {
                "release_id": view.id,
                "move_id": move_id,
                "updated_at": now,
                "updated_by": by,
            }
            conn.execute(
                pg_insert(channels)
                .values(domain=domain, name=name, channel=channel, **values)
                .on_conflict_do_update(index_elements=list(channels.primary_key), set_=values)
            )
        return ChannelMove(
            id=move_id, domain=domain, name=name, channel=channel, release=view.id,
            version=view.version, moved_at=now, moved_by=by, gate=checked_gate,
        )  # fmt: skip

    def get_channel(self, domain: str, name: str, channel: str) -> ChannelView | None:
        check_domain(domain)
        c = channels
        with self._tx(f"get_channel {name}:{channel}") as conn:
            found = conn.execute(
                sa.select(c.c.release_id, c.c.updated_at, c.c.updated_by).where(
                    c.c.domain == domain, c.c.name == name, c.c.channel == channel
                )
            ).first()
            if found is None:
                return None
            view = self._select_releases(conn, releases.c.id == found.release_id)[0]
        return ChannelView(
            domain=domain, name=name, channel=channel, release=view,
            updated_at=found.updated_at, updated_by=str(found.updated_by),
        )  # fmt: skip

    def channel_history(self, domain: str, name: str, channel: str) -> list[ChannelMove]:
        check_domain(domain)
        h = channel_history
        with self._tx(f"channel_history {name}:{channel}") as conn:
            rows = (
                conn.execute(
                    sa.select(h, releases.c.version)
                    .join(releases, releases.c.id == h.c.release_id)
                    .where(h.c.domain == domain, h.c.name == name, h.c.channel == channel)
                    .order_by(h.c.id)
                )
                .mappings()
                .all()
            )
        return [self._move(r, domain, name, channel) for r in rows]

    def gate_builds(self, domain: str, flow: str, ids: Iterable[Digest]) -> list[BuildId]:
        check_domain(domain)
        wanted = sorted({str(check_digest(d, "gate_builds id")) for d in ids})
        e = provenance_edges
        stmt = (
            sa.select(builds.c.id)
            .distinct()
            .select_from(e)
            .join(actions, actions.c.key == e.c.action_key)
            .join(builds, builds.c.id == actions.c.build_id)
            .where(
                e.c.domain == domain,
                e.c.direction == "in",
                e.c.object_id.in_(wanted),
                builds.c.domain == domain,
                builds.c.project == flow,
                builds.c.status == "passed",
            )
            .order_by(builds.c.id.desc())
        )
        with self._tx(f"gate_builds {domain}/{flow}") as conn:
            found: Sequence[int] = conn.execute(stmt).scalars().all() if wanted else []
        return [BuildId(int(b)) for b in found]

    # --- provenance (ProvenanceStore, P2-04) -----------------------------------------------------

    def provenance_upstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("upstream", domain, ids, keys, depth)

    def provenance_downstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("downstream", domain, ids, keys, depth)

    def _walk(
        self, way: str, domain: str, ids: Iterable[Digest], keys: Iterable[Digest], depth: object
    ) -> ProvenanceGraph:
        domain, id_list, key_list, levels = check_walk(
            domain, ids, keys, depth, f"provenance_{way}"
        )
        params = {"domain": domain, "ids": id_list, "keys": key_list, "depth": levels}
        a, b = actions, builds
        with self._tx(f"provenance_{way} {domain}") as conn:
            found = conn.execute(_WALK_SQL[way], params).all()
            level = {str(r[0]): r[1] for r in found}
            walked = sorted(level)
            last = [k for k, d in level.items() if d == levels]
            beyond: Sequence[str] = (
                conn.execute(_STEP_SQL[way], {"domain": domain, "keys": last}).scalars().all()
                if last
                else []
            )
            edge_rows_ = [(r[0], r[2], r[3], r[4], r[5]) for r in found if r[2] is not None]
            in_walk = sa.literal(walked, ARRAY(sa.Text))
            action_rows = (
                conn.execute(
                    sa.select(
                        a.c.key,
                        a.c.build_id,
                        a.c.action_id,
                        a.c.step,
                        a.c.rule,
                        a.c.params,
                        a.c.toolchain,
                        a.c.state,
                        a.c.cached,
                        a.c.finished_at,
                        a.c.result["status"].astext.label("status"),
                        b.c.project,
                        b.c.flow_repo,
                        b.c.flow_commit,
                        b.c.flow_dirty,
                        b.c.user_name,
                        b.c.created_at.label("build_created_at"),
                    )
                    .join(b, b.c.id == a.c.build_id)
                    .where(b.c.domain == domain, a.c.key == sa.any_(in_walk))
                    .order_by(a.c.build_id, a.c.action_id.collate("C"))
                )
                .mappings()
                .all()
            )
            objects = sorted(set(id_list) | {r[3] for r in edge_rows_})
            held: sa.ColumnElement[bool] = sa.text(  # type: ignore[assignment]
                "EXISTS (SELECT 1 FROM jsonb_each(ebs.releases.outputs) AS o "
                "WHERE o.value->>'id' = ANY(:objects))"
            ).bindparams(sa.bindparam("objects", objects, type_=ARRAY(sa.Text)))
            rels = self._select_releases(conn, releases.c.domain == domain, held)
        edges = edge_models(edge_rows_, f"provenance_{way} {domain}")
        rows = [
            ProvenanceAction.model_validate(
                {**{k: v for k, v in r.items() if k != "build_id"}, "build": r["build_id"],
                 "key": Digest.parse(r["key"]),
                 "toolchain": None if r["toolchain"] is None else Digest.parse(r["toolchain"])}
            )
            for r in action_rows
        ]  # fmt: skip
        matched = [m for rel in rels for m in release_matches(rel, set(objects))]
        truncated = bool(set(beyond) - set(walked))
        return provenance_graph(domain, edges, rows, matched, truncated, presorted=True)

    def record_export(self, release: ReleaseId, to_domain: str, *, by: str) -> ReleaseView:
        check_domain(to_domain)
        by = check_by(by, "record_export")
        with self._tx(f"record_export {release} to {to_domain}") as conn:
            found = self._select_releases(conn, releases.c.id == int(release))
            if not found:
                raise MetadataError(f"record_export: no release with id {release}")
            source = found[0]
            check_exportable(source)
            if source.domain == to_domain:
                raise MetadataError(
                    f"record_export: release {release} is already in domain {to_domain!r}"
                )
            spec = ReleaseCreate.model_validate(
                {**source.model_dump(include=set(ReleaseCreate.model_fields)), "domain": to_domain}
            )
            exported = self._insert_release(conn, spec, by, exported_from=source.id)
            conn.execute(
                sa.insert(exports).values(
                    release_id=source.id,
                    exported_release_id=exported.id,
                    to_domain=to_domain,
                    exported_at=self._clock.now(),
                    exported_by=by,
                )
            )
        return exported

    def list_exports(self, release: ReleaseId) -> list[ExportRecord]:
        x = exports
        with self._tx(f"list_exports {release}") as conn:
            rows = conn.execute(
                sa.select(x)
                .where(
                    sa.or_(x.c.release_id == int(release), x.c.exported_release_id == int(release))
                )
                .order_by(x.c.id)
            ).all()
        return [
            ExportRecord(
                id=int(r.id),
                release=ReleaseId(int(r.release_id)),
                exported=ReleaseId(int(r.exported_release_id)),
                to_domain=str(r.to_domain),
                exported_at=r.exported_at,
                exported_by=str(r.exported_by),
            )
            for r in rows
        ]

    # --- garbage collection (GcStore) -----------------------------------------------------------

    def gc_policy(self, domain: str) -> DomainPolicy:
        check_domain(domain)
        d = domains
        with self._tx(f"gc_policy {domain}") as conn:
            found = conn.execute(
                sa.select(d.c.ttl_days, d.c.quota_bytes, d.c.high_water, d.c.low_water).where(
                    d.c.name == domain
                )
            ).first()
        if found is None:
            return DomainPolicy()
        return DomainPolicy(
            ttl_days=int(found.ttl_days),
            quota_bytes=None if found.quota_bytes is None else int(found.quota_bytes),
            high_water=float(found.high_water),
            low_water=float(found.low_water),
        )

    def gc_blobs(self, domain: str) -> list[GcBlob]:
        check_domain(domain)
        b = blobs
        with self._tx(f"gc_blobs {domain}") as conn:
            rows = conn.execute(
                sa.select(b.c.digest, b.c.size, b.c.kind, b.c.created_at, b.c.last_access)
                .where(b.c.domain == domain)
                .order_by(b.c.digest)
            ).all()
        return [
            GcBlob(Digest.parse(r.digest), int(r.size), str(r.kind), r.created_at, r.last_access)
            for r in rows
        ]

    def gc_cache_entries(self, domain: str) -> list[GcCacheEntry]:
        check_domain(domain)
        t = action_cache
        with self._tx(f"gc_cache_entries {domain}") as conn:
            rows = conn.execute(
                sa.select(t.c.key, t.c.last_access, t.c.result)
                .where(t.c.domain == domain)
                .order_by(t.c.key)
            ).all()
        return [
            GcCacheEntry(Digest.parse(r.key), r.last_access, *cache_refs(r.result)) for r in rows
        ]

    def gc_roots(self, domain: str, now: datetime) -> GcRoots:
        check_domain(domain)
        with self._tx(f"gc_roots {domain}") as conn:
            pinned_builds = conn.execute(
                sa.select(builds.c.plan_digest, builds.c.final_plan_digest, actions.c.result)
                .select_from(pins)
                .join(builds, builds.c.id == pins.c.build_id)
                .outerjoin(actions, actions.c.build_id == builds.c.id)
                .where(pins.c.domain == domain)
            ).all()
            leased: Sequence[str] = (
                conn.execute(
                    sa.select(lease_objects.c.digest)
                    .distinct()
                    .join(leases, leases.c.id == lease_objects.c.lease_id)
                    .where(leases.c.domain == domain, leases.c.expires_at > now)
                )
                .scalars()
                .all()
            )
            released_outputs: Sequence[dict[str, dict[str, str]]] = (
                conn.execute(sa.select(releases.c.outputs).where(releases.c.domain == domain))
                .scalars()
                .all()
            )
        released = {o["digest"] for outputs in released_outputs for o in outputs.values()}
        pinned: set[Digest] = set()
        for plan_digest, final_plan, result in pinned_builds:
            pinned.add(Digest.parse(plan_digest))
            if final_plan is not None:
                pinned.add(Digest.parse(final_plan))
            pinned.update(attribution_digests(result))
        return GcRoots(
            frozenset(pinned),
            frozenset(Digest.parse(k) for k in leased),
            frozenset(Digest.parse(k) for k in released),
        )

    def gc_tombstones(self, domain: str) -> list[Tombstone]:
        check_domain(domain)
        t = tombstones
        with self._tx(f"gc_tombstones {domain}") as conn:
            rows = conn.execute(
                sa.select(t.c.digest, t.c.deleted_rows_at, t.c.blob_delete_after)
                .where(t.c.domain == domain)
                .order_by(t.c.digest)
            ).all()
        return [
            Tombstone(Digest.parse(r.digest), r.deleted_rows_at, r.blob_delete_after) for r in rows
        ]

    def gc_attribution(self, domain: str) -> list[GcAttribution]:
        check_domain(domain)
        with self._tx(f"gc_attribution {domain}") as conn:
            rows = conn.execute(
                sa.select(builds.c.id, builds.c.project, actions.c.step, actions.c.result)
                .join(actions, actions.c.build_id == builds.c.id)
                .where(builds.c.domain == domain, actions.c.result.is_not(None))
                .order_by(builds.c.id, actions.c.action_id.collate("C"))
            ).all()
        return [
            GcAttribution(d, BuildId(int(r.id)), str(r.project), str(r.step))
            for r in rows
            for d in attribution_digests(r.result)
        ]

    def gc_expire_leases(self, domain: str, now: datetime) -> int:
        check_domain(domain)
        with self._tx(f"gc_expire_leases {domain}") as conn:
            gone = conn.execute(
                sa.delete(leases)
                .where(leases.c.domain == domain, leases.c.expires_at <= now)
                .returning(leases.c.id)
            ).all()
        return len(gone)

    def gc_delete_cache_entries(
        self, domain: str, keys: Iterable[Digest], *, unused_before: datetime
    ) -> list[Digest]:
        check_domain(domain)
        wanted = sorted({str(check_digest(k, "gc_delete_cache_entries key")) for k in keys})
        if not wanted:
            return []
        t = action_cache
        with self._tx(f"gc_delete_cache_entries {domain}") as conn:
            # The condition is re-checked under the row lock: a hit since the snapshot moved
            # `last_access` past the cutoff (by more than the touch interval), so it stays.
            deleted: Sequence[str] = (
                conn.execute(
                    sa.delete(t)
                    .where(
                        t.c.domain == domain,
                        t.c.key == sa.any_(sa.literal(wanted, ARRAY(sa.Text))),
                        t.c.last_access <= unused_before,
                    )
                    .returning(t.c.key)
                )
                .scalars()
                .all()
            )
        return [Digest.parse(k) for k in sorted(deleted)]

    def gc_tombstone(
        self, domain: str, digests: Iterable[Digest], *, now: datetime, delete_after: datetime
    ) -> None:
        check_domain(domain)
        wanted = sorted({str(check_digest(d, "gc_tombstone digest")) for d in digests})
        if not wanted:
            return
        with self._tx(f"gc_tombstone {domain}") as conn:
            self._ensure_domain(conn, domain)
            conn.execute(
                pg_insert(tombstones).on_conflict_do_nothing(),
                [
                    {
                        "domain": domain,
                        "digest": k,
                        "deleted_rows_at": now,
                        "blob_delete_after": delete_after,
                    }
                    for k in wanted
                ],
            )

    def gc_drop_tombstones(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        wanted = sorted({str(check_digest(d, "gc_drop_tombstones digest")) for d in digests})
        if not wanted:
            return
        with self._tx(f"gc_drop_tombstones {domain}") as conn:
            conn.execute(
                sa.delete(tombstones).where(
                    tombstones.c.domain == domain,
                    tombstones.c.digest == sa.any_(sa.literal(wanted, ARRAY(sa.Text))),
                )
            )

    def gc_claim(self, domain: str, digest: Digest, now: datetime) -> bool:
        check_domain(domain)
        key = str(check_digest(digest, "gc_claim digest"))
        t, b = tombstones, blobs
        with self._tx(f"gc_claim {domain}/{key}") as conn:
            # Lock the blob row first (as `touch` and `register_blobs` do), then decide: a touch
            # either committed before (and is seen) or waits and finds the row gone.
            touched: datetime | None = conn.execute(
                sa.select(b.c.last_access)
                .where(b.c.domain == domain, b.c.digest == key)
                .with_for_update()
            ).scalar_one_or_none()
            stone: datetime | None = conn.execute(
                sa.select(t.c.deleted_rows_at)
                .where(t.c.domain == domain, t.c.digest == key, t.c.blob_delete_after <= now)
                .with_for_update()
            ).scalar_one_or_none()
            if stone is None or (touched is not None and touched >= stone):
                return False
            conn.execute(sa.delete(t).where(t.c.domain == domain, t.c.digest == key))
            conn.execute(sa.delete(b).where(b.c.domain == domain, b.c.digest == key))
        return True

    def gc_lock(self, domain: str, owner: str, *, now: datetime, ttl_s: int) -> GcLock:
        check_domain(domain)
        check_driver_owner(owner, "gc_lock")
        check_lease_ttl(ttl_s)
        g = gc_locks
        stmt = pg_insert(g).values(
            domain=domain, owner=owner, expires_at=now + timedelta(seconds=ttl_s)
        )
        with self._tx(f"gc_lock {domain}") as conn:
            # Concurrent runs serialize on the row; at most one takes a free or expired lock.
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[g.c.domain],
                    set_={"owner": stmt.excluded.owner, "expires_at": stmt.excluded.expires_at},
                    where=sa.or_(g.c.expires_at <= now, g.c.owner == stmt.excluded.owner),
                )
            )
            row = conn.execute(
                sa.select(g.c.owner, g.c.expires_at).where(g.c.domain == domain)
            ).one()
        return GcLock(domain, str(row.owner), row.expires_at)

    def gc_unlock(self, domain: str, owner: str) -> None:
        check_domain(domain)
        with self._tx(f"gc_unlock {domain}") as conn:
            conn.execute(
                sa.delete(gc_locks).where(gc_locks.c.domain == domain, gc_locks.c.owner == owner)
            )

    def gc_record_run(self, run: GcRun) -> None:
        checked = check_run(run)
        with self._tx(f"gc_record_run {checked.domain}") as conn:
            self._ensure_domain(conn, checked.domain)
            conn.execute(
                sa.insert(gc_runs).values(
                    domain=checked.domain,
                    started_at=checked.started_at,
                    finished_at=checked.finished_at,
                    marked=checked.marked,
                    swept_rows=checked.swept_rows,
                    swept_bytes=checked.swept_bytes,
                    dry_run=checked.dry_run,
                    report=checked.report,
                )
            )

    # --- events ---------------------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        """Append the event; inserts of a domain are serialized (an advisory lock held until
        commit), so its ids commit in order and a stream resuming after an id misses nothing.
        Commit wakes the streams of the build and its domain (NOTIFY `ebs_events`)."""
        checked = revalidate(event, Event, "emit")
        stored_at = self._clock.now()  # the partition key: never the client's `ts`
        month = month_start(stored_at)
        for attempt in (0, 1):
            try:
                with self._tx(f"emit {checked.type}") as conn:
                    domain: str | None = conn.execute(
                        sa.select(builds.c.domain).where(builds.c.id == checked.build)
                    ).scalar_one_or_none()
                    if domain is None:
                        raise MetadataError(f"no build with id {checked.build}")
                    conn.execute(
                        sa.select(sa.func.pg_advisory_xact_lock(
                            sa.func.hashtextextended(f"{SCHEMA}.events:{domain}", 0)
                        ))
                    )  # fmt: skip
                    if month not in self._event_months:
                        conn.execute(sa.select(sa.func.ebs.ensure_events_partition(month)))
                    conn.execute(
                        sa.insert(events).values(
                            build_id=checked.build,
                            ts=checked.ts,
                            type=checked.type,
                            action_id=checked.action_id,
                            data=checked.data,
                            stored_at=stored_at,
                        )
                    )
                    _notify(conn, str(domain), checked.build)
                self._event_months.add(month)
                return
            except MetadataError as exc:
                # Retention (maybe in another process) dropped the partition this process
                # remembered: create it again, once. A missing partition is a check violation.
                cause = getattr(exc.__cause__, "orig", exc.__cause__)
                if attempt or not isinstance(cause, psycopg.errors.CheckViolation):
                    raise
                self._event_months.discard(month)

    def events_after(
        self,
        after: int,
        *,
        build: BuildId | None = None,
        domain: str | None = None,
        limit: int = EVENT_PAGE_MAX,
    ) -> list[StoredEvent]:
        check_event_query(build, domain, limit)
        e = events
        stmt = sa.select(e.c.id, e.c.build_id, e.c.ts, e.c.type, e.c.action_id, e.c.data).where(
            e.c.id > int(after)
        )
        if build is not None:
            stmt = stmt.where(e.c.build_id == int(build))
        else:
            stmt = stmt.join(builds, builds.c.id == e.c.build_id).where(builds.c.domain == domain)
        with self._tx(f"events_after {after}") as conn:
            rows = conn.execute(stmt.order_by(e.c.id).limit(limit)).all()
        return [
            StoredEvent(
                id=int(r.id),
                event=Event.model_validate(
                    {
                        "ts": r.ts,
                        "build": int(r.build_id),
                        "type": r.type,
                        "action_id": r.action_id,
                        "data": r.data,
                    }
                ),
            )
            for r in rows
        ]

    def _event_partitions(self, conn: sa.Connection) -> dict[datetime, str]:
        rows: Sequence[str] = (
            conn.execute(
                sa.text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = CAST(:parent AS regclass)"
                ),
                {"parent": f"{SCHEMA}.events"},
            )
            .scalars()
            .all()
        )
        found: dict[datetime, str] = {}
        for name in rows:
            m = _PARTITION.fullmatch(str(name))
            if m is not None:
                found[datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=UTC)] = str(name)
        return found

    def prune_events(self, before: datetime, *, dry_run: bool = False) -> list[str]:
        if before.tzinfo is None:
            raise MetadataError("prune_events: `before` must be timezone-aware")
        with self._tx("prune_events") as conn:
            parts = self._event_partitions(conn)
            old = sorted(m for m in parts if next_month(m) <= before)
            if not dry_run:
                for m in old:
                    conn.execute(sa.text(f'DROP TABLE {SCHEMA}."{parts[m]}"'))
                    self._event_months.discard(m)
        return [month_name(m) for m in old]

    def ensure_event_partitions(self, at: datetime, *, months_ahead: int = 1) -> list[str]:
        """Create the partitions of `at`'s month and the next `months_ahead` ones, so the first
        events of a month need no DDL (the retention job calls this)."""
        month = month_start(at)
        names = []
        with self._tx("ensure_event_partitions") as conn:
            for _ in range(months_ahead + 1):
                names.append(
                    str(
                        conn.execute(sa.select(sa.func.ebs.ensure_events_partition(month))).scalar()
                    )
                )
                month = next_month(month)
        return names

    # --- toolchain registry ---------------------------------------------------------------------

    def register_toolchain(self, spec: ToolchainCreate, manifest: bytes) -> RegisterOutcome:
        checked = check_toolchain(spec, manifest)
        new, t = str(checked.id), toolchains
        now = self._clock.now()
        with self._tx(f"register_toolchain {checked.module!r}") as conn:
            # One registration per module at a time: at most one current toolchain per module.
            conn.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(checked.module, 0))
                )
            )
            found = conn.execute(
                sa.select(t.c.superseded_by).where(t.c.id == new).with_for_update()
            ).first()
            if found is not None and found[0] is None:
                return RegisterOutcome(inserted=False, superseded=())
            if found is None:
                conn.execute(
                    sa.insert(t).values(**checked.model_dump(mode="json"), registered_at=now)
                )
                conn.execute(
                    sa.insert(toolchain_manifests).values(toolchain_id=new, entries=manifest)
                )
            else:
                conn.execute(sa.update(t).where(t.c.id == new).values(superseded_by=None))
            superseded: list[str] = list(
                conn.execute(
                    sa.update(t)
                    .where(t.c.module == checked.module, t.c.superseded_by.is_(None), t.c.id != new)
                    .values(superseded_by=new)
                    .returning(t.c.id)
                ).scalars()
            )
        return RegisterOutcome(
            inserted=found is None, superseded=tuple(Digest.parse(i) for i in sorted(superseded))
        )

    def _toolchain_rows(self, what: str, *where: sa.ColumnElement[bool]) -> list[ToolchainRecord]:
        t = toolchains
        stmt = (
            sa.select(t)
            .where(*where)
            .order_by(t.c.module.collate("C"), t.c.registered_at, t.c.id.collate("C"))
        )
        with self._tx(what) as conn:
            found = conn.execute(stmt).mappings().all()
        try:
            return [ToolchainRecord.model_validate(dict(r)) for r in found]
        except ValueError as exc:
            raise MetadataError(f"{what}: a stored toolchain row is invalid: {exc}") from None

    def get_toolchain(self, toolchain_id: Digest) -> ToolchainRecord | None:
        key = str(check_digest(toolchain_id, "get_toolchain id"))
        rows = self._toolchain_rows(f"get_toolchain {key}", toolchains.c.id == key)
        return rows[0] if rows else None

    def current_toolchain(self, module: str) -> ToolchainRecord | None:
        rows = self.list_toolchains(module=module, current_only=True)
        return rows[-1] if rows else None

    def list_toolchains(
        self, *, module: str | None = None, current_only: bool = False
    ) -> list[ToolchainRecord]:
        where: list[sa.ColumnElement[bool]] = []
        if module is not None:
            where.append(toolchains.c.module == module)
        if current_only:
            where.append(toolchains.c.superseded_by.is_(None))
        return self._toolchain_rows("list_toolchains", *where)

    def toolchain_manifest(self, toolchain_id: Digest) -> bytes | None:
        key = str(check_digest(toolchain_id, "toolchain_manifest id"))
        m = toolchain_manifests
        with self._tx(f"toolchain_manifest {key}") as conn:
            entries: bytes | None = conn.execute(
                sa.select(m.c.entries).where(m.c.toolchain_id == key)
            ).scalar_one_or_none()
        return None if entries is None else bytes(entries)

    def touch_toolchain(self, toolchain_id: Digest) -> None:
        key = str(check_digest(toolchain_id, "touch_toolchain id"))
        now, t = self._clock.now(), toolchains
        with self._tx(f"touch_toolchain {key}") as conn:
            conn.execute(
                sa.update(t)
                .where(
                    t.c.id == key,
                    sa.or_(t.c.last_used_at.is_(None), t.c.last_used_at <= touch_cutoff(now)),
                )
                .values(last_used_at=now)
            )


_PARTITION: Final = re.compile(r"events_y(\d{4})m(\d{2})")


def _notify(conn: sa.Connection, domain: str, build: int) -> None:
    """Wake the event streams of `build` and `domain` when the transaction commits."""
    conn.execute(sa.select(sa.func.pg_notify(EVENTS_CHANNEL, f"{domain} {int(build)}")))
