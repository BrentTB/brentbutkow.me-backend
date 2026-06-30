import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from resend.exceptions import ResendError

from app.subscriptions import email as email_module


def _recall(**overrides) -> SimpleNamespace:
    base = {
        "source": "fda",
        "recall_number": "F-001",
        "product_description": "Plain product",
        "company_name": None,
        "country": "us",
        "category": "allergen",
        "severity_label": "high",
        "source_url": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_recall_card_escapes_dynamic_fields():
    recall = _recall(
        product_description="<script>alert(1)</script> & peanuts",
        company_name="A & B <b>",
    )
    html = email_module._recall_card(recall)

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; peanuts" in html
    assert "A &amp; B &lt;b&gt;" in html


def test_digest_html_escapes_recall_and_skipped_dates():
    subscription = SimpleNamespace(
        management_token="tok",
        email="x@example.com",
        skipped_at=["2026-06-01 <evil>"],
    )
    recall = _recall(product_description="<img src=x onerror=alert(1)> & co")
    html = email_module._digest_html(
        subscription,
        [recall],
        manage_url="https://brentbutkow.me/m?token=tok",
        unsub_url="https://brentbutkow.me/u?token=tok",
    )

    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; co" in html
    # The skipped-date notice is escaped too.
    assert "<evil>" not in html
    assert "&lt;evil&gt;" in html


def _message(**overrides) -> SimpleNamespace:
    base = {
        "name": "Ada",
        "email": "ada@example.com",
        "message": "Hello there",
        "created_at": datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_operator_message_row_escapes_visitor_fields():
    row = email_module._operator_message_row(
        _message(name="<b>x</b>", email="a&b@e.com", message="<script>evil()</script> & co")
    )
    assert "<script>" not in row
    assert "&lt;script&gt;evil()&lt;/script&gt; &amp; co" in row
    assert "<b>x</b>" not in row
    assert "a&amp;b@e.com" in row


def test_operator_digest_html_includes_messages_section():
    html = email_module._operator_digest_html(
        metrics={"new_message_count": 1},
        recalls=[],
        errors=[],
        messages=[_message(message="Please call me <urgent>")],
    )
    assert "Contact messages" in html
    assert "Please call me &lt;urgent&gt;" in html
    assert "Contact messages this run" in html  # metrics row


def test_operator_digest_html_no_messages_placeholder():
    html = email_module._operator_digest_html(
        metrics={"new_message_count": 0}, recalls=[], errors=[], messages=[]
    )
    assert "No new messages this run." in html


def test_operator_recall_row_escapes_fields_and_source_url():
    recall = _recall(
        product_description="<b>boom</b>",
        source_url="https://example.com/r?a=1&b=2",
    )
    row = email_module._operator_recall_row(recall)

    assert "<b>boom</b>" not in row
    assert "&lt;b&gt;boom&lt;/b&gt;" in row
    # & in the source URL is escaped in both the href and the link text.
    assert "a=1&amp;b=2" in row
    assert "a=1&b=2" not in row


def test_recall_detail_url_encodes_path_segments():
    # A recall_number carrying URL-structural characters must not rewrite the link target.
    recall = _recall(source="fda", recall_number="../../evil?x=1")
    url = email_module._recall_detail_url(recall)

    assert url == "https://brentbutkow.me/projects/recall-radar/fda/..%2F..%2Fevil%3Fx%3D1"
    # The card embeds the encoded URL, never the raw traversal/query characters.
    card = email_module._recall_card(recall)
    assert "/evil?x=1" not in card


# ---------------------------------------------------------------------------
# send_with_retry — Resend SDK raises ResendError (with an HTTP-status .code),
# never httpx exceptions. These guard the retry/classification contract.
# ---------------------------------------------------------------------------


def _run(coro):
    # Use an isolated loop rather than asyncio.run() — the latter resets the global event loop
    # to None, which breaks sibling tests that rely on asyncio.get_event_loop() auto-creating one.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resend_error(code: int) -> ResendError:
    return ResendError(code=code, error_type="error", message=f"HTTP {code}", suggested_action="")


def _record_async(sink: list):
    async def _sleep(delay):
        sink.append(delay)

    return _sleep


def test_send_with_retry_permanent_4xx_does_not_retry(monkeypatch):
    calls: list = []
    sleeps: list = []
    monkeypatch.setattr(email_module.asyncio, "sleep", _record_async(sleeps))

    async def _send():
        calls.append(1)
        raise _resend_error(400)

    with pytest.raises(ResendError):
        _run(email_module.send_with_retry(_send))

    assert len(calls) == 1  # permanent client error → no retry
    assert sleeps == []


def test_send_with_retry_retries_transient_then_raises(monkeypatch):
    calls: list = []
    sleeps: list = []
    monkeypatch.setattr(email_module.asyncio, "sleep", _record_async(sleeps))

    async def _send():
        calls.append(1)
        raise _resend_error(503)

    with pytest.raises(ResendError):
        _run(email_module.send_with_retry(_send))

    assert len(calls) == len(email_module.RETRY_DELAYS) + 1  # initial attempt + one per delay
    assert sleeps == email_module.RETRY_DELAYS


def test_send_with_retry_succeeds_after_transient(monkeypatch):
    calls: list = []
    monkeypatch.setattr(email_module.asyncio, "sleep", _record_async([]))

    async def _send():
        calls.append(1)
        if len(calls) == 1:
            raise _resend_error(429)  # rate limit is transient and retried
        return "ok"

    result = _run(email_module.send_with_retry(_send))

    assert result == "ok"
    assert len(calls) == 2
