from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, Text, UniqueConstraint
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
        Index("idx_subscriptions_email", "email"),
        Index(
            "idx_subscriptions_active",
            "id",
            postgresql_where=Column("status") == "active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_confirmation")
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
