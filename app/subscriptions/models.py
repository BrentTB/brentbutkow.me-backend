from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base  # existing declarative base

SEVERITY_ORDER = ["low", "moderate", "high", "severe", "critical"]


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_confirmation','active','paused','unsubscribed')",
            name="ck_subscriptions_status",
        ),
        UniqueConstraint("management_token", name="uq_subscriptions_management_token"),
        # One subscription per email (case-insensitive): a resubscribe/restage reuses the existing
        # row rather than inserting a second. Functional unique index — also serves the
        # func.lower(email) lookup in service.create(), so a plain email index is redundant.
        Index("uq_subscriptions_email_lower", func.lower(Column("email")), unique=True),
        Index(
            "idx_subscriptions_active",
            "id",
            postgresql_where=Column("status") == "active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_confirmation")
    entities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    companies: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    countries: Mapped[list] = mapped_column(JSONB, nullable=False)
    categories: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    min_severity: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmation_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    management_token: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_at: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # A confirmed subscriber's requested-but-not-yet-confirmed preference change. Holds
    # {"criteria": <normalised filters>, "requested_at": <iso>}; cleared once confirmed/expired.
    pending_update: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DispatchState(Base):
    """Singleton row (id=1) persisting the dispatch cursor across restarts and deploys.

    The dispatcher only sends digests for recalls created after last_run_at; keeping it in the DB
    (rather than a process-local variable) stops a restart from re-treating the whole backlog as
    new. A multi-instance deploy would still need a shared lock — see app/internal/router.py.
    """

    __tablename__ = "dispatch_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
