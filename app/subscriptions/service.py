from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.subscriptions.models import Subscription
from app.subscriptions.schemas import SubscriptionCreate, SubscriptionOut, SubscriptionPatch

logger = logging.getLogger(__name__)


def generate_confirmation_token() -> tuple[str, str]:
    """
    Returns (raw_token, sha256_hex_hash).
    The raw token is NEVER stored; only the hash is persisted.
    """
    raw = secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def generate_management_token() -> str:
    """
    Returns a 43-char base64url token derived from 32 cryptographically random bytes.

    32 bytes → base64url encodes to ceil(32 / 3) * 4 = 44 chars, with one trailing '='.
    Stripping that one '=' yields exactly 43 chars.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _normalise_criteria(data: SubscriptionCreate) -> dict:
    """Returns a dict of normalised filter fields for comparison.

    Normalisation rules:
    - Lowercase all strings.
    - Sort all arrays.
    - Treat None, [], and "" as equivalent (absent → None after normalisation).
    """
    return {
        "entities": sorted(e.lower() for e in (data.entities or [])),
        "companies": sorted(c.lower() for c in (data.companies or []) if c and c.strip()),
        "countries": sorted(c.lower() for c in (data.countries or [])),
        "categories": sorted(c.lower() for c in (data.categories or [])),
        "min_severity": (data.min_severity or "").lower() or None,
    }


def _apply_criteria(row: Subscription, norm: dict) -> None:
    """Copy normalised filter criteria onto a subscription row and stamp updated_at."""
    row.entities = norm["entities"]
    row.companies = norm["companies"]
    row.countries = norm["countries"]
    row.categories = norm["categories"]
    row.min_severity = norm["min_severity"]
    row.updated_at = datetime.now(UTC)


# Same response whatever the email's prior state — never reveals whether an address is registered.
_CREATE_RESPONSE = (
    200,
    {
        "message": (
            "Thanks! If this email isn't already subscribed, check your inbox for a confirmation "
            "link. If it is, your alert preferences have been updated."
        )
    },
)


def create(data: SubscriptionCreate, db: Session) -> tuple[int, dict]:
    """
    Create or update the single subscription for an email (one subscription per address).

    Returns (http_status_code, response_body_dict) — always the same uniform response, so the
    endpoint never reveals whether an address is already registered.

    Behaviour by the email's current state:
    - No subscription → create one in pending_confirmation, send the opt-in email.
    - pending_confirmation → update its criteria, issue a fresh token, resend the opt-in.
    - unsubscribed → restage as pending_confirmation with the new criteria + token, send the opt-in.
    - active → already confirmed; stage the change and email a confirm-update link. The live
      criteria don't change until that link is followed (an unauthenticated request can't alter a
      live subscription on its own).
    """
    norm = _normalise_criteria(data)

    stmt = select(Subscription).where(func.lower(Subscription.email) == func.lower(data.email))
    rows: list[Subscription] = list(db.scalars(stmt).all())
    # One subscription per email: act on a single primary row, preferring an active one.
    primary = (
        next((r for r in rows if r.status == "active"), None)
        or next((r for r in rows if r.status == "pending_confirmation"), None)
        or next((r for r in rows if r.status == "unsubscribed"), None)
    )

    if primary is not None and primary.status == "active":
        # Confirmed already — but this request is unauthenticated, so don't touch the live criteria.
        # Stage the change and email the verified address a single confirm-update link; the criteria
        # only change once that link is followed. The current alerts keep running until then.
        raw_token, token_hash = generate_confirmation_token()
        primary.confirmation_token_hash = token_hash
        primary.pending_update = {"criteria": norm, "requested_at": datetime.now(UTC).isoformat()}
        primary.updated_at = datetime.now(UTC)
        db.commit()
        _try_send_update_confirm(primary, raw_token)
        return _CREATE_RESPONSE

    raw_token, token_hash = generate_confirmation_token()

    if primary is not None:
        # Pending or previously unsubscribed → restage as pending with the new criteria and a fresh
        # token, then (re)send the opt-in so confirmation is required before any alert is sent.
        _apply_criteria(primary, norm)
        primary.status = "pending_confirmation"
        primary.confirmation_token_hash = token_hash
        primary.confirmed_at = None
        db.commit()
        _try_send_optin(email=data.email, raw_token=raw_token, subscription=primary)
        return _CREATE_RESPONSE

    subscription = Subscription(
        email=data.email,
        status="pending_confirmation",
        entities=norm["entities"],
        companies=norm["companies"],
        countries=norm["countries"],
        categories=norm["categories"],
        min_severity=norm["min_severity"],
        confirmation_token_hash=token_hash,
        management_token=generate_management_token(),
    )
    db.add(subscription)
    db.flush()  # assigns subscription.id before the email call
    db.commit()

    _try_send_optin(
        email=data.email,
        raw_token=raw_token,
        subscription=subscription,
    )

    return _CREATE_RESPONSE


def _try_send_optin(
    email: str,
    raw_token: str,
    subscription: Subscription,
) -> None:
    """
    Attempt to send the opt-in confirmation email.

    Failures are logged at ERROR level and swallowed so the caller can degrade gracefully.
    """
    try:
        import importlib  # noqa: PLC0415

        email_module = importlib.import_module("app.subscriptions.email")
        email_module.send_optin_email(  # type: ignore[attr-defined]
            email=email,
            raw_token=raw_token,
        )
    except Exception as exc:
        logger.error(
            "Failed to send opt-in email for subscription %s: %s",
            subscription.id,
            exc,
        )


def _try_send_update_confirm(subscription: Subscription, raw_token: str) -> None:
    """
    Email a confirmed subscriber a link to confirm a staged preference change (best-effort).

    Only the verified address receives it, so an unauthenticated request can't change live criteria
    without the owner clicking through. Failures are logged and swallowed.
    """
    try:
        import importlib  # noqa: PLC0415

        email_module = importlib.import_module("app.subscriptions.email")
        email_module.send_update_confirm_email(  # type: ignore[attr-defined]
            email=subscription.email,
            raw_token=raw_token,
            management_token=subscription.management_token,
        )
    except Exception as exc:
        logger.error(
            "Failed to send update-confirmation email for subscription %s: %s",
            subscription.id,
            exc,
        )


def confirm(raw_token: str, db: Session) -> tuple[int, dict]:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    stmt = select(Subscription).where(Subscription.confirmation_token_hash == token_hash)
    row = db.scalars(stmt).first()
    if row is None:
        return (404, {"detail": "Token not found or already used."})

    # The same token mechanism confirms two things: an initial opt-in, or a staged preference
    # change on an already-active subscription. pending_update tells them apart.
    if row.pending_update is not None:
        requested_at = datetime.fromisoformat(row.pending_update["requested_at"])
        if (datetime.now(UTC) - requested_at).total_seconds() > 72 * 3600:
            row.pending_update = None
            row.confirmation_token_hash = None
            db.commit()
            return (
                410,
                {"detail": "This update link has expired. Please submit your changes again."},
            )
        _apply_criteria(row, row.pending_update["criteria"])
        row.pending_update = None
        row.confirmation_token_hash = None
        db.commit()
        return (
            200,
            {
                "message": "Your alert preferences have been updated.",
                "management_token": row.management_token,
                "updated": True,
            },
        )

    age_seconds = (datetime.now(UTC) - row.created_at).total_seconds()
    if age_seconds > 72 * 3600:
        return (410, {"detail": "This confirmation link has expired. Please subscribe again."})
    row.status = "active"
    row.confirmed_at = datetime.now(UTC)
    row.confirmation_token_hash = None
    row.updated_at = datetime.now(UTC)
    db.commit()
    return (
        200,
        {
            "message": (
                "Subscription confirmed. You will receive alerts when matching recalls are found."
            ),
            # Hand back the management token so the just-confirmed visitor can jump straight to
            # managing their preferences. They own this subscription (they followed the email link).
            "management_token": row.management_token,
            "updated": False,
        },
    )


def get_manage(management_token: str, db: Session) -> tuple[int, dict]:
    stmt = select(Subscription).where(Subscription.management_token == management_token)
    row = db.scalars(stmt).first()
    if row is None:
        return (404, {"detail": "Token not found."})
    if row.status == "unsubscribed":
        return (410, {"detail": "This subscription has been unsubscribed."})
    return (200, SubscriptionOut.model_validate(row).model_dump())


def patch_manage(management_token: str, patch: SubscriptionPatch, db: Session) -> tuple[int, dict]:
    stmt = select(Subscription).where(Subscription.management_token == management_token)
    row = db.scalars(stmt).first()
    if row is None:
        return (404, {"detail": "Token not found."})
    if row.status == "unsubscribed":
        return (410, {"detail": "This subscription has been unsubscribed."})
    # Apply partial update — only fields explicitly set in the patch body
    patch_data = patch.model_dump(exclude_unset=True)
    for field, value in patch_data.items():
        setattr(row, field, value)
    # Validate post-patch state: at least one filter field must be non-empty
    has_entities = bool(row.entities)
    has_companies = any(c and c.strip() for c in (row.companies or []))
    has_categories = bool(row.categories)
    has_min_severity = row.min_severity is not None
    if not any([has_entities, has_companies, has_categories, has_min_severity]):
        db.rollback()
        return (422, {"detail": "At least one filter field must remain after update."})
    row.updated_at = datetime.now(UTC)
    db.commit()
    return (200, SubscriptionOut.model_validate(row).model_dump())


def unsubscribe(management_token: str, db: Session) -> tuple[int, dict]:
    stmt = select(Subscription).where(Subscription.management_token == management_token)
    row = db.scalars(stmt).first()
    if row is None:
        return (404, {"detail": "Token not found."})
    if row.status == "unsubscribed":
        return (410, {"detail": "Already unsubscribed."})
    row.status = "unsubscribed"
    row.updated_at = datetime.now(UTC)
    db.commit()
    return (200, {"message": "You have been unsubscribed."})
