"""enforce one subscription per email (case-insensitive)

Deduplicates existing subscriptions by lower(email) — keeping the single row that best reflects
current intent — then swaps the plain email index for a UNIQUE functional index on lower(email),
so a second row for the same address can never be inserted again (the create() flow reuses the
existing row across unsubscribe/resubscribe).

Keep-priority when deduping: active > paused > pending_confirmation > unsubscribed, tie-broken by
most-recently updated, then created, then id. Deletions are irreversible — downgrade restores the
non-unique index but cannot resurrect removed rows.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-06-30 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEDUPE = """
DELETE FROM subscriptions s
USING (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY lower(email)
            ORDER BY
                CASE status
                    WHEN 'active' THEN 0
                    WHEN 'paused' THEN 1
                    WHEN 'pending_confirmation' THEN 2
                    WHEN 'unsubscribed' THEN 3
                    ELSE 4
                END,
                updated_at DESC NULLS LAST,
                created_at DESC NULLS LAST,
                id DESC
        ) AS rn
    FROM subscriptions
) ranked
WHERE s.id = ranked.id AND ranked.rn > 1;
"""


def upgrade() -> None:
    op.execute(_DEDUPE)
    op.execute("DROP INDEX IF EXISTS idx_subscriptions_email")
    op.execute("CREATE UNIQUE INDEX uq_subscriptions_email_lower ON subscriptions (lower(email))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_subscriptions_email_lower")
    op.execute("CREATE INDEX idx_subscriptions_email ON subscriptions (email)")
