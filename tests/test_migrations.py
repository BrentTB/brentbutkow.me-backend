"""Models and migrations must describe the same schema, so `alembic revision --autogenerate` never
proposes destructive operations.

Before this guard, alembic/env.py didn't import the subscriptions models and four indexes existed
only in their migrations — autogenerate would have emitted DROP TABLE subscriptions (live subscriber
data) and dropped the leaderboard / entity / full-text indexes in the next unrelated revision.
"""

import os
import re
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command
from app.config import settings
from app.db import Base

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get("TEST_DATABASE_URL")


def _model_modules() -> set[str]:
    return {
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in (ROOT / "app").rglob("models.py")
    }


def test_env_registers_every_models_module():
    # autogenerate only knows the tables whose module env.py imports; a missing one reads as
    # "table removed". DB-free, so it runs everywhere.
    env = (ROOT / "alembic" / "env.py").read_text()
    imported = {
        f"{package}.{module}"
        for package, module in re.findall(r"^from (app[\w.]*) import (\w+)", env, re.MULTILINE)
    }
    assert _model_modules() <= imported, _model_modules() - imported


@pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL (Postgres) to run")
def test_migrated_schema_matches_models(monkeypatch):
    # Import what env.py imports, so Base.metadata is the full target autogenerate compares to.
    for module in _model_modules():
        __import__(module)

    assert TEST_DB is not None
    monkeypatch.setattr(settings, "database_url", TEST_DB)
    engine = create_engine(settings.sqlalchemy_url)
    try:
        engine.connect().close()
    except OperationalError as exc:  # pragma: no cover - depends on local env
        pytest.skip(f"cannot reach TEST_DATABASE_URL: {exc}")

    def reset_schema() -> None:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))

    reset_schema()
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        command.upgrade(config, "head")
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == []
    finally:
        reset_schema()
        engine.dispose()
