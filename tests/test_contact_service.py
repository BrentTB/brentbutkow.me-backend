import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.modules.contact import service
from app.modules.contact.models import Message
from app.modules.contact.schemas import ContactSubmission


@pytest.fixture
def session():
    # In-memory SQLite with only the messages table — enough to exercise the service's
    # persist/list/prune logic without a Postgres dependency.
    engine = create_engine("sqlite://")
    Message.__table__.create(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        yield db
    engine.dispose()


def _store(session, message="Hello there", **meta):
    return service.create_message(
        session,
        ContactSubmission(message=message),
        user_agent=meta.pop("user_agent", None),
        accept_language=meta.pop("accept_language", None),
        ip_address=meta.pop("ip_address", None),
        **meta,
    )


def _count(session, **where):
    stmt = select(func.count()).select_from(Message)
    if "is_bot" in where:
        stmt = stmt.where(Message.is_bot.is_(where["is_bot"]))
    return session.scalar(stmt)


def test_create_message_persists_and_defaults(session):
    saved = _store(session, "Hello", ip_address="1.2.3.4", user_agent="UA")
    assert saved.id is not None
    assert saved.is_bot is False
    rows = service.list_messages(session)
    assert len(rows) == 1
    assert rows[0].message == "Hello"
    assert rows[0].ip_address == "1.2.3.4"
    assert rows[0].user_agent == "UA"


def test_list_messages_newest_first_and_capped(session, monkeypatch):
    monkeypatch.setattr(service, "_LIST_LIMIT", 3)
    for i in range(5):
        _store(session, f"msg {i}")
    assert [m.message for m in service.list_messages(session)] == ["msg 4", "msg 3", "msg 2"]


def test_prune_caps_total_messages(session, monkeypatch):
    monkeypatch.setattr(service, "_MAX_ROWS", 3)
    for i in range(5):
        _store(session, f"msg {i}")
    assert _count(session) == 3
    assert [m.message for m in service.list_messages(session)] == ["msg 4", "msg 3", "msg 2"]


def test_prune_caps_bot_messages_without_touching_real_ones(session, monkeypatch):
    monkeypatch.setattr(service, "_MAX_BOT_ROWS", 2)
    _store(session, "real")
    for i in range(4):
        _store(session, f"spam {i}", is_bot=True, bot_reason="honeypot")
    assert _count(session, is_bot=True) == 2
    assert _count(session, is_bot=False) == 1


def test_total_cap_evicts_oldest_bot_before_any_real_message(session, monkeypatch):
    monkeypatch.setattr(service, "_MAX_ROWS", 4)
    monkeypatch.setattr(service, "_MAX_BOT_ROWS", 100)  # keep the bot cap out of the way
    _store(session, "bot 0", is_bot=True, bot_reason="honeypot")
    _store(session, "bot 1", is_bot=True, bot_reason="honeypot")
    _store(session, "real 0")
    _store(session, "real 1")  # total now at the cap (4)
    _store(session, "real 2")  # over the cap → oldest bot is evicted, no real message touched
    assert _count(session) == 4
    assert _count(session, is_bot=False) == 3  # every real message survives
    survivors = {m.message for m in service.list_messages(session)}
    assert "bot 0" not in survivors  # oldest bot went first
    assert "bot 1" in survivors


def test_total_cap_falls_back_to_real_messages_once_no_bots_left(session, monkeypatch):
    monkeypatch.setattr(service, "_MAX_ROWS", 2)
    monkeypatch.setattr(service, "_MAX_BOT_ROWS", 100)
    _store(session, "bot 0", is_bot=True, bot_reason="honeypot")
    _store(session, "real 0")  # total at the cap (2)
    _store(session, "real 1")  # over → the only bot is evicted first
    assert _count(session, is_bot=True) == 0
    _store(session, "real 2")  # over again, no bots left → oldest real message evicted
    assert _count(session) == 2
    assert {m.message for m in service.list_messages(session)} == {"real 1", "real 2"}
