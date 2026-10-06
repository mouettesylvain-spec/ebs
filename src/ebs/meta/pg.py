"""PostgreSQL MetadataStore (interfaces.md § 6, data-model.md).

Settings come from the `[metadata]` config section (R9). Each public method runs in one short
transaction on a pooled connection, so no transaction ever spans a driver wait. Timestamps come
from the injected clock, not from the database, so behaviour is testable with a FakeClock.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Annotated, ClassVar, Final

import psycopg
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import get_logger
from ebs.meta._common import (
    check_digest,
    check_final_status,
    check_result,
    edge_rows,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
)
from ebs.meta.api import (
    ActionRow,
    ActionState,
    BuildCreate,
    BuildId,
    BuildStatus,
    BuildView,
    Event,
    ResultManifest,
    check_domain,
)
from ebs.meta.models import action_cache, actions, blobs, builds, domains, events, provenance_edges

__all__ = ["MetadataConfig", "PgMetadataStore"]

_log = get_logger(__name__)

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
            outputs = [
                {
                    "domain": domain,
                    "digest": str(out.digest),
                    "size": out.size,
                    "kind": out.type,
                    "created_at": now,
                    "last_access": now,
                }
                for out in checked.outputs.values()
            ]
            if outputs:
                conn.execute(pg_insert(blobs).on_conflict_do_nothing(), outputs)

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

    def finish_build(self, build: BuildId, status: BuildStatus) -> None:
        check_final_status(status)
        with self._tx(f"finish_build {build}") as conn:
            done = conn.execute(
                sa.update(builds)
                .where(builds.c.id == build, builds.c.status == "running")
                .values(status=status, finished_at=self._clock.now())
                .returning(builds.c.id)
            ).first()
            if done is None:
                current = self._build_status(conn, build)
                raise MetadataError(f"build {build} is already finished ({current})")

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

    # --- access tracking, events ----------------------------------------------------------------

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

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        with self._tx(f"emit {checked.type}") as conn:
            self._build_status(conn, checked.build)
            conn.execute(
                sa.insert(events).values(
                    build_id=checked.build,
                    ts=checked.ts,
                    type=checked.type,
                    action_id=checked.action_id,
                    data=checked.data,
                )
            )
