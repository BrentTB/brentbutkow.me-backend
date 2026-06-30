from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    ingest_bearer_token: str
    # Shared secret guarding POST /internal/dispatch-alerts (the daily digest trigger). Absent →
    # the endpoint rejects every caller, so a missing secret fails closed rather than open.
    internal_dispatch_token: str | None = None
    # Master secret for the operator admin API (POST /admin/login). Absent → admin login rejects
    # everyone, so a missing secret fails closed rather than open (like internal_dispatch_token).
    # The session-token signing key is derived from it, so changing it invalidates live sessions.
    admin_password: str | None = None
    # Lifetime of an issued admin session token, in seconds (default 24h). Plain config, not a
    # secret — the login password is exchanged for a token that expires after this window.
    admin_session_ttl_seconds: int = 86400
    # Email delivery (Resend). resend_api_key absent → email sending is disabled (dev default):
    # subscriptions still work, confirmation/digest emails are skipped. operator_email absent →
    # the operator digest is skipped.
    resend_api_key: str | None = None
    resend_from_address: str = "recalls@notify.brentbutkow.me"
    operator_email: str | None = None
    allowed_origin: str = "http://localhost:5173"
    # Optional regex matched in *addition* to allowed_origin, for origins whose hostname is not
    # fixed (e.g. Vercel preview deploys, where the subdomain changes per deployment). Anchor it
    # to your own scope — a blanket *.vercel.app would let any site on Vercel read the API.
    allowed_origin_regex: str | None = None
    # Number of trusted reverse-proxy hops in front of the app. 0 = direct connections
    # (local/Docker): rate-limit by the peer IP. In production behind a proxy (e.g. Render = 1),
    # set this so the real client IP is read from the proxy-controlled end of X-Forwarded-For
    # instead of every request sharing the proxy's IP. Never trust XFF when this is 0 — a client
    # can forge the header, so an unset value must fall back to the direct peer.
    trusted_proxy_hops: int = 0

    @field_validator("allowed_origin_regex", mode="after")
    @classmethod
    def _blank_regex_is_none(cls, value: str | None) -> str | None:
        # `ALLOWED_ORIGIN_REGEX=` (blank) loads as "", which Starlette would compile into an empty
        # regex run uselessly on every request; treat blank as unset so the None path applies.
        return value if value and value.strip() else None

    @property
    def origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origin.split(",") if origin.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        # SQLAlchemy needs the driver in the URL; Neon provides a bare postgresql:// string.
        for prefix in ("postgresql://", "postgres://"):
            if self.database_url.startswith(prefix):
                return "postgresql+psycopg://" + self.database_url[len(prefix) :]
        return self.database_url


settings = Settings()  # type: ignore[call-arg]
