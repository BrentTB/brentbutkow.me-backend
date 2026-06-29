from types import SimpleNamespace

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
