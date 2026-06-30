import uuid
from datetime import datetime

from pydantic import Field

from app.camel import CamelModel
from app.modules.contact.schemas import MessageOut


class AdminLoginRequest(CamelModel):
    password: str = Field(min_length=1, max_length=500)


class AdminLoginResult(CamelModel):
    token: str
    expires_at: datetime


class MessageCounts(CamelModel):
    total: int
    real: int
    bot: int


class SubscriptionCounts(CamelModel):
    total: int
    active: int
    pending_confirmation: int
    paused: int
    unsubscribed: int


class IngestSummary(CamelModel):
    last_run_at: datetime | None
    status: str | None
    fetched_count: int
    upserted_count: int


class RecallCounts(CamelModel):
    total: int
    us: int
    uk: int
    za: int


class NullspaceCounts(CamelModel):
    score_count: int


class AdminOverview(CamelModel):
    messages: MessageCounts
    subscriptions: SubscriptionCounts
    # None until the first ingest run has been recorded.
    ingest: IngestSummary | None
    recalls: RecallCounts
    nullspace: NullspaceCounts


class MessageListResult(CamelModel):
    items: list[MessageOut]
    total: int


class SubscriptionAdminOut(CamelModel):
    # Operator view — fuller than the subscriber-facing SubscriptionOut: includes id, lifecycle
    # timestamps, and the last-digest cursor. Management/confirmation tokens are left out.
    id: uuid.UUID
    email: str
    status: str
    countries: list[str]
    entities: list[str]
    companies: list[str]
    categories: list[str]
    min_severity: str | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_digest_at: datetime | None


class SubscriptionListResult(CamelModel):
    items: list[SubscriptionAdminOut]
    total: int
