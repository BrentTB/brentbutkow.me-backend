"""
app/subscriptions/email.py
Resend SDK wrapper and HTML email templates for Recall Radar subscriptions.

Configuration
-------------
- If the `resend` package is not installed → ImportError is raised immediately.
- All settings come from app.config (resend_api_key / resend_from_address / operator_email).
- resend_api_key absent → email_disabled() returns True: sends are silently skipped.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime
from urllib.parse import quote

# Raise ImportError at import time if resend is unavailable.
try:
    import resend
    from resend.exceptions import ResendError
except ModuleNotFoundError as _e:
    raise ImportError(
        "The 'resend' package is required for email delivery. Install it with: pip install resend"
    ) from _e

from app.config import settings

logger = logging.getLogger(__name__)

if settings.resend_api_key:
    resend.api_key = settings.resend_api_key
else:
    logger.warning(
        "resend_api_key is not set — email sending is disabled. "
        "Subscription creation will still succeed; confirmation emails will be silently skipped."
    )


def email_disabled() -> bool:
    """Email sending is disabled when no API key is configured. Read live so tests can override."""
    return not settings.resend_api_key


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

RETRY_DELAYS = [5, 10, 20]  # backoff (seconds) before each retry — 4 attempts total


def status_code(exc: ResendError) -> int:
    """Coerce ResendError.code (documented as str | int) to an int HTTP status, default 500."""
    try:
        return int(exc.code)
    except (TypeError, ValueError):
        return 500


def is_permanent_failure(exc: ResendError) -> bool:
    """A 4xx other than 429 is a permanent client error (bad recipient, invalid payload).

    429 (rate limit / quota) and 5xx are transient and worth retrying.
    """
    code = status_code(exc)
    return 400 <= code < 500 and code != 429


async def send_with_retry(send_fn, *args, **kwargs):
    """
    Call send_fn(*args, **kwargs) with backoff on transient Resend failures.

    The resend SDK wraps every failure — API errors and transport errors alike — in a
    resend.exceptions.ResendError carrying an HTTP-status `.code`. Permanent client errors
    (4xx except 429) re-raise immediately; 429 and 5xx are retried before the delays in
    RETRY_DELAYS. After the final attempt the last exception re-raises.
    """
    last_exc: ResendError | None = None
    # One initial attempt, then one retry per backoff delay.
    for delay in (None, *RETRY_DELAYS):
        if delay is not None:
            await asyncio.sleep(delay)
        try:
            return await send_fn(*args, **kwargs)
        except ResendError as exc:
            if is_permanent_failure(exc):
                raise
            last_exc = exc

    assert last_exc is not None  # always set when the retry loop exhausts
    raise last_exc


# ---------------------------------------------------------------------------
# Opt-in email
# ---------------------------------------------------------------------------


def send_optin_email(email: str, raw_token: str) -> None:
    """
    Send the double opt-in confirmation email.

    Silently skipped when email is disabled. No unsubscribe link is shown — a pre-confirmation
    subscription has nothing to manage, and ignoring this email is itself the opt-out.

    Parameters
    ----------
    email:      Recipient's email address.
    raw_token:  Raw (unhashed) confirmation token.
    """
    if email_disabled():
        return

    confirm_url = f"https://brentbutkow.me/projects/recall-radar/confirm?token={raw_token}"

    html = _optin_html(confirm_url=confirm_url)

    resend.Emails.send(
        {
            "from": settings.resend_from_address,
            "to": [email],
            "subject": "Confirm your Recall Radar alert subscription",
            "html": html,
        }
    )


def _optin_html(confirm_url: str) -> str:
    # Escape before interpolating into the href — defence in depth even though the token is
    # URL-safe by construction.
    confirm_url = _html_escape(confirm_url)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f5f5f5;padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:6px;overflow:hidden;
                      max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:#1a1a2e;padding:24px 32px;">
              <span style="color:#ffffff;font-size:20px;font-weight:bold;
                           letter-spacing:0.5px;">
                Recall Radar
              </span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px;">
              <h1 style="margin:0 0 16px 0;font-size:22px;color:#1a1a2e;">
                Confirm your subscription
              </h1>
              <p style="margin:0 0 24px 0;font-size:15px;color:#444444;line-height:1.6;">
                You asked to receive food-recall alerts from Recall Radar.
                Click the button below to activate your subscription.
              </p>

              <!-- CTA button -->
              <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
                <tr>
                  <td style="background:#1a1a2e;border-radius:4px;">
                    <a href="{confirm_url}"
                       style="display:inline-block;padding:12px 24px;color:#ffffff;
                              text-decoration:none;font-size:15px;font-weight:bold;">
                      Confirm my subscription
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 8px 0;font-size:14px;color:#666666;line-height:1.5;">
                This link expires in 72 hours. If you didn&#39;t subscribe, you can safely ignore
                this email.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f9f9f9;padding:20px 32px;border-top:1px solid #e8e8e8;">
              <p style="margin:0 0 8px 0;font-size:13px;color:#888888;">
                <a href="https://brentbutkow.me"
                   style="color:#888888;text-decoration:underline;">brentbutkow.me</a>
              </p>
              <p style="margin:0;font-size:12px;color:#aaaaaa;line-height:1.5;">
                Recall alerts are best-effort and sent via a free service. Always check
                FDA&nbsp;/&nbsp;FSIS&nbsp;/&nbsp;FSA&nbsp;/&nbsp;NCC for official notices.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Preference-update confirmation
# ---------------------------------------------------------------------------


def send_update_confirm_email(email: str, raw_token: str, management_token: str) -> None:
    """
    Email a confirmed subscriber a link to confirm a staged preference change.

    Silently skipped when email is disabled. The change only takes effect once this link is
    followed, so an unauthenticated request can never alter a live subscription on its own.
    """
    if email_disabled():
        return

    confirm_url = f"https://brentbutkow.me/projects/recall-radar/confirm?token={raw_token}"
    manage_url = f"https://brentbutkow.me/projects/recall-radar/manage?token={management_token}"
    unsub_url = f"https://brentbutkow.me/projects/recall-radar/unsubscribe?token={management_token}"

    resend.Emails.send(
        {
            "from": settings.resend_from_address,
            "to": [email],
            "subject": "Confirm your Recall Radar preference update",
            "html": _update_confirm_html(
                confirm_url=confirm_url, manage_url=manage_url, unsub_url=unsub_url
            ),
        }
    )


def _update_confirm_html(confirm_url: str, manage_url: str, unsub_url: str) -> str:
    confirm_url = _html_escape(confirm_url)
    manage_url = _html_escape(manage_url)
    unsub_url = _html_escape(unsub_url)
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
              <span style="color:#ffffff;font-size:20px;font-weight:bold;">Recall Radar</span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <h1 style="margin:0 0 16px 0;font-size:22px;color:#1a1a2e;">
                Confirm your preference update
              </h1>
              <p style="margin:0 0 24px 0;font-size:15px;color:#444444;line-height:1.6;">
                Someone asked to update the filters on your Recall Radar alerts. Click below to
                apply the change. Your current alerts stay as they are until you do. If this
                wasn&#39;t you, ignore this email and nothing changes.
              </p>
              <table cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
                <tr>
                  <td style="background:#1a1a2e;border-radius:4px;">
                    <a href="{confirm_url}"
                       style="display:inline-block;padding:12px 24px;color:#ffffff;
                              text-decoration:none;font-size:15px;font-weight:bold;">
                      Confirm preference update
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#f9f9f9;padding:20px 32px;border-top:1px solid #e8e8e8;">
              <p style="margin:0 0 8px 0;font-size:13px;color:#888888;">
                <a href="{manage_url}"
                   style="color:#888888;text-decoration:underline;">Manage preferences</a>
                &nbsp;&middot;&nbsp;
                <a href="{unsub_url}"
                   style="color:#888888;text-decoration:underline;">Unsubscribe</a>
                &nbsp;&middot;&nbsp;
                <a href="https://brentbutkow.me"
                   style="color:#888888;text-decoration:underline;">brentbutkow.me</a>
              </p>
              <p style="margin:0;font-size:12px;color:#aaaaaa;line-height:1.5;">
                Recall alerts are best-effort and sent via a free service. Always check
                FDA&nbsp;/&nbsp;FSIS&nbsp;/&nbsp;FSA&nbsp;/&nbsp;NCC for official notices.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Digest email
# ---------------------------------------------------------------------------


def send_digest_email(subscription, matching_recalls: list) -> None:
    """
    Send the subscriber daily digest email.

    Silently skipped when email is disabled.

    Parameters
    ----------
    subscription:      Subscription ORM instance.
    matching_recalls:  List of Recall ORM instances that matched the subscription.
    """
    if email_disabled():
        return

    count = len(matching_recalls)
    subject = f"Recall Radar: {count} new recall(s) match your alert"
    manage_url = (
        f"https://brentbutkow.me/projects/recall-radar/manage?token={subscription.management_token}"
    )
    unsub_url = (
        f"https://brentbutkow.me/projects/recall-radar/unsubscribe"
        f"?token={subscription.management_token}"
    )

    html = _digest_html(
        subscription=subscription,
        matching_recalls=matching_recalls,
        manage_url=manage_url,
        unsub_url=unsub_url,
    )

    resend.Emails.send(
        {
            "from": settings.resend_from_address,
            "to": [subscription.email],
            "subject": subject,
            "html": html,
        }
    )


def _digest_html(subscription, matching_recalls: list, manage_url: str, unsub_url: str) -> str:
    today = datetime.now(UTC).date().isoformat()
    count = len(matching_recalls)
    manage_url = _html_escape(manage_url)
    unsub_url = _html_escape(unsub_url)

    # skipped_at notice block
    skipped_notice = ""
    if subscription.skipped_at:
        dates_str = ", ".join(_html_escape(str(d)) for d in subscription.skipped_at)
        skipped_notice = f"""
          <tr>
            <td style="padding:0 32px 20px 32px;">
              <div style="background:#fff8e1;border-left:4px solid #f9a825;
                          padding:12px 16px;border-radius:0 4px 4px 0;">
                <p style="margin:0;font-size:14px;color:#555555;line-height:1.5;">
                  <strong>&#9888; Note:</strong> due to sending limits, you may have missed
                  alerts on {dates_str}. Check the official agency channels
                  (FDA, FSIS, FSA, NCC) for those dates.
                </p>
              </div>
            </td>
          </tr>"""

    # Per-recall cards
    recall_cards = "".join(_recall_card(r) for r in matching_recalls)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f5f5f5;padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:6px;overflow:hidden;
                      max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:#1a1a2e;padding:24px 32px;">
              <span style="color:#ffffff;font-size:20px;font-weight:bold;">
                Recall Radar &mdash; {count} new recall(s) match your alert
              </span>
              <br>
              <span style="color:#aaaacc;font-size:13px;">{today}</span>
            </td>
          </tr>

          <!-- Manage / unsubscribe links — placed before the recall list -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding-right:16px;">
                    <a href="{manage_url}"
                       style="display:inline-block;padding:8px 16px;background:#1a1a2e;
                              color:#ffffff;text-decoration:none;font-size:13px;
                              border-radius:4px;">
                      Manage preferences
                    </a>
                  </td>
                  <td>
                    <a href="{unsub_url}"
                       style="display:inline-block;padding:8px 16px;background:#e8e8e8;
                              color:#444444;text-decoration:none;font-size:13px;
                              border-radius:4px;">
                      Unsubscribe
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- skipped_at notice (conditional) -->
          {skipped_notice}

          <!-- Divider -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <hr style="border:none;border-top:1px solid #e8e8e8;margin:0;">
            </td>
          </tr>

          <!-- Recall cards -->
          <tr>
            <td style="padding:0 32px;">
              {recall_cards}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f9f9f9;padding:20px 32px;border-top:1px solid #e8e8e8;">
              <p style="margin:0 0 8px 0;font-size:13px;color:#888888;line-height:1.5;">
                Recall alerts are best-effort and sent via a free service.
                Always treat FDA&nbsp;/&nbsp;FSIS&nbsp;/&nbsp;FSA&nbsp;/&nbsp;NCC as the
                source of truth.
              </p>
              <p style="margin:0;font-size:13px;color:#888888;">
                <a href="{unsub_url}"
                   style="color:#888888;text-decoration:underline;">Unsubscribe</a>
                &nbsp;&middot;&nbsp;
                <a href="{manage_url}"
                   style="color:#888888;text-decoration:underline;">Manage preferences</a>
                &nbsp;&middot;&nbsp;
                <a href="https://brentbutkow.me"
                   style="color:#888888;text-decoration:underline;">brentbutkow.me</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _recall_detail_url(recall) -> str:
    """Build the canonical recall-detail URL with each path segment percent-encoded.

    source and recall_number come from external feeds; quoting each segment (safe="") stops a
    stray '/', '?', '#' or '@' in the value from rewriting the link target a subscriber clicks.
    """
    source = quote(str(recall.source), safe="")
    recall_number = quote(str(recall.recall_number), safe="")
    return f"https://brentbutkow.me/projects/recall-radar/{source}/{recall_number}"


def _recall_card(recall) -> str:
    """Return an HTML card for a single recall (inline styles only).

    Every recall field comes from external ingest feeds, so each is HTML-escaped before
    interpolation to keep feed content from breaking or injecting markup.
    """
    detail_url = _html_escape(_recall_detail_url(recall))
    company_row = ""
    if recall.company_name:
        company = _html_escape(recall.company_name)
        company_row = f'<span style="color:#555555;font-size:13px;">Company: {company}</span><br>'

    product_description = _html_escape(recall.product_description)
    country = _html_escape(recall.country)
    category = _html_escape(recall.category)
    severity_label = _html_escape(recall.severity_label)

    return f"""
      <div style="padding:16px 0;border-bottom:1px solid #f0f0f0;">
        <p style="margin:0 0 6px 0;font-size:15px;font-weight:bold;color:#1a1a2e;">
          {product_description}
        </p>
        {company_row}
        <span style="color:#555555;font-size:13px;">
          Country: {country}
          &nbsp;&middot;&nbsp;
          Category: {category}
          &nbsp;&middot;&nbsp;
          Severity: {severity_label}
        </span>
        <br>
        <a href="{detail_url}"
           style="color:#1a1a2e;font-size:13px;text-decoration:underline;">
          &rarr; View recall
        </a>
      </div>"""


# ---------------------------------------------------------------------------
# Operator digest email
# ---------------------------------------------------------------------------


def send_operator_digest_email(
    metrics: dict, recalls: list, errors: list[str], messages: list | None = None
) -> None:
    """
    Send the operator summary email.

    Silently skipped when email is disabled.
    Also skipped (with WARNING) when operator_email is absent.

    Parameters
    ----------
    metrics:  Dict with keys: new_recall_count, new_message_count, total_active,
              will_receive_count, skipped_count, stale_pending_count, oldest_last_digest_at, and
              (when the per-country backfill guard trips) suppressed_recall_count +
              suppressed_countries.
    recalls:  The dispatchable new Recall ORM instances this run (guard-suppressed countries are
              excluded; their counts are reported via the suppressed_* metrics instead).
    errors:   List of ERROR/WARNING message strings collected during the run.
    messages: New (non-spam) contact-form Message instances since the last run.
    """
    if email_disabled():
        return

    operator_email = (settings.operator_email or "").strip()
    if not operator_email:
        logger.warning("operator_email is not set — operator digest will not be sent.")
        return

    messages = messages or []
    today_date = datetime.now(UTC).date().isoformat()
    new_count = metrics["new_recall_count"]
    will_receive = metrics["will_receive_count"]
    guard_prefix = "[BACKFILL GUARD] " if metrics.get("backfill_guard_tripped") else ""
    suppressed_count = metrics.get("suppressed_recall_count", 0)
    # On a guard trip new_count is the dispatchable-only count, so the subject would understate the
    # run; surface the held batch alongside it.
    suppressed_part = f" ({suppressed_count} suppressed)" if suppressed_count else ""
    msg_part = f", {len(messages)} message(s)" if messages else ""
    subject = (
        f"{guard_prefix}Recall Radar ops: {new_count} new recall(s){suppressed_part}, "
        f"{will_receive} digest(s) queued{msg_part} \u2014 {today_date}"
    )

    html = _operator_digest_html(metrics=metrics, recalls=recalls, errors=errors, messages=messages)

    resend.Emails.send(
        {
            "from": settings.resend_from_address,
            "to": [operator_email],
            "subject": subject,
            "html": html,
        }
    )


def _operator_digest_html(
    metrics: dict, recalls: list, errors: list[str], messages: list | None = None
) -> str:
    messages = messages or []
    run_timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    oldest_digest = metrics.get("oldest_last_digest_at")
    oldest_str = oldest_digest.isoformat() if oldest_digest else "never"

    # Only shown when the per-country backfill guard held a bulk batch this run, so a normal run's
    # metrics table is unchanged.
    suppressed_count = metrics.get("suppressed_recall_count", 0)
    suppressed_countries = metrics.get("suppressed_countries", [])
    suppressed_row = (
        f"""
        <tr style="background:#fdecea;">
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#c0392b;">
            Suppressed (backfill guard)
          </td>
          <td style="padding:6px 0;font-size:14px;color:#c0392b;font-weight:bold;">
            {suppressed_count} &middot; {_html_escape(", ".join(suppressed_countries))}
          </td>
        </tr>"""
        if suppressed_count
        else ""
    )

    # Metrics table rows
    metrics_rows = f"""
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">New recalls this run</td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("new_recall_count", 0)}
          </td>
        </tr>{suppressed_row}
        <tr style="background:#f9f9f9;">
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">Active subscriptions</td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("total_active", 0)}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">Will receive digest</td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("will_receive_count", 0)}
          </td>
        </tr>
        <tr style="background:#f9f9f9;">
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">Skipped (cap)</td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("skipped_count", 0)}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">
            Pending &gt; 72h (stale)
          </td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("stale_pending_count", 0)}
          </td>
        </tr>
        <tr style="background:#f9f9f9;">
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">
            Oldest last_digest_at
          </td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {oldest_str}
          </td>
        </tr>
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">
            Contact messages this run
          </td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("new_message_count", 0)}
          </td>
        </tr>"""

    # Errors / warnings section
    if errors:
        error_items = "".join(
            f'<li style="margin:4px 0;font-size:13px;color:#c0392b;">{_html_escape(e)}</li>'
            for e in errors
        )
        errors_section = f"""
          <h2 style="font-size:16px;color:#c0392b;margin:24px 0 8px 0;">
            Errors / Warnings this run
          </h2>
          <ul style="margin:0;padding-left:18px;">{error_items}</ul>"""
    else:
        errors_section = """
          <h2 style="font-size:16px;color:#27ae60;margin:24px 0 8px 0;">
            Errors / Warnings this run
          </h2>
          <p style="font-size:14px;color:#555555;margin:0;">None</p>"""

    # Full recall list
    recall_items = "".join(_operator_recall_row(r) for r in recalls) or (
        '<p style="font-size:14px;color:#888888;margin:0;">No new recalls this run.</p>'
    )

    # Contact-form messages (spam already filtered out upstream)
    message_items = "".join(_operator_message_row(m) for m in messages) or (
        '<p style="font-size:14px;color:#888888;margin:0;">No new messages this run.</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f5f5f5;padding:32px 0;">
    <tr>
      <td align="center">
        <table width="700" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:6px;overflow:hidden;
                      max-width:700px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:#1a1a2e;padding:24px 32px;">
              <span style="color:#ffffff;font-size:20px;font-weight:bold;">
                Recall Radar &mdash; Operator Summary
              </span>
              <br>
              <span style="color:#aaaacc;font-size:13px;">Run at: {run_timestamp}</span>
            </td>
          </tr>

          <!-- Metrics -->
          <tr>
            <td style="padding:24px 32px 0 32px;">
              <h2 style="font-size:18px;color:#1a1a2e;margin:0 0 12px 0;">Metrics</h2>
              <table cellpadding="0" cellspacing="0" width="100%">
                {metrics_rows}
              </table>
            </td>
          </tr>

          <!-- Errors / Warnings -->
          <tr>
            <td style="padding:0 32px;">
              {errors_section}
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <hr style="border:none;border-top:1px solid #e8e8e8;margin:0;">
            </td>
          </tr>

          <!-- Contact messages -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <h2 style="font-size:18px;color:#1a1a2e;margin:0 0 12px 0;">Contact messages</h2>
              {message_items}
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <hr style="border:none;border-top:1px solid #e8e8e8;margin:0;">
            </td>
          </tr>

          <!-- All new recalls -->
          <tr>
            <td style="padding:20px 32px 32px 32px;">
              <h2 style="font-size:18px;color:#1a1a2e;margin:0 0 12px 0;">All new recalls</h2>
              {recall_items}
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _operator_recall_row(recall) -> str:
    # Recall fields come from external feeds — escape each before interpolation.
    company_part = f" &middot; {_html_escape(recall.company_name)}" if recall.company_name else ""
    source_url = _html_escape(recall.source_url or _recall_detail_url(recall))
    product_description = _html_escape(recall.product_description)
    country = _html_escape(recall.country)
    category = _html_escape(recall.category)
    severity_label = _html_escape(recall.severity_label)
    return (
        f'<div style="padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#333333;">'
        f"<strong>{product_description}</strong>{company_part}"
        f" &middot; {country}"
        f" &middot; {category}"
        f" &middot; {severity_label}"
        f"<br>"
        f'<a href="{source_url}" style="color:#1a1a2e;text-decoration:underline;">'
        f"{source_url}"
        f"</a>"
        f"</div>"
    )


def _operator_message_row(message) -> str:
    # Every field is visitor-supplied — escape all of them before interpolation.
    name = _html_escape(message.name) if message.name else "Anonymous"
    sender_email = _html_escape(message.email) if message.email else "no email"
    when = message.created_at.strftime("%Y-%m-%d %H:%M UTC") if message.created_at else ""
    body = _html_escape(message.message).replace("\n", "<br>")
    return (
        f'<div style="padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px;color:#333333;">'
        f"<strong>{name}</strong> &middot; {sender_email} &middot; {when}"
        f"<br>"
        f'<span style="color:#555555;">{body}</span>'
        f"</div>"
    )


def _html_escape(text: str) -> str:
    """Escape untrusted strings for insertion into HTML email bodies (incl. quoted attributes)."""
    return html.escape(text, quote=True)
