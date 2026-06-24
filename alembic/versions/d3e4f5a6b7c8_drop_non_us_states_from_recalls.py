"""drop non-US state values from recalls (openFDA Canadian provinces, "N/A")

openFDA stored the recalling firm's state verbatim, so foreign firms put Canadian provinces
("Ontario", "Nova Scotia", ...) and "N/A" into the state column, and these then surfaced in the
/recalls/facets state filter as if they were US states. Null them out. Future ingests are filtered
at the source via normalize.parse_us_state; FSIS/UK/NCC were already clean.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 50 states + DC + inhabited territories — kept in sync with normalize.US_STATE_CODES.
_US_STATE_CODES = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "PR",
    "GU",
    "VI",
)


def upgrade() -> None:
    # Null both the single display value and the array (the facet/map source) wherever the stored
    # state isn't a real US code. Keying on the scalar `state` is safe: FSIS leaves it NULL for
    # multi-state recalls and a valid code for single-state ones, so only the FDA junk matches.
    op.execute(
        sa.text(
            "UPDATE recalls SET state = NULL, states = NULL "
            "WHERE state IS NOT NULL AND upper(btrim(state)) NOT IN :codes"
        ).bindparams(sa.bindparam("codes", value=_US_STATE_CODES, expanding=True))
    )


def downgrade() -> None:
    # Irreversible: the original (invalid) values aren't retained, but the raw source payload still
    # lives in recalls.raw if a value ever needs to be recovered.
    pass
