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
        "company": (data.company or "").lower() or None,
        "countries": sorted(c.lower() for c in (data.countries or [])),
        "categories": sorted(c.lower() for c in (data.categories or [])),
        "min_severity": (data.min_severity or "").lower() or None,
    }


def _normalise_criteria_from_row(row: Subscription) -> dict:
    """Builds a normalised criteria dict from an existing Subscription ORM row."""
    return {
        "entities": sorted(e.lower() for e in (row.entities or [])),
        "company": (row.company or "").lower() or None,
        "countries": sorted(c.lower() for c in (row.countries or [])),
        "categories": sorted(c.lower() for c in (row.categories or [])),
        "min_severity": (row.min_severity or "").lower() or None,
    }


def create(data: SubscriptionCreate, db: Session) -> tuple[int, dict]:
    """
    Create a new subscription or handle an existing one for the given email.

    Returns (http_status_code, response_body_dict).

    Decision tree:
    - pending_confirmation row exists → resend opt-in email, return 200
    - active row with identical normalised criteria → return 409
    - active row with different criteria → create new row, dispatch opt-in, return 201
    - no active or pending rows → create new row, dispatch opt-in, return 201
    """
    # Look up all existing subscriptions for this email (case-insensitive).
    stmt = select(Subscription).where(func.lower(Subscription.email) == func.lower(data.email))
    existing_rows: list[Subscription] = list(db.scalars(stmt).all())

    pending_rows = [r for r in existing_rows if r.status == "pending_confirmation"]
    active_rows = [r for r in existing_rows if r.status == "active"]

    # Branch 1: pending_confirmation row already exists — resend the opt-in email.
    if pending_rows:
        pending = pending_rows[0]
        new_raw, new_hash = generate_confirmation_token()
        pending.confirmation_token_hash = new_hash
        pending.updated_at = datetime.now(UTC)
        db.commit()
        _try_send_optin(
            email=data.email,
            raw_token=new_raw,
            subscription=pending,
        )
        return (200, {"message": "Confirmation email resent."})

    # Branch 2: check active rows for identical criteria.
    normalised_incoming = _normalise_criteria(data)
    for active_row in active_rows:
        if _normalise_criteria_from_row(active_row) == normalised_incoming:
            # Active subscription with identical criteria already exists.
            return (
                409,
                {"detail": "An active subscription with identical criteria already exists."},
            )

    # Branch 3 / 4: no pending and no identical-active row — create a new subscription.
    raw_token, token_hash = generate_confirmation_token()
    mgmt_token = generate_management_token()

    subscription = Subscription(
        email=data.email,
        status="pending_confirmation",
        entities=normalised_incoming["entities"],
        company=normalised_incoming["company"],
        countries=normalised_incoming["countries"],
        categories=normalised_incoming["categories"],
        min_severity=normalised_incoming["min_severity"],
        confirmation_token_hash=token_hash,
        management_token=mgmt_token,
    )
    db.add(subscription)
    db.flush()  # assigns subscription.id before the email call
    db.commit()

    _try_send_optin(
        email=data.email,
        raw_token=raw_token,
        subscription=subscription,
    )

    return (201, {"message": "Subscription created. Please check your email to confirm."})


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
            management_token=subscription.management_token,
        )
    except Exception as exc:
        logger.error(
            "Failed to send opt-in email for subscription %s: %s",
            subscription.id,
            exc,
        )


def confirm(raw_token: str, db: Session) -> tuple[int, dict]:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    stmt = select(Subscription).where(Subscription.confirmation_token_hash == token_hash)
    row = db.scalars(stmt).first()
    if row is None:
        return (404, {"detail": "Token not found or already used."})
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
            )
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
    has_company = bool(row.company)
    has_categories = bool(row.categories)
    has_min_severity = row.min_severity is not None
    if not any([has_entities, has_company, has_categories, has_min_severity]):
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
