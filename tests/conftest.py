import os

# Force a hermetic env before any `app.*` import triggers Settings() — the suite must be
# deterministic regardless of the developer's shell/.env (e.g. an exported INGEST_BEARER_TOKEN
# must not change the token the auth test relies on).
os.environ["DATABASE_URL"] = "postgresql+psycopg://test:test@localhost:5432/test"
os.environ["INGEST_BEARER_TOKEN"] = "test-token"
os.environ["ALLOWED_ORIGIN"] = "http://localhost:5173"
