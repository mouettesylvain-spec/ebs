"""Readiness of the metadata service (P1-06 R6): database reachable, migrations at head."""

from __future__ import annotations

from typing import Protocol

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from ebs.meta.migrations import SCRIPT_LOCATION

__all__ = ["PgReadiness", "Readiness", "migration_heads"]


class Readiness(Protocol):
    def check(self) -> str | None:
        """None when ready, else why not (shown by `/readyz`)."""
        ...


def migration_heads() -> frozenset[str]:
    """The head revisions this code expects (`ebs.meta.migrations`)."""
    return frozenset(ScriptDirectory(str(SCRIPT_LOCATION)).get_heads())


class PgReadiness:
    def __init__(self, engine: sa.Engine, *, heads: frozenset[str] | None = None) -> None:
        self._engine = engine
        self._heads = heads if heads is not None else migration_heads()

    def check(self) -> str | None:
        try:
            with self._engine.connect() as conn:
                found = {
                    str(r[0])
                    for r in conn.execute(sa.text("SELECT version_num FROM public.alembic_version"))
                }
        except sa.exc.SQLAlchemyError as exc:
            return f"metadata database not reachable or not migrated: {exc.__class__.__name__}"
        if found != self._heads:
            return (
                f"database schema at {sorted(found) or 'no revision'}, this service expects "
                f"{sorted(self._heads)}: run the migrations (alembic upgrade head)"
            )
        return None
