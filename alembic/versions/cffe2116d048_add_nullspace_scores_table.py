"""add nullspace scores table

Revision ID: cffe2116d048
Revises: a7b8c9d0e1f2
Create Date: 2026-06-19 19:25:24.275134

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cffe2116d048"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nullspace_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("kills", sa.Integer(), nullable=False),
        sa.Column("wave", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("ship_kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("currency", sa.Integer(), nullable=False),
        sa.Column("space_metal", sa.Integer(), nullable=False),
        sa.Column("upgrades_purchased", sa.Integer(), nullable=False),
        sa.Column("ultimates_owned", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("flagged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flag_reason", sa.Text(), nullable=True),
    )
    # Leaderboard reads: global top-N by score, and the per-version board.
    op.create_index("ix_nullspace_scores_score", "nullspace_scores", ["score"])
    op.create_index("ix_nullspace_scores_version_score", "nullspace_scores", ["version", "score"])


def downgrade() -> None:
    op.drop_index("ix_nullspace_scores_version_score", table_name="nullspace_scores")
    op.drop_index("ix_nullspace_scores_score", table_name="nullspace_scores")
    op.drop_table("nullspace_scores")
