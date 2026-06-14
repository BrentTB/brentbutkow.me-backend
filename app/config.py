from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    ingest_bearer_token: str
    allowed_origin: str = "http://localhost:5173"
    port: int = 3000
    openfda_api_key: str | None = None

    @property
    def origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origin.split(",") if origin.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        # SQLAlchemy needs the driver in the URL; Railway provides a bare postgresql:// string.
        for prefix in ("postgresql://", "postgres://"):
            if self.database_url.startswith(prefix):
                return "postgresql+psycopg://" + self.database_url[len(prefix) :]
        return self.database_url


settings = Settings()  # type: ignore[call-arg]
