from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    ingest_bearer_token: str
    allowed_origin: str = "http://localhost:5173"
    openfda_api_key: str | None = None
    # Number of trusted reverse-proxy hops in front of the app. 0 = direct connections
    # (local/Docker): rate-limit by the peer IP. In production behind a proxy (e.g. Render = 1),
    # set this so the real client IP is read from the proxy-controlled end of X-Forwarded-For
    # instead of every request sharing the proxy's IP. Never trust XFF when this is 0 — a client
    # can forge the header, so an unset value must fall back to the direct peer.
    trusted_proxy_hops: int = 0

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
