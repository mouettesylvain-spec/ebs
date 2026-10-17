"""PostgreSQL MetadataStore (interfaces.md § 6, data-model.md).

Settings come from the `[metadata]` config section (R9). Each public method runs in one short
transaction on a pooled connection, so no transaction ever spans a driver wait. Timestamps come
from the injected clock, not from the database, so behaviour is testable with a FakeClock.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
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
    attribution_digests,
    cache_refs,
    check_blobs,
    check_by,
    check_digest,
    check_exportable,
    check_final_status,
    check_lease_ttl,
    check_pin,
    check_release,
    check_result,
    check_run,
    check_set_channel,
    check_toolchain,
    edge_rows,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
)
from ebs.meta.api import (
    ActionRow,
    ActionState,
    BlobRef,
    BuildCreate,
    BuildId,
    BuildStatus,
    BuildView,
    ChannelMove,
    ChannelView,
    DomainPolicy,
    Event,
    ExportRecord,
    GateCheck,
    GcAttribution,
    GcBlob,
    GcCacheEntry,
    GcRoots,
    GcRun,
    LeaseId,
    RegisterOutcome,
    ReleaseCreate,
    ReleaseId,
    ReleaseView,
    ResultManifest,
    Tombstone,
    ToolchainCreate,
    ToolchainRecord,
    check_domain,
)
from ebs.meta.models import (
    action_cache,
    actions,
    blobs,
    builds,
    channel_history,
    channels,
    domains,
    events,
    exports,
    gc_runs,
    lease_objects,
    leases,
    pins,
    provenance_edges,
    releases,
    tombstones,
    toolchain_manifests,
    toolchains,
)

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
            # A finished build holds no lease; a pin is a separate root.
            conn.execute(sa.delete(leases).where(leases.c.build_id == build))

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
            roots = {str(self._plan_of(conn, build))}
            for payload in results:
                roots |= {str(d) for d in attribution_digests(payload)}
            self._touch_rows(conn, domain, roots, self._clock.now())

    @staticmethod
    def _plan_of(conn: sa.Connection, build: BuildId) -> str:
        plan: str = conn.execute(
            sa.select(builds.c.plan_digest).where(builds.c.id == build)
        ).scalar_one()
        return plan

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
                sa.select(builds.c.plan_digest, actions.c.result)
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
        for plan_digest, result in pinned_builds:
            pinned.add(Digest.parse(plan_digest))
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
