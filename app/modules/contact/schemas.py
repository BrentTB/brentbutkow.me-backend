from datetime import datetime

from pydantic import Field, field_validator

from app.camel import CamelModel

_MAX_MESSAGE = 5000
_MAX_FIELD = 200


class ContactSubmission(CamelModel):
    message: str = Field(max_length=_MAX_MESSAGE, description="The visitor's message.")
    name: str | None = Field(default=None, max_length=_MAX_FIELD)
    email: str | None = Field(default=None, max_length=_MAX_FIELD)
    # Client-collected context — free signals, no geolocation prompt.
    timezone: str | None = Field(default=None, max_length=_MAX_FIELD)
    locale: str | None = Field(default=None, max_length=_MAX_FIELD)
    referrer: str | None = Field(default=None, max_length=_MAX_FIELD)
    # Milliseconds the form was on screen before submit — bots submit near-instantly.
    elapsed_ms: int | None = Field(default=None, ge=0)
    # Honeypot — real users leave it blank, bots fill it. Never stored.
    website: str | None = Field(default=None, max_length=_MAX_FIELD)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()

    @field_validator("email")
    @classmethod
    def _email_looks_valid(cls, value: str | None) -> str | None:
        # Light check, not RFC validation — avoids a dependency for an optional field.
        if value and ("@" not in value or "." not in value.rsplit("@", 1)[-1]):
            raise ValueError("invalid email")
        return value


class ContactResult(CamelModel):
    status: str


class MessageOut(CamelModel):
    id: int
    created_at: datetime
    message: str
    name: str | None
    email: str | None
    timezone: str | None
    locale: str | None
    referrer: str | None
    user_agent: str | None
    accept_language: str | None
    ip_address: str | None
    country: str | None
    is_bot: bool
    bot_reason: str | None
