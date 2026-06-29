"""
app/subscriptions/email.py
Resend SDK wrapper and HTML email templates for Recall Radar subscriptions.

Import-time behaviour
---------------------
- If the `resend` package is not installed → ImportError is raised immediately.
- If RESEND_API_KEY is absent → WARNING is logged, EMAIL_DISABLED is set to True.
- If RESEND_API_KEY is present → resend.api_key is configured.
- RESEND_FROM_ADDRESS defaults to recalls@notify.brentbutkow.me.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

# Raise ImportError at import time if resend is unavailable.
try:
    import resend
except ModuleNotFoundError as _e:
    raise ImportError(
        "The 'resend' package is required for email delivery. Install it with: pip install resend"
    ) from _e

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level configuration (evaluated once at import time)
# ---------------------------------------------------------------------------

_api_key = os.getenv("RESEND_API_KEY")
if not _api_key:
    logger.warning(
        "RESEND_API_KEY is not set — email sending is disabled. "
        "Subscription creation will still succeed; confirmation emails will be silently skipped."
    )
    EMAIL_DISABLED = True
else:
    resend.api_key = _api_key
    EMAIL_DISABLED = False

FROM_ADDRESS: str = os.getenv("RESEND_FROM_ADDRESS", "recalls@notify.brentbutkow.me")

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

RETRY_DELAYS = [5, 10, 20]  # seconds — 3 total attempts


async def send_with_retry(send_fn, *args, **kwargs):
    """
    Call send_fn(*args, **kwargs) with exponential backoff on transient failures.

    - On 4xx HTTPStatusError → re-raise immediately (permanent failure, no retry).
    - On 5xx / network error → retry with delays [5, 10, 20] seconds.
    - After all retries exhausted → re-raise last exception.
    """
    import httpx  # noqa: PLC0415 — already in project deps

    last_exc: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            return await send_fn(*args, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                # Permanent failure — do not retry.
                raise
            # Transient 5xx — fall through to retry logic.
            last_exc = exc
        except httpx.TransportError as exc:
            # Network-level failure — also transient.
            last_exc = exc

        if attempt < len(RETRY_DELAYS) - 1:
            await asyncio.sleep(delay)

    assert last_exc is not None  # always set when we reach here
    raise last_exc


# ---------------------------------------------------------------------------
# Opt-in email
# ---------------------------------------------------------------------------


def send_optin_email(email: str, raw_token: str) -> None:
    """
    Send the double opt-in confirmation email.

    Silently skipped when EMAIL_DISABLED is True. No unsubscribe link is shown — a pre-confirmation
    subscription has nothing to manage, and ignoring this email is itself the opt-out.

    Parameters
    ----------
    email:      Recipient's email address.
    raw_token:  Raw (unhashed) confirmation token.
    """
    if EMAIL_DISABLED:
        return

    confirm_url = f"https://brentbutkow.me/projects/recall-radar/confirm?token={raw_token}"

    html = _optin_html(confirm_url=confirm_url)

    resend.Emails.send(
        {
            "from": FROM_ADDRESS,
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

    Silently skipped when EMAIL_DISABLED is True. The change only takes effect once this link is
    followed, so an unauthenticated request can never alter a live subscription on its own.
    """
    if EMAIL_DISABLED:
        return

    confirm_url = f"https://brentbutkow.me/projects/recall-radar/confirm?token={raw_token}"
    manage_url = f"https://brentbutkow.me/projects/recall-radar/manage?token={management_token}"
    unsub_url = f"https://brentbutkow.me/projects/recall-radar/unsubscribe?token={management_token}"

    resend.Emails.send(
        {
            "from": FROM_ADDRESS,
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

    Silently skipped when EMAIL_DISABLED is True.

    Parameters
    ----------
    subscription:      Subscription ORM instance.
    matching_recalls:  List of Recall ORM instances that matched the subscription.
    """
    if EMAIL_DISABLED:
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
            "from": FROM_ADDRESS,
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


def _recall_card(recall) -> str:
    """Return an HTML card for a single recall (inline styles only).

    Every recall field comes from external ingest feeds, so each is HTML-escaped before
    interpolation to keep feed content from breaking or injecting markup.
    """
    detail_url = _html_escape(
        f"https://brentbutkow.me/projects/recall-radar/{recall.source}/{recall.recall_number}"
    )
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


def send_operator_digest_email(metrics: dict, recalls: list, errors: list[str]) -> None:
    """
    Send the operator summary email.

    Silently skipped when EMAIL_DISABLED is True.
    Also skipped (with WARNING) when OPERATOR_EMAIL is absent.

    Parameters
    ----------
    metrics:  Dict with keys: new_recall_count, total_active, will_receive_count,
              skipped_count, stale_pending_count, oldest_last_digest_at.
    recalls:  All new Recall ORM instances ingested in this run.
    errors:   List of ERROR/WARNING message strings collected during the run.
    """
    if EMAIL_DISABLED:
        return

    operator_email = os.getenv("OPERATOR_EMAIL", "").strip()
    if not operator_email:
        logger.warning("OPERATOR_EMAIL is not set — operator digest will not be sent.")
        return

    today_date = datetime.now(UTC).date().isoformat()
    new_count = metrics["new_recall_count"]
    will_receive = metrics["will_receive_count"]
    subject = (
        f"Recall Radar ops: {new_count} new recall(s), "
        f"{will_receive} digest(s) queued \u2014 {today_date}"
    )

    html = _operator_digest_html(metrics=metrics, recalls=recalls, errors=errors)

    resend.Emails.send(
        {
            "from": FROM_ADDRESS,
            "to": [operator_email],
            "subject": subject,
            "html": html,
        }
    )


def _operator_digest_html(metrics: dict, recalls: list, errors: list[str]) -> str:
    run_timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    oldest_digest = metrics.get("oldest_last_digest_at")
    oldest_str = oldest_digest.isoformat() if oldest_digest else "never"

    # Metrics table rows
    metrics_rows = f"""
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:14px;color:#444444;">New recalls this run</td>
          <td style="padding:6px 0;font-size:14px;color:#1a1a2e;font-weight:bold;">
            {metrics.get("new_recall_count", 0)}
          </td>
        </tr>
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
    source_url = _html_escape(
        recall.source_url
        or f"https://brentbutkow.me/projects/recall-radar/{recall.source}/{recall.recall_number}"
    )
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


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for untrusted strings inserted into email bodies."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
