"""add predicted class + novelty score columns

Revision ID: a1b2c3d4e5f6
Revises: d5e6f7a8b9c0
Create Date: 2026-07-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Cross-country class prediction (scripts/build_predictions.py): a binary Class-I-vs-not guess
    # for recalls from countries with no native class system (UK, ZA). NULL for US/CA (they carry a
    # real `classification`) and until the build runs. Derived, like topic_id — no server_default.
    op.add_column("recalls", sa.Column("predicted_class", sa.Text(), nullable=True))
    op.add_column("recalls", sa.Column("predicted_class_confidence", sa.Float(), nullable=True))
    # Novelty (scripts/build_analytics.py): how unlike its nearest neighbours a recall is, in
    # embedding space. Indexed to back `sort=novelty`. NULL for recalls with too few neighbours to
    # judge and until the analytics build runs.
    op.add_column("recalls", sa.Column("novelty_score", sa.Float(), nullable=True))
    op.create_index("ix_recalls_novelty_score", "recalls", ["novelty_score"])


def downgrade() -> None:
    op.drop_index("ix_recalls_novelty_score", table_name="recalls")
    op.drop_column("recalls", "novelty_score")
    op.drop_column("recalls", "predicted_class_confidence")
    op.drop_column("recalls", "predicted_class")
