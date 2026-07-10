"""scripts/check_nrcs.py — the NRCS statement watcher.

Fixtures copy REAL SharePoint API records fetched live from www.nrcs.org.za on 2026-07-10
(CLAUDE.md: fixtures from real feed records; an invented shape once hid a real field bug). News
fixtures carry the fields our $select requests; media fixtures are the full file records.

The DB-touching paths run on in-memory SQLite — nrcs_watch_items is plain-typed (no JSONB), so
only its table is created and the suite stays database-free by default.
"""

from datetime import date

import pytest
from resend.exceptions import ResendError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.recalls.models import NrcsWatchItem
from scripts import check_nrcs
from scripts.check_nrcs import (
    SOURCE_MEDIA,
    SOURCE_NEWS,
    WatchFetchError,
    is_recall_related,
    parse_media_files,
    parse_news_items,
    plan_updates,
    process,
)

# --- Real records: News list, $select=Id,Title,Created ---------------------------------------

NEWS_RECALL_ITEM = {
    "Id": 4,
    "Title": "NRCS orders for National recall of  PILCHARDS tin fish",
    "Created": "2020-08-13T20:28:00Z",
}
NEWS_ORDINARY_ITEM = {
    "Id": 1,
    "Title": "NRCS to host the 9th Annual Building Control Officers’ Convention in Plettenburg Bay",
    "Created": "2020-08-13T20:28:00Z",
}

# --- Real records: Media Release folder, $expand=ListItemAllFields ---------------------------

MEDIA_RECALL_FILE = {
    "CheckInComment": "",
    "CheckOutType": 2,
    "ContentTag": "{B9EF02B8-543F-4557-A799-D972769C27D1},4,2",
    "CustomizedPageStatus": 0,
    "ETag": '"{B9EF02B8-543F-4557-A799-D972769C27D1},4"',
    "Exists": True,
    "IrmEnabled": False,
    "Length": "124488",
    "Level": 1,
    "LinkingUri": None,
    "LinkingUrl": "",
    "MajorVersion": 3,
    "MinorVersion": 0,
    "Name": "NRCS calls for a nationwide recall of ECONO CEMENT final statement 11 December 2023.pdf",  # noqa: E501
    "ServerRelativeUrl": "/Media Release/NRCS calls for a nationwide recall of ECONO CEMENT final statement 11 December 2023.pdf",  # noqa: E501
    "TimeCreated": "2023-12-21T11:02:57Z",
    "TimeLastModified": "2023-12-21T11:03:52Z",
    "Title": "NRCS calls for a nationwide recall of ECONO CEMENT final statement 11 December 2023",
    "UIVersion": 1536,
    "UIVersionLabel": "3.0",
    "UniqueId": "b9ef02b8-543f-4557-a799-d972769c27d1",
    "ListItemAllFields": {
        "FileSystemObjectType": 0,
        "Id": 29,
        "Title": "NRCS calls for a nationwide recall of ECONO CEMENT final statement 11 December 2023",  # noqa: E501
        "OData__dlc_DocId": "NRCS-1756278537-29",
        "ReleaseDate": "2023-12-11T00:00:00",
        "ID": 29,
        "Created": "2023-12-21T13:02:57",
        "Modified": "2023-12-21T13:03:52",
    },
}
MEDIA_ORDINARY_FILE = {
    "Name": "MEDIA RELEASE - MOU CHINA ASMR VM_updated (1).docx",
    "ServerRelativeUrl": "/Media Release/MEDIA RELEASE - MOU CHINA ASMR VM_updated (1).docx",
    "ListItemAllFields": {
        "Id": 52,
        "Title": "MEDIA RELEASE - MOU CHINA ASMR VM_updated (1)",
        "ReleaseDate": "2026-06-26T00:00:00",
    },
}


def _items(*records):
    return parse_news_items([r for r in records if "ListItemAllFields" not in r]) + (
        parse_media_files([r for r in records if "ListItemAllFields" in r])
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_news_items_maps_real_record():
    (item,) = parse_news_items([NEWS_RECALL_ITEM])
    assert item["source"] == SOURCE_NEWS
    assert item["item_key"] == "4"
    assert item["title"] == "NRCS orders for National recall of  PILCHARDS tin fish"
    assert item["release_date"] == date(2020, 8, 13)
    assert item["url"] == "https://www.nrcs.org.za/Pages/SingleNews.aspx?newsId=4"


def test_parse_news_items_skips_record_without_id():
    assert parse_news_items([{"Title": "orphan"}]) == []


def test_parse_media_files_maps_real_record_and_quotes_url():
    (item,) = parse_media_files([MEDIA_RECALL_FILE])
    assert item["source"] == SOURCE_MEDIA
    assert item["item_key"] == "29"
    assert item["release_date"] == date(2023, 12, 11)
    # Statement filenames contain spaces — the emailed link must be percent-encoded.
    assert item["url"] is not None
    assert item["url"].startswith("https://www.nrcs.org.za/Media%20Release/NRCS%20calls")
    assert " " not in item["url"]


def test_parse_media_files_title_falls_back_to_file_name():
    file = {**MEDIA_ORDINARY_FILE, "ListItemAllFields": {"Id": 52, "ReleaseDate": None}}
    (item,) = parse_media_files([file])
    assert item["title"] == "MEDIA RELEASE - MOU CHINA ASMR VM_updated (1).docx"
    assert item["release_date"] is None


def test_is_recall_related_flags_recall_titles_only():
    assert is_recall_related(NEWS_RECALL_ITEM["Title"])
    assert is_recall_related(MEDIA_RECALL_FILE["Title"])
    assert not is_recall_related(NEWS_ORDINARY_ITEM["Title"])


# ---------------------------------------------------------------------------
# Diff plan
# ---------------------------------------------------------------------------


def test_plan_updates_first_run_seeds_silently():
    plan = plan_updates({}, _items(NEWS_RECALL_ITEM, NEWS_ORDINARY_ITEM, MEDIA_RECALL_FILE))
    assert len(plan.seed) == 3
    assert plan.notify == []


def test_plan_updates_new_item_past_first_run_notifies():
    existing = {SOURCE_NEWS: {"1"}, SOURCE_MEDIA: {"29"}}
    plan = plan_updates(existing, _items(NEWS_RECALL_ITEM, NEWS_ORDINARY_ITEM, MEDIA_RECALL_FILE))
    assert plan.seed == []
    assert [i["item_key"] for i in plan.notify] == ["4"]


def test_plan_updates_first_run_is_judged_per_source():
    # News has history but Media Release has never been seen: the media backlog seeds silently
    # while the genuinely new news item still gets emailed — one container's first run must not
    # suppress (or spam) the other.
    existing = {SOURCE_NEWS: {"1"}}
    plan = plan_updates(existing, _items(NEWS_RECALL_ITEM, NEWS_ORDINARY_ITEM, MEDIA_RECALL_FILE))
    assert [i["item_key"] for i in plan.seed] == ["29"]
    assert [i["item_key"] for i in plan.notify] == ["4"]


def test_plan_updates_dedupes_repeated_records_in_one_payload():
    existing = {SOURCE_NEWS: {"1"}}
    plan = plan_updates(existing, _items(NEWS_RECALL_ITEM) + _items(NEWS_RECALL_ITEM))
    assert len(plan.notify) == 1


def test_plan_updates_sorts_notifications_newest_first_none_dates_last():
    existing = {SOURCE_MEDIA: {"0"}}
    dated_old = parse_media_files([MEDIA_RECALL_FILE])[0]  # 2023-12-11
    dated_new = parse_media_files([MEDIA_ORDINARY_FILE])[0]  # 2026-06-26
    undated = {**dated_new, "item_key": "53", "release_date": None}
    plan = plan_updates(existing, [dated_old, undated, dated_new])
    assert [i["item_key"] for i in plan.notify] == ["52", "29", "53"]


# ---------------------------------------------------------------------------
# Fetch retry behavior (the site's observed failure shapes)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes, json_value=None):
        self.content = content
        self._json_value = json_value

    def raise_for_status(self):
        return None

    def json(self):
        if self._json_value is None:
            raise ValueError("not JSON")
        return self._json_value


def test_get_json_retries_empty_200_then_succeeds(monkeypatch):
    responses = iter(
        [
            _FakeResponse(b""),  # the intermittent empty-200 quirk
            _FakeResponse(b'{"value": []}', {"value": []}),
        ]
    )
    sleeps = []
    monkeypatch.setattr(check_nrcs.curl_requests, "get", lambda *a, **k: next(responses))
    monkeypatch.setattr(check_nrcs.time, "sleep", sleeps.append)

    assert check_nrcs._get_json("https://example.invalid", {}) == {"value": []}
    assert sleeps == [check_nrcs._FETCH_RETRY_SLEEP_SECONDS]


def test_get_json_gives_up_on_persistent_runtime_error_page(monkeypatch):
    # During outages the site serves its ASP.NET "Runtime Error" HTML page — sometimes as a 200 —
    # which must exhaust retries and raise, not crash on parse or loop forever.
    calls = []

    def _runtime_error_page(*args, **kwargs):
        calls.append(1)
        return _FakeResponse(b"<!DOCTYPE html><title>Runtime Error</title>")

    monkeypatch.setattr(check_nrcs.curl_requests, "get", _runtime_error_page)
    monkeypatch.setattr(check_nrcs.time, "sleep", lambda _s: None)

    with pytest.raises(WatchFetchError):
        check_nrcs._get_json("https://example.invalid", {})
    assert len(calls) == check_nrcs._FETCH_ATTEMPTS


# ---------------------------------------------------------------------------
# process() — diff, email gating, and record-after-send ordering (SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    # Only the watcher's table — the other models use Postgres-only types (JSONB/TSVECTOR).
    NrcsWatchItem.__table__.create(engine)
    with Session(engine) as session:
        yield session


def _stored_keys(session):
    return set(session.scalars(select(NrcsWatchItem.item_key)))


def _enable_email(monkeypatch, operator="ops@example.com"):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "operator_email", operator)


def test_process_first_run_seeds_backlog_without_emailing(session, monkeypatch):
    _enable_email(monkeypatch)
    sent = []

    summary = process(session, _items(NEWS_RECALL_ITEM, MEDIA_RECALL_FILE), send_email=sent.append)

    assert sent == []
    assert summary == {"fetched": 2, "seeded": 2, "notified": 0, "held": 0, "email_sent": False}
    assert _stored_keys(session) == {"4", "29"}


def test_process_emails_and_records_only_new_items(session, monkeypatch):
    _enable_email(monkeypatch)
    process(session, _items(NEWS_ORDINARY_ITEM, MEDIA_RECALL_FILE), send_email=lambda _i: None)
    sent = []

    summary = process(
        session,
        _items(NEWS_ORDINARY_ITEM, NEWS_RECALL_ITEM, MEDIA_RECALL_FILE),
        send_email=sent.append,
    )

    assert [i["item_key"] for i in sent[0]] == ["4"]
    assert summary == {"fetched": 3, "seeded": 0, "notified": 1, "held": 0, "email_sent": True}
    assert _stored_keys(session) == {"1", "4", "29"}


def test_process_holds_new_items_unseen_when_email_unavailable(session, monkeypatch):
    _enable_email(monkeypatch)
    process(session, _items(NEWS_ORDINARY_ITEM), send_email=lambda _i: None)
    sent = []

    # operator_email unset → the new item must be held back, NOT recorded, so it resurfaces.
    monkeypatch.setattr(settings, "operator_email", None)
    summary = process(session, _items(NEWS_ORDINARY_ITEM, NEWS_RECALL_ITEM), send_email=sent.append)
    assert sent == []
    assert summary == {"fetched": 2, "seeded": 0, "notified": 0, "held": 1, "email_sent": False}
    assert _stored_keys(session) == {"1"}

    # Email restored → the held item surfaces on the next run.
    monkeypatch.setattr(settings, "operator_email", "ops@example.com")
    summary = process(session, _items(NEWS_ORDINARY_ITEM, NEWS_RECALL_ITEM), send_email=sent.append)
    assert [i["item_key"] for i in sent[0]] == ["4"]
    assert summary["notified"] == 1
    assert _stored_keys(session) == {"1", "4"}


def test_process_send_failure_records_nothing_new(session, monkeypatch):
    _enable_email(monkeypatch)
    process(session, _items(NEWS_ORDINARY_ITEM), send_email=lambda _i: None)

    def _boom(_items):
        raise ResendError(code=500, error_type="error", message="HTTP 500", suggested_action="")

    with pytest.raises(ResendError):
        process(session, _items(NEWS_ORDINARY_ITEM, NEWS_RECALL_ITEM), send_email=_boom)
    # The failed notification stays unseen — it must resurface next run, never drop silently.
    assert _stored_keys(session) == {"1"}


# ---------------------------------------------------------------------------
# Resend retry policy (mocks raise the real SDK exception — CLAUDE.md)
# ---------------------------------------------------------------------------


def _resend_error(code: int) -> ResendError:
    return ResendError(code=code, error_type="error", message=f"HTTP {code}", suggested_action="")


def test_send_with_retry_permanent_4xx_raises_immediately(monkeypatch):
    attempts = []

    def _reject(_params):
        attempts.append(1)
        raise _resend_error(422)

    monkeypatch.setattr(check_nrcs.resend.Emails, "send", _reject)
    monkeypatch.setattr(check_nrcs.time, "sleep", lambda _s: pytest.fail("must not sleep"))

    with pytest.raises(ResendError):
        check_nrcs._send_with_retry({})
    assert len(attempts) == 1


def test_send_with_retry_transient_5xx_retries_then_succeeds(monkeypatch):
    outcomes = iter([_resend_error(503), None])
    sleeps = []

    def _flaky(_params):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(check_nrcs.resend.Emails, "send", _flaky)
    monkeypatch.setattr(check_nrcs.time, "sleep", sleeps.append)

    check_nrcs._send_with_retry({})
    assert sleeps == [check_nrcs.RETRY_DELAYS[0]]


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------


def test_watch_html_escapes_titles_and_caps_listed_items():
    hostile = {
        "source": SOURCE_NEWS,
        "item_key": "999",
        "title": '<script>alert("x")</script>',
        "release_date": None,
        "url": None,
    }
    many = [dict(hostile, item_key=str(n)) for n in range(check_nrcs._EMAIL_MAX_ITEMS + 5)]

    body = check_nrcs._watch_html([hostile])
    assert "<script>" not in body
    assert "&lt;script&gt;" in body

    capped = check_nrcs._watch_html(many)
    assert "and 5 more" in capped
