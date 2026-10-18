"""Release imports of builds: which release each import alias of a build used (P2-03).

Revision ID: 0008_release_imports
Revises: 0007_releases
Create Date: 2026-10-17

Rows are what `ebs used-by` follows from a release to the builds that consumed it; they are
append-only like the other release history.

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_release_imports"
down_revision: str | None = "0007_releases"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
ALIAS = "^[A-Za-z_][A-Za-z0-9_]{0,63}$"


def upgrade() -> None:
    op.create_table(
        "release_imports",
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("build_id", "alias", name=op.f("pk_release_imports")),
        sa.ForeignKeyConstraint(
            ["build_id"], [f"{S}.builds.id"], name=op.f("fk_release_imports_build_id_builds")
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            [f"{S}.releases.id"],
            name=op.f("fk_release_imports_release_id_releases"),
        ),
        sa.CheckConstraint(f"alias ~ '{ALIAS}'", name=op.f("ck_release_imports_alias_valid")),
        schema=S,
    )
    op.create_index("ix_release_imports_release_id", "release_imports", ["release_id"], schema=S)
    op.execute(
        f"CREATE TRIGGER release_imports_append_only BEFORE UPDATE OR DELETE ON "
        f"{S}.release_imports FOR EACH ROW EXECUTE FUNCTION {S}.append_only()"
    )
    op.execute(
        f"CREATE TRIGGER release_imports_no_truncate BEFORE TRUNCATE ON {S}.release_imports "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {S}.append_only()"
    )


def downgrade() -> None:
    op.drop_index("ix_release_imports_release_id", table_name="release_imports", schema=S)
    op.drop_table("release_imports", schema=S)
