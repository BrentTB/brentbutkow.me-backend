from typing import Any

from pydantic import model_validator
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
    # How long a multiplayer room lives before it expires (default 24h). Expiry is checked on read,
    # and expired rooms are pruned on the next create, so a stale game frees its code.
    room_ttl_seconds: int = 86400
    # How long a player's seat survives without a read before the other side is told they left. Wide
    # enough to ride out a slow network on a two-second poll; short enough that a closed tab
    # shows up while the opponent is still watching.
    room_presence_timeout_seconds: int = 20
    # How long a seat may go quiet before a game in progress is forfeited to the player still here.
    # Far wider than the presence window on purpose: presence is a display signal, and a browser
    # throttles a background tab's polling to roughly once a minute, so a player who switches tabs
    # must not lose a game they are still playing.
    room_forfeit_timeout_seconds: int = 300

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, values: Any) -> Any:
        """
        A blank value means "use the default".

        `op inject` renders a 1Password field that holds nothing as an empty string, so a template
        line left blank on purpose arrives as `SETTING=`. Pydantic counts that as a value it must
        parse: an int field rejects it outright, and a `str | None` field lands on `""` rather than
        the None its default promises. Dropping the key leaves the field absent, which is the only
        state that actually reaches a default.
        """
        if not isinstance(values, dict):
            return values
        return {
            key: value
            for key, value in values.items()
            if not (isinstance(value, str) and not value.strip())
        }

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
