"""NRCS statement watcher — email the operator when the NRCS publishes something new.

South Africa's NRCS is the recall gap no feed or alert covers: its site is a SharePoint SPA whose
page content Google never indexes (the May-2024 canned-molluscs recall left zero trace in the
search index), and its statements reach neither the NCC feed nor any API we ingest. The curated SA
list (app/modules/recalls/seed_za.py) is maintained by hand, so the missing piece is *hearing
about* new NRCS statements. This script polls the same SharePoint REST endpoints the site's own
pages call — the News list and the Media Release document library, where past recall statements
live (the old /recalls page is gone; it 404s) — and emails the operator every item it hasn't seen
before. Notify-only: it never writes to `recalls`; a human decides what joins the curated list.

Mass-send safety (CLAUDE.md): the first sight of a source seeds its current backlog silently —
"first run" is a normal production state and ~90 historical statements must never become ~90 email
lines. Past first run, new items are recorded as seen only after the notification email is handed
to Resend, so a disabled or failed send leaves them unseen to resurface next run rather than drop
silently.

Observed site quirks this must survive: the API intermittently answers HTTP 200 with an empty body
(retry), serves its ASP.NET "Runtime Error" HTML page during outages — sometimes as a 200 (retry,
then give up) — and the whole site vanishes for hours at a time, so an unreachable source skips
this run without failing the job (its items are still new tomorrow). A Resend failure, by
contrast, propagates and fails the job: that's an actionable outage worth a GitHub notification.

Run: `python -m scripts.check_nrcs` (daily via .github/workflows/ingest.yml; the first manual or
scheduled run just seeds the backlog).
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, TypedDict
from urllib.parse import quote

import resend
from curl_cffi import requests as curl_requests
from resend.exceptions import ResendError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.modules.recalls.models import NrcsWatchItem

# Importing the email module also configures resend.api_key from settings (or logs that email is
# disabled) — the same setup path the dispatcher relies on.
from app.subscriptions.email import RETRY_DELAYS, email_disabled, is_permanent_failure

logger = logging.getLogger(__name__)

_BASE = "https://www.nrcs.org.za"
# The same OData calls the site's own pages embed in inline scripts; odata=nometadata keeps the
# payload plain JSON. $top is generous headroom — both containers hold well under 100 items.
_NEWS_ENDPOINT = f"{_BASE}/_api/web/lists/GetByTitle('News')/items"
_MEDIA_ENDPOINT = f"{_BASE}/_api/web/GetFolderByServerRelativeUrl('/Media%20Release')/files"
_ACCEPT_JSON = "application/json; odata=nometadata"

SOURCE_NEWS = "news"
SOURCE_MEDIA = "media_release"

_FETCH_ATTEMPTS = 4
_FETCH_RETRY_SLEEP_SECONDS = 5

# Cap on the items *listed* in one email — everything is still recorded and counted in the subject.
# Only reachable if the NRCS bulk re-imports a container (new ids); normal volume is ~1-2/month.
_EMAIL_MAX_ITEMS = 30

# Highlight-only hint that a new statement is recall-shaped (the email sends either way — NRCS
# volume is low enough to list every new statement, so a title this misses is still seen).
_RECALL_HINT = re.compile(
    r"recall|withdraw|unsafe|contaminat|listeri|salmonell|aflatoxin|botul|food safety",
    re.IGNORECASE,
)


class WatchItem(TypedDict):
    source: str
    item_key: str
    title: str
    release_date: date | None
    url: str | None


class WatchFetchError(RuntimeError):
    """The NRCS API stayed unreachable/broken across every retry — skip this source this run."""


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def _get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    """GET a SharePoint OData endpoint, retrying the site's two observed failure shapes.

    Empty-200 bodies and the ASP.NET "Runtime Error" HTML page (which json() rejects) both retry;
    so do transport errors and non-2xx. Only the failure *class* is logged — never str(exc), which
    embeds the request URL (CLAUDE.md).
    """
    last_error = "no attempt made"
    for attempt in range(_FETCH_ATTEMPTS):
        if attempt:
            time.sleep(_FETCH_RETRY_SLEEP_SECONDS)
        try:
            response = curl_requests.get(
                url,
                params=params,
                headers={"Accept": _ACCEPT_JSON},
                impersonate="chrome",
                timeout=45,
            )
            response.raise_for_status()
            if not response.content:
                last_error = "empty 200 body"
                continue
            payload = response.json()
        except (curl_requests.exceptions.RequestException, ValueError) as exc:
            last_error = type(exc).__name__
            continue
        if not isinstance(payload, dict):
            last_error = "non-object JSON payload"
            continue
        return payload
    raise WatchFetchError(f"gave up after {_FETCH_ATTEMPTS} attempts (last: {last_error})")


def parse_news_items(items: list[dict[str, Any]]) -> list[WatchItem]:
    parsed: list[WatchItem] = []
    for item in items:
        item_id = item.get("Id")
        if item_id is None:
            continue
        key = str(item_id)
        parsed.append(
            {
                "source": SOURCE_NEWS,
                "item_key": key,
                "title": (item.get("Title") or "").strip() or "(untitled)",
                "release_date": _parse_date(item.get("Created")),
                # The site's own article page for a News item; the id is feed data, so quote it.
                "url": f"{_BASE}/Pages/SingleNews.aspx?newsId={quote(key, safe='')}",
            }
        )
    return parsed


def parse_media_files(files: list[dict[str, Any]]) -> list[WatchItem]:
    parsed: list[WatchItem] = []
    for file in files:
        fields = file.get("ListItemAllFields") or {}
        relative_url = file.get("ServerRelativeUrl")
        item_id = fields.get("Id")
        # List-item id is the stable key; a file odd enough to lack one falls back to its path.
        key = str(item_id) if item_id is not None else relative_url
        if not key:
            continue
        url = None
        if relative_url:
            # Statement filenames contain spaces (and whatever else) — quote the path, keeping the
            # segment separators (CLAUDE.md: feed values interpolated into URLs get quoted).
            url = f"{_BASE}{quote(relative_url, safe='/')}"
        parsed.append(
            {
                "source": SOURCE_MEDIA,
                "item_key": key,
                "title": (fields.get("Title") or file.get("Name") or "").strip() or "(untitled)",
                "release_date": _parse_date(fields.get("ReleaseDate")),
                "url": url,
            }
        )
    return parsed


def _parse_date(value: Any) -> date | None:
    # SharePoint dates arrive as "2023-12-11T00:00:00" (sometimes with a trailing Z); only the
    # date part matters here, and a malformed value must not sink the whole item.
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_news() -> list[WatchItem]:
    payload = _get_json(_NEWS_ENDPOINT, params={"$select": "Id,Title,Created", "$top": "500"})
    return parse_news_items(payload.get("value") or [])


def fetch_media_releases() -> list[WatchItem]:
    payload = _get_json(_MEDIA_ENDPOINT, params={"$expand": "ListItemAllFields", "$top": "500"})
    return parse_media_files(payload.get("value") or [])


_FETCHERS: tuple[tuple[str, Callable[[], list[WatchItem]]], ...] = (
    (SOURCE_NEWS, fetch_news),
    (SOURCE_MEDIA, fetch_media_releases),
)


# ---------------------------------------------------------------------------
# Diff plan
# ---------------------------------------------------------------------------


def is_recall_related(title: str) -> bool:
    return bool(_RECALL_HINT.search(title))


@dataclass
class Plan:
    seed: list[WatchItem]  # first sight of a source — record its backlog silently
    notify: list[WatchItem]  # genuinely new past first run — email, then record


def plan_updates(existing: dict[str, set[str]], fetched: list[WatchItem]) -> Plan:
    """Split fetched items into silent first-run seeds and email-worthy news.

    First run is judged per source: seeding one container's history must not suppress (or spam)
    the other — same per-scope principle as the dispatch backfill guard (CLAUDE.md).
    """
    seed: list[WatchItem] = []
    notify: list[WatchItem] = []
    seen_this_run: set[tuple[str, str]] = set()
    for item in fetched:
        pair = (item["source"], item["item_key"])
        if pair in seen_this_run:  # defensive: a duplicate in one payload must not double-insert
            continue
        seen_this_run.add(pair)
        if item["item_key"] in existing.get(item["source"], set()):
            continue
        if existing.get(item["source"]):
            notify.append(item)
        else:
            seed.append(item)
    notify.sort(key=lambda item: item["release_date"] or date.min, reverse=True)
    return Plan(seed=seed, notify=notify)


def load_existing_keys(session: Session) -> dict[str, set[str]]:
    existing: dict[str, set[str]] = {}
    for source, item_key in session.execute(select(NrcsWatchItem.source, NrcsWatchItem.item_key)):
        existing.setdefault(source, set()).add(item_key)
    return existing


def _insert_items(session: Session, items: list[WatchItem]) -> None:
    session.add_all(
        NrcsWatchItem(
            source=item["source"],
            item_key=item["item_key"],
            title=item["title"],
            release_date=item["release_date"],
            url=item["url"],
        )
        for item in items
    )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _send_with_retry(params: resend.Emails.SendParams) -> None:
    """Sync twin of app.subscriptions.email.send_with_retry (async is for the request path; this
    script is synchronous). Same policy: permanent 4xx raises immediately, 429/5xx retries."""
    last_exc: ResendError | None = None
    for delay in (None, *RETRY_DELAYS):
        if delay is not None:
            time.sleep(delay)
        try:
            resend.Emails.send(params)
            return
        except ResendError as exc:
            if is_permanent_failure(exc):
                raise
            last_exc = exc
    assert last_exc is not None  # always set when the retry loop exhausts
    raise last_exc


def send_watch_email(items: list[WatchItem]) -> None:
    operator_email = (settings.operator_email or "").strip()
    if not operator_email:
        # process() gates on this before calling; failing loudly beats silently "sending" nowhere.
        raise RuntimeError("operator_email is not configured")
    recall_count = sum(1 for item in items if is_recall_related(item["title"]))
    today = datetime.now(UTC).date().isoformat()
    subject = f"NRCS watch: {len(items)} new statement(s), {recall_count} recall-related — {today}"
    _send_with_retry(
        {
            "from": settings.resend_from_address,
            "to": [operator_email],
            "subject": subject,
            "html": _watch_html(items),
        }
    )


def _watch_html(items: list[WatchItem]) -> str:
    shown = items[:_EMAIL_MAX_ITEMS]
    rows = "".join(_item_row(item) for item in shown)
    overflow = len(items) - len(shown)
    overflow_note = ""
    if overflow:
        overflow_note = (
            f'<p style="margin:12px 0 0 0;font-size:13px;color:#888888;">'
            f"&hellip;and {overflow} more (all recorded; likely a bulk re-import on the NRCS "
            f"site rather than {overflow} real statements).</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:6px;overflow:hidden;
                      max-width:600px;width:100%;">
          <tr>
            <td style="background:#1a1a2e;padding:24px 32px;">
              <span style="color:#ffffff;font-size:20px;font-weight:bold;">
                Recall Radar &mdash; NRCS watch
              </span>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 32px;">
              <p style="margin:0 0 16px 0;font-size:14px;color:#444444;line-height:1.6;">
                New statements on the NRCS site since the last check. If one is a food recall,
                add it to the curated SA list (app/modules/recalls/seed_za.py) by hand.
              </p>
              {rows}
              {overflow_note}
            </td>
          </tr>
          <tr>
            <td style="background:#f9f9f9;padding:20px 32px;border-top:1px solid #e8e8e8;">
              <p style="margin:0;font-size:12px;color:#aaaaaa;line-height:1.5;">
                Watching the NRCS News list and Media Release library via their SharePoint API.
                Notify-only &mdash; nothing is ingested automatically.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _item_row(item: WatchItem) -> str:
    # Title and URL are external-feed values — escape both before interpolation.
    title = html.escape(item["title"], quote=True)
    when = item["release_date"].isoformat() if item["release_date"] else "date unknown"
    badge = ""
    if is_recall_related(item["title"]):
        badge = '<strong style="color:#c0392b;">[recall?]</strong>&nbsp;'
    link = ""
    if item["url"]:
        url = html.escape(item["url"], quote=True)
        link = (
            f'<br><a href="{url}" style="color:#1a1a2e;font-size:12px;'
            f'text-decoration:underline;">{url}</a>'
        )
    return (
        f'<div style="padding:10px 0;border-bottom:1px solid #f0f0f0;font-size:14px;'
        f'color:#333333;">'
        f"{badge}<strong>{title}</strong> &middot; {when}{link}"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def process(
    session: Session,
    fetched: list[WatchItem],
    send_email: Callable[[list[WatchItem]], None] = send_watch_email,
) -> dict[str, Any]:
    """Diff fetched items against the seen table; email the new ones, then record them.

    Every return path carries the same summary keys (CLAUDE.md). Order matters: the email goes out
    *before* the notify items are recorded, so a send failure leaves them unseen to resurface next
    run — a crash after the send can at worst repeat an email, never lose one.
    """
    plan = plan_updates(load_existing_keys(session), fetched)
    summary: dict[str, Any] = {
        "fetched": len(fetched),
        "seeded": len(plan.seed),
        "notified": 0,
        "held": 0,
        "email_sent": False,
    }
    if plan.notify and (email_disabled() or not (settings.operator_email or "").strip()):
        # No way to notify — record only the silent seeds and hold the new items back unseen, so
        # they surface (rather than vanish) once email is configured again.
        logger.warning(
            "NRCS watch: %d new statement(s) held — email disabled or operator_email unset.",
            len(plan.notify),
        )
        summary["held"] = len(plan.notify)
        _insert_items(session, plan.seed)
        session.commit()
        return summary
    if plan.notify:
        send_email(plan.notify)
        summary["notified"] = len(plan.notify)
        summary["email_sent"] = True
    _insert_items(session, plan.seed + plan.notify)
    session.commit()
    return summary


def main() -> None:
    fetched: list[WatchItem] = []
    skipped: list[str] = []
    for source, fetcher in _FETCHERS:
        try:
            fetched.extend(fetcher())
        except WatchFetchError as exc:
            # The NRCS site disappears for hours at a time — an unreachable source skips this run
            # rather than failing the job; its items are still new next run.
            skipped.append(source)
            logger.warning("NRCS %s unreachable, skipping this run: %s", source, exc)
    if skipped and not fetched:
        print(f"NRCS watch: site unreachable ({', '.join(skipped)} skipped); nothing checked.")
        return

    session = SessionLocal()
    try:
        summary = process(session, fetched)
    finally:
        session.close()

    skipped_part = f" ({', '.join(skipped)} unreachable, skipped)" if skipped else ""
    print(
        f"NRCS watch: {summary['fetched']} item(s) fetched{skipped_part}, "
        f"{summary['seeded']} seeded silently, {summary['notified']} emailed, "
        f"{summary['held']} held (email unavailable)."
    )


if __name__ == "__main__":
    main()
