from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="pending_confirmation")
    entities = Column(JSONB, nullable=False, default=list)
    company = Column(Text, nullable=True)
    countries = Column(JSONB, nullable=False)
    categories = Column(JSONB, nullable=False, default=list)
    min_severity = Column(Text, nullable=True)
    confirmation_token_hash = Column(Text, nullable=True)
    management_token = Column(Text, nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_digest_at = Column(DateTime(timezone=True), nullable=True)
    skipped_at = Column(JSONB, nullable=False, default=list)
