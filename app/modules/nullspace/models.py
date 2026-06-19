from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Score(Base):
    __tablename__ = "nullspace_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    name: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer)
    # Cumulative enemies destroyed — the run stat the score is cross-checked against.
    kills: Mapped[int] = mapped_column(Integer)
    wave: Mapped[int] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    # Wall-clock run length (client-reported), used for the too-fast-for-wave check.
    duration_ms: Mapped[int] = mapped_column(Integer)
    ship_kind: Mapped[str] = mapped_column(Text)
    # Game version at time of play — balance differs per version, so leaderboards
    # and the plausibility ceilings are scoped by it.
    version: Mapped[str] = mapped_column(Text)
    # Economy snapshot — all flow from kills like the score does, so they corroborate it.
    currency: Mapped[int] = mapped_column(Integer)
    space_metal: Mapped[int] = mapped_column(Integer)
    upgrades_purchased: Mapped[int] = mapped_column(Integer)
    ultimates_owned: Mapped[int] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(Text)
    # Implausible submissions are kept (capped) but hidden from the leaderboard, so a
    # forged score gets no signal it was caught and the run can still be inspected.
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    flag_reason: Mapped[str | None] = mapped_column(Text)
