import os

# Set required env before any `app.*` import triggers Settings() / engine creation.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
os.environ.setdefault("INGEST_BEARER_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_ORIGIN", "http://localhost:5173")
