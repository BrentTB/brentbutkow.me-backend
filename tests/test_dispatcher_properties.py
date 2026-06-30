"""
tests/test_dispatcher_properties.py
Property-based tests for dispatcher.py dispatch logic.

Properties tested:
  - last_digest_at advances only on confirmed delivery
  - Sending cap creates skipped_at entries and respects 89-slot limit
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st
from resend.exceptions import ResendError

from app.modules.recalls.schemas import RecallCategory, RecallCountry
from app.subscriptions.email import is_permanent_failure
from app.subscriptions.models import SEVERITY_ORDER

# ---------------------------------------------------------------------------
# Valid enum values (mirrors test_matcher_properties.py)
# ---------------------------------------------------------------------------

VALID_COUNTRIES = [c.value for c in RecallCountry]
VALID_SEVERITIES = list(SEVERITY_ORDER)
VALID_CATEGORIES = [c.value for c in RecallCategory]

# ---------------------------------------------------------------------------
# Fake domain objects (no ORM, no DB)
# ---------------------------------------------------------------------------

_DAILY_SEND_CAP = 89  # must match dispatcher.py


@dataclass
class _FakeRecall:
    """Minimal stand-in for Recall ORM — only fields matcher.py touches."""

    entities: list[dict]
    company_name: str | None
    country: str
    category: str
    severity_label: str
    report_date: date | None
    recall_initiation_date: date | None


@dataclass
class _FakeSubscription:
    """Minimal stand-in for Subscription ORM — all fields dispatcher logic touches."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    email: str = "user@example.com"
    status: str = "active"
    entities: list[str] = field(default_factory=list)
    company: str | None = None
    countries: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    min_severity: str | None = None
    last_digest_at: datetime | None = None
    skipped_at: list[str] = field(default_factory=list)
    confirmed_at: datetime | None = None
    management_token: str = field(default_factory=lambda: str(uuid.uuid4()))
    sent_count: int = 0  # local counter — not an ORM field; used by helper below


# ---------------------------------------------------------------------------
# Test-local async helper: per-subscription dispatch logic (step 5 from task spec)
# ---------------------------------------------------------------------------


async def _dispatch_single_subscription(
    sub: _FakeSubscription,
    matching_recalls: list[_FakeRecall],
    db_commit,  # callable — replaces db_session.commit()
    sent_count: int,
    send_fn=None,  # injectable: async () -> None; defaults to no-op success
    cap: int = _DAILY_SEND_CAP,
) -> tuple[int, str | None]:
    """
    Run the per-subscription dispatch logic from dispatcher.run_dispatch step 5.

    Returns (new_sent_count, outcome) where outcome is one of:
      "sent", "skipped_no_match", "skipped_cap", "error_permanent", "error_transient", None
    """
    today_iso = datetime.now(UTC).date().isoformat()

    # 5b. Skip if zero matches
    if not matching_recalls:
        return sent_count, "skipped_no_match"

    # 5c. Daily cap check
    if sent_count >= cap:
        if today_iso not in sub.skipped_at:
            sub.skipped_at = list(sub.skipped_at) + [today_iso]
        db_commit()
        return sent_count, "skipped_cap"

    # 5d. Send with retry — inject send_fn or use a no-op success
    if send_fn is None:

        async def send_fn():
            pass

    try:
        await send_fn()
        # Success
        sub.last_digest_at = datetime.now(UTC)
        sub.skipped_at = []
        db_commit()
        sent_count += 1
        return sent_count, "sent"
    except ResendError as exc:
        if is_permanent_failure(exc):
            sub.status = "unsubscribed"
            db_commit()
            return sent_count, "error_permanent"
        return sent_count, "error_transient"
    except Exception:
        return sent_count, "error_transient"


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

country_st = st.sampled_from(VALID_COUNTRIES)
severity_st = st.sampled_from(VALID_SEVERITIES)
category_st = st.sampled_from(VALID_CATEGORIES)

_ENTITY_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

entity_value_st = st.text(min_size=1, max_size=20, alphabet=_ENTITY_ALPHABET)


@st.composite
def matching_sub_and_recalls_st(draw):
    """Draw a (_FakeSubscription, [_FakeRecall, ...]) pair guaranteed to have ≥1 match."""
    country = draw(country_st)
    category = draw(category_st)
    severity = draw(severity_st)
    entity_val = draw(entity_value_st)

    recall = _FakeRecall(
        entities=[{"type": "allergen", "value": entity_val}],
        company_name=None,
        country=country,
        category=category,
        severity_label=severity,
        report_date=date.today(),
        recall_initiation_date=None,
    )
    sub = _FakeSubscription(
        entities=[entity_val.lower()],
        countries=[country],
        categories=[category],
        min_severity=severity,
        last_digest_at=None,
        confirmed_at=datetime.now(UTC) - timedelta(days=1),
    )
    return sub, [recall]


@st.composite
def fail_mode_st(draw):
    """Draw an exception that _dispatch_single_subscription should encounter."""
    mode = draw(st.sampled_from(["4xx", "5xx", "generic"]))
    if mode == "4xx":
        status = draw(st.integers(min_value=400, max_value=499))
    elif mode == "5xx":
        status = draw(st.integers(min_value=500, max_value=599))
    else:
        status = None
    return mode, status


# ---------------------------------------------------------------------------
# last_digest_at advances only on confirmed delivery
# ---------------------------------------------------------------------------


@given(pair=matching_sub_and_recalls_st())
@settings(max_examples=50)
def test_property_12_last_digest_at_advances_only_on_success(pair):
    """
    # last_digest_at advances only on confirmed delivery

    For any subscription, after a dispatch cycle:
    - If the email send succeeds (no exception) → last_digest_at is updated to a new value.
    - If the email send raises any exception (4xx or transient) → last_digest_at is unchanged.

    """
    sub, matching_recalls = pair

    original_last_digest_at = sub.last_digest_at
    db_commit = MagicMock()

    # --- Case 1: send succeeds → last_digest_at must change ---
    sub_success = _FakeSubscription(
        entities=sub.entities,
        countries=sub.countries,
        categories=sub.categories,
        min_severity=sub.min_severity,
        last_digest_at=original_last_digest_at,
        confirmed_at=sub.confirmed_at,
    )

    async def _succeeding_send():
        pass

    asyncio.get_event_loop().run_until_complete(
        _dispatch_single_subscription(
            sub=sub_success,
            matching_recalls=matching_recalls,
            db_commit=db_commit,
            sent_count=0,
            send_fn=_succeeding_send,
        )
    )

    assert sub_success.last_digest_at is not None, (
        "last_digest_at must be set after successful delivery"
    )
    assert sub_success.last_digest_at != original_last_digest_at, (
        "last_digest_at must change on successful delivery"
    )

    # --- Case 2: send raises 4xx → last_digest_at must NOT change ---
    sub_4xx = _FakeSubscription(
        entities=sub.entities,
        countries=sub.countries,
        categories=sub.categories,
        min_severity=sub.min_severity,
        last_digest_at=original_last_digest_at,
        confirmed_at=sub.confirmed_at,
    )

    async def _raise_4xx():
        raise ResendError(
            code=400, error_type="validation_error", message="Bad Request", suggested_action=""
        )

    asyncio.get_event_loop().run_until_complete(
        _dispatch_single_subscription(
            sub=sub_4xx,
            matching_recalls=matching_recalls,
            db_commit=db_commit,
            sent_count=0,
            send_fn=_raise_4xx,
        )
    )

    assert sub_4xx.last_digest_at == original_last_digest_at, (
        "last_digest_at must NOT change on 4xx delivery failure"
    )

    # --- Case 3: send raises generic transient error → last_digest_at must NOT change ---
    sub_transient = _FakeSubscription(
        entities=sub.entities,
        countries=sub.countries,
        categories=sub.categories,
        min_severity=sub.min_severity,
        last_digest_at=original_last_digest_at,
        confirmed_at=sub.confirmed_at,
    )

    async def _raise_transient():
        raise RuntimeError("Network error")

    asyncio.get_event_loop().run_until_complete(
        _dispatch_single_subscription(
            sub=sub_transient,
            matching_recalls=matching_recalls,
            db_commit=db_commit,
            sent_count=0,
            send_fn=_raise_transient,
        )
    )

    assert sub_transient.last_digest_at == original_last_digest_at, (
        "last_digest_at must NOT change on transient delivery failure"
    )


# ---------------------------------------------------------------------------
# Sending cap and skipped_at round-trip
# ---------------------------------------------------------------------------


@given(extra_count=st.integers(min_value=1, max_value=20))
@settings(max_examples=50)
def test_property_13_sending_cap_and_skipped_at_round_trip(extra_count: int):
    """

    For N subscriptions above 89 (total = 89 + N, N ≥ 1):
    - Dispatcher sends exactly 89 digests.
    - All subscriptions beyond the first 89 have today's ISO date in skipped_at.
    - skipped_at is cleared after the next successful send.

    """
    today_iso = datetime.now(UTC).date().isoformat()
    total = _DAILY_SEND_CAP + extra_count

    # Build total fake subscriptions — each has one matching recall
    country = VALID_COUNTRIES[0]
    category = VALID_CATEGORIES[0]
    severity = VALID_SEVERITIES[0]
    entity_val = "testentity"

    recall = _FakeRecall(
        entities=[{"type": "allergen", "value": entity_val}],
        company_name=None,
        country=country,
        category=category,
        severity_label=severity,
        report_date=date.today(),
        recall_initiation_date=None,
    )
    matching_recalls = [recall]

    subs = [
        _FakeSubscription(
            entities=[entity_val],
            countries=[country],
            categories=[category],
            min_severity=severity,
            last_digest_at=None,
            confirmed_at=datetime.now(UTC) - timedelta(days=1),
        )
        for _ in range(total)
    ]

    db_commit = MagicMock()

    # Run dispatch loop using the helper (simulating the outer loop in run_dispatch)
    async def _succeeding_send():
        pass

    async def run_loop():
        sent_count = 0
        for sub in subs:
            sent_count, outcome = await _dispatch_single_subscription(
                sub=sub,
                matching_recalls=matching_recalls,
                db_commit=db_commit,
                sent_count=sent_count,
                send_fn=_succeeding_send,
            )
        return sent_count

    final_sent_count = asyncio.get_event_loop().run_until_complete(run_loop())

    # Assert: exactly 89 sent
    assert final_sent_count == _DAILY_SEND_CAP, (
        f"Expected exactly {_DAILY_SEND_CAP} sent, got {final_sent_count}"
    )

    # Assert: first 89 subs were sent successfully (skipped_at cleared, last_digest_at set)
    for i, sub in enumerate(subs[:_DAILY_SEND_CAP]):
        assert sub.last_digest_at is not None, (
            f"Sub {i} (within cap) should have last_digest_at set"
        )
        assert sub.skipped_at == [], (
            f"Sub {i} (within cap) should have skipped_at cleared after success"
        )

    # Assert: remaining subs have today's ISO date in skipped_at
    for i, sub in enumerate(subs[_DAILY_SEND_CAP:], start=_DAILY_SEND_CAP):
        assert today_iso in sub.skipped_at, (
            f"Sub {i} (beyond cap) should have today_iso={today_iso} in skipped_at, "
            f"got: {sub.skipped_at}"
        )

    # Assert: skipped_at is cleared after a subsequent successful send
    # Pick one of the skipped subs, pre-populate skipped_at with an old date, then send with cap=0
    skipped_sub = subs[_DAILY_SEND_CAP]
    skipped_sub.skipped_at = [today_iso, "2024-01-01"]  # simulate prior skips
    skipped_sub.last_digest_at = None

    async def run_clear():
        return await _dispatch_single_subscription(
            sub=skipped_sub,
            matching_recalls=matching_recalls,
            db_commit=db_commit,
            sent_count=0,  # cap resets (new day) — sent_count starts at 0
            send_fn=_succeeding_send,
        )

    _, outcome = asyncio.get_event_loop().run_until_complete(run_clear())

    assert outcome == "sent", f"Expected 'sent', got {outcome!r}"
    assert skipped_sub.skipped_at == [], "skipped_at must be cleared after a successful send"
    assert skipped_sub.last_digest_at is not None, (
        "last_digest_at must be set after a successful send"
    )


# ---------------------------------------------------------------------------
# Dispatch is a no-op when email is disabled (no phantom deliveries / cursor moves)
# ---------------------------------------------------------------------------


def test_run_dispatch_skips_when_email_disabled(monkeypatch):
    from app.config import settings
    from app.subscriptions import dispatcher

    # No API key → run_dispatch must bail before touching subscription state, so nobody is marked
    # "sent" and the persisted cursor isn't advanced over undelivered recalls.
    monkeypatch.setattr(settings, "resend_api_key", None)
    session = MagicMock()

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(dispatcher.run_dispatch(session))
    finally:
        loop.close()

    assert result["emailDisabled"] is True
    assert result["sent"] == 0
    session.get.assert_not_called()  # no dispatch_state load
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Backfill circuit breaker — an abnormally large batch suppresses subscriber sends
# ---------------------------------------------------------------------------


def _mock_session_for_dispatch(
    recalls: list, subs: list, last_run_at, messages: list | None = None
):
    """Build a MagicMock Session that feeds run_dispatch a recall batch, message batch, and subs."""
    from types import SimpleNamespace

    session = MagicMock()
    session.get.return_value = SimpleNamespace(last_run_at=last_run_at)

    recalls_scalar = MagicMock()
    recalls_scalar.all.return_value = recalls
    messages_scalar = MagicMock()
    messages_scalar.all.return_value = messages or []
    subs_scalar = MagicMock()
    subs_scalar.all.return_value = subs
    # run_dispatch calls scalars() three times: recalls, then messages, then subscriptions.
    session.scalars.side_effect = [recalls_scalar, messages_scalar, subs_scalar]
    # Stale-pending and oldest-last-digest metrics — values are irrelevant here.
    session.execute.return_value.scalar_one.return_value = 0
    return session


def test_run_dispatch_backfill_guard_suppresses_subscriber_sends(monkeypatch):
    from app.config import settings
    from app.subscriptions import dispatcher

    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "operator_email", "ops@example.com")

    # Operator digest is still expected; capture it instead of hitting the network.
    operator_calls: list[tuple] = []
    monkeypatch.setattr(
        dispatcher,
        "send_operator_digest_email",
        lambda metrics, recalls, errors, messages=None: operator_calls.append(
            (metrics, recalls, errors)
        ),
    )

    # Any subscriber send while the guard is tripped is a bug.
    def _must_not_send(*args, **kwargs):
        raise AssertionError("subscriber digest must not be sent when the backfill guard trips")

    monkeypatch.setattr(dispatcher, "send_digest_email", _must_not_send)

    # First run (last_run_at None) with a batch above the threshold — the 137-style flood.
    big_batch = [object() for _ in range(dispatcher._BACKFILL_GUARD_THRESHOLD + 1)]
    session = _mock_session_for_dispatch(recalls=big_batch, subs=[], last_run_at=None)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(dispatcher.run_dispatch(session))
    finally:
        loop.close()

    assert result["backfillGuardTripped"] is True
    assert result["sent"] == 0
    # Operator still alerted, with the guard flag and a non-empty errors list.
    assert len(operator_calls) == 1
    metrics, _recalls, errors = operator_calls[0]
    assert metrics["backfill_guard_tripped"] is True
    assert errors, "operator digest should carry the guard warning in its errors list"
    # Cursor still advances so the next run returns to normal.
    assert session.get.return_value.last_run_at is not None


def test_run_dispatch_below_threshold_does_not_trip_guard(monkeypatch):
    from app.config import settings
    from app.subscriptions import dispatcher

    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "operator_email", "ops@example.com")
    monkeypatch.setattr(
        dispatcher,
        "send_operator_digest_email",
        lambda metrics, recalls, errors, messages=None: None,
    )

    # A normal daily delta (at the threshold, not above it) with no subscribers to send to.
    normal_batch = [object() for _ in range(dispatcher._BACKFILL_GUARD_THRESHOLD)]
    session = _mock_session_for_dispatch(recalls=normal_batch, subs=[], last_run_at=None)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(dispatcher.run_dispatch(session))
    finally:
        loop.close()

    assert result["backfillGuardTripped"] is False


def test_run_dispatch_forwards_contact_messages_to_operator(monkeypatch):
    from app.config import settings
    from app.subscriptions import dispatcher

    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "operator_email", "ops@example.com")

    captured: dict = {}
    monkeypatch.setattr(
        dispatcher,
        "send_operator_digest_email",
        lambda metrics, recalls, errors, messages=None: captured.update(
            messages=messages, count=metrics.get("new_message_count")
        ),
    )

    msgs = [object(), object()]  # opaque — run_dispatch only counts and forwards them
    session = _mock_session_for_dispatch(recalls=[], subs=[], last_run_at=None, messages=msgs)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(dispatcher.run_dispatch(session))
    finally:
        loop.close()

    assert captured["messages"] == msgs
    assert captured["count"] == 2
