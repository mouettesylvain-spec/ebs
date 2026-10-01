"""Alembic migrations of the metadata schema (docs/design/data-model.md).

Each task that changes the schema adds exactly one revision under `versions/`. The version table
lives in `public` so that downgrading to base can drop schema `ebs` entirely.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

__all__ = ["alembic_config", "downgrade", "upgrade"]

SCRIPT_LOCATION = Path(__file__).resolve().parent


def alembic_config(url: str) -> Config:
    """An Alembic config for the database at `url` (a SQLAlchemy URL, psycopg driver)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # ConfigParser interpolation
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
