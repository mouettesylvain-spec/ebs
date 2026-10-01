"""Alembic environment: online migrations only, one transaction per run."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context

from ebs.meta.models import metadata

config = context.config


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("alembic config has no sqlalchemy.url; use ebs.meta.migrations")
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=metadata,
                include_schemas=True,
                # Pinned: by default it follows search_path, which puts it in `ebs` for a role
                # named `ebs` ("$user", public), and `downgrade base` could not drop the schema.
                version_table_schema="public",
                transaction_per_migration=False,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
