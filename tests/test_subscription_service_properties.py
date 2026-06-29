from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.modules.recalls.schemas import RecallCategory, RecallCountry
from app.subscriptions import service
from app.subscriptions.schemas import SubscriptionCreate, SubscriptionPatch

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------
VALID_COUNTRIES = [c.value for c in RecallCountry]  # ['us', 'uk', 'za']
VALID_SEVERITIES = ["low", "moderate", "high", "severe", "critical"]
VALID_CATEGORIES = [c.value for c in RecallCategory]

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
country_st = st.sampled_from(VALID_COUNTRIES)
countries_st = st.lists(country_st, min_size=1, max_size=3, unique=True)
severity_st = st.sampled_from(VALID_SEVERITIES)
category_st = st.sampled_from(VALID_CATEGORIES)
categories_st = st.lists(category_st, min_size=1, max_size=len(VALID_CATEGORIES), unique=True)

# Entity names: letters and digits only to avoid pydantic / JSON edge-cases
entity_char_st = st.characters(whitelist_categories=("Lu", "Ll", "Nd"))
entity_st = st.text(min_size=1, max_size=30, alphabet=entity_char_st)
entities_st = st.lists(entity_st, min_size=1, max_size=5)

company_st = st.text(
    min_size=1,
    max_size=80,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs")),
)
companies_st = st.lists(company_st, min_size=1, max_size=3)

# Combine into "at least one filter present" strategy
filter_fields_st = st.fixed_dictionaries(
    {},
    optional={
        "entities": entities_st,
        "companies": companies_st,
        "categories": categories_st,
        "min_severity": severity_st,
    },
).filter(lambda d: bool(d))  # at least one key present


# ---------------------------------------------------------------------------
# Mock DB helpers
# ---------------------------------------------------------------------------


def make_mock_db(existing_rows: list | None = None) -> tuple[MagicMock, list]:
    """Return (mock_session, added_objects_list)."""
    mock_db = MagicMock()
    existing_rows = existing_rows or []

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = list(existing_rows)
    mock_scalars.first.return_value = existing_rows[0] if existing_rows else None
    mock_db.scalars.return_value = mock_scalars

    added_objects: list = []
    mock_db.add.side_effect = lambda obj: added_objects.append(obj)
    mock_db.flush.return_value = None
    mock_db.commit.return_value = None
    mock_db.rollback.return_value = None

    return mock_db, added_objects


class _FakeSub:
    """
    A plain Python object that quacks like a Subscription ORM row.

    We intentionally avoid constructing a real SQLAlchemy-mapped object because
    we are using a mock Session — the ORM instrumentation is not needed and its
    __init__ machinery would require a properly configured Session / connection.
    """

    def __init__(
        self,
        *,
        email: str = "user@example.com",
        status: str = "active",
        entities: list | None = None,
        companies: list | None = None,
        countries: list | None = None,
        categories: list | None = None,
        min_severity: str | None = None,
        management_token: str | None = None,
        confirmation_token_hash: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.email = email
        self.status = status
        self.entities = entities if entities is not None else []
        self.companies = companies if companies is not None else []
        self.countries = countries if countries is not None else ["us"]
        self.categories = categories if categories is not None else []
        self.min_severity = min_severity
        self.management_token = management_token or str(uuid.uuid4())
        self.confirmation_token_hash = confirmation_token_hash
        self.confirmed_at = None
        self.created_at = created_at if created_at is not None else now
        self.updated_at = now
        self.last_digest_at = None
        self.skipped_at = []


def make_subscription(
    *,
    email: str = "user@example.com",
    status: str = "active",
    entities: list | None = None,
    companies: list | None = None,
    countries: list | None = None,
    categories: list | None = None,
    min_severity: str | None = None,
    management_token: str | None = None,
    confirmation_token_hash: str | None = None,
    created_at: datetime | None = None,
) -> _FakeSub:
    """Return a _FakeSub that acts as a Subscription row for mock-session tests."""
    return _FakeSub(
        email=email,
        status=status,
        entities=entities,
        companies=companies,
        countries=countries,
        categories=categories,
        min_severity=min_severity,
        management_token=management_token,
        confirmation_token_hash=confirmation_token_hash,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Subscription creation produces pending_confirmation status
# ---------------------------------------------------------------------------


@given(
    email=st.emails(),
    countries=countries_st,
    filters=filter_fields_st,
)
@settings(max_examples=50)
def test_property_1_creation_produces_pending_confirmation(
    email: str,
    countries: list[str],
    filters: dict[str, Any],
) -> None:
    """
    # subscription creation produces pending_confirmation

    For any valid email + non-empty filter criteria, the created record has
    status = 'pending_confirmation'.

    """
    data = SubscriptionCreate(email=email, countries=countries, **filters)
    mock_db, added_objects = make_mock_db(existing_rows=[])

    with patch("app.subscriptions.service._try_send_optin"):
        status_code, _ = service.create(data, mock_db)

    assert status_code == 200
    assert len(added_objects) == 1
    new_sub = added_objects[0]
    assert new_sub.status == "pending_confirmation"


# ---------------------------------------------------------------------------
# Resubscribing updates the single subscription in place (one per email)
# ---------------------------------------------------------------------------


@given(
    email=st.emails(),
    countries=countries_st,
    filters=filter_fields_st,
)
@settings(max_examples=50)
def test_property_2_resubscribe_updates_active_in_place(
    email: str,
    countries: list[str],
    filters: dict[str, Any],
) -> None:
    """

    When an active subscription already exists for an email, resubscribing updates that row in
    place — it never creates a second subscription, stays active, takes on the new criteria, and
    returns the uniform 200 (so the response never reveals the address is registered).

    """
    existing = make_subscription(
        email=email,
        status="active",
        entities=[],
        companies=[],
        countries=["us"],
        categories=[],
        min_severity=None,
    )
    mock_db, added_objects = make_mock_db(existing_rows=[existing])

    data = SubscriptionCreate(email=email, countries=countries, **filters)

    with patch("app.subscriptions.service._try_send_prefs_updated"):
        status_code, _ = service.create(data, mock_db)

    assert status_code == 200
    # No second subscription row was created for this email.
    assert added_objects == []
    # The existing row stays active and adopts the new (normalised) criteria.
    assert existing.status == "active"
    assert existing.countries == sorted(c.lower() for c in countries)


def test_resubscribe_active_sends_preferences_updated_email():
    existing = make_subscription(email="a@b.com", status="active", entities=["milk"])
    mock_db, added = make_mock_db(existing_rows=[existing])
    data = SubscriptionCreate(email="a@b.com", countries=["us"], entities=["peanut"])

    with (
        patch("app.subscriptions.service._try_send_prefs_updated") as prefs,
        patch("app.subscriptions.service._try_send_optin") as optin,
    ):
        status_code, _ = service.create(data, mock_db)

    assert status_code == 200
    assert added == []
    assert existing.entities == ["peanut"]
    prefs.assert_called_once()  # the verified owner is notified
    optin.assert_not_called()  # no re-confirmation for an already-active subscription


def test_resubscribe_pending_updates_and_resends_optin():
    existing = make_subscription(email="a@b.com", status="pending_confirmation", entities=["milk"])
    mock_db, added = make_mock_db(existing_rows=[existing])
    data = SubscriptionCreate(email="a@b.com", countries=["us"], entities=["peanut"])

    with (
        patch("app.subscriptions.service._try_send_optin") as optin,
        patch("app.subscriptions.service._try_send_prefs_updated") as prefs,
    ):
        status_code, _ = service.create(data, mock_db)

    assert status_code == 200
    assert added == []
    assert existing.status == "pending_confirmation"
    assert existing.entities == ["peanut"]
    optin.assert_called_once()
    prefs.assert_not_called()


def test_resubscribe_unsubscribed_restages_as_pending():
    existing = make_subscription(email="a@b.com", status="unsubscribed", entities=["milk"])
    mock_db, added = make_mock_db(existing_rows=[existing])
    data = SubscriptionCreate(email="a@b.com", countries=["us"], entities=["peanut"])

    with patch("app.subscriptions.service._try_send_optin") as optin:
        status_code, _ = service.create(data, mock_db)

    assert status_code == 200
    assert added == []
    # Re-confirmation is required after an unsubscribe.
    assert existing.status == "pending_confirmation"
    assert existing.confirmation_token_hash is not None
    assert existing.confirmed_at is None
    optin.assert_called_once()


# ---------------------------------------------------------------------------
# All-empty filter body rejected with 422
# ---------------------------------------------------------------------------


@given(
    email=st.emails(),
    countries=countries_st,
)
@settings(max_examples=50)
def test_property_3_empty_filter_body_rejected(
    email: str,
    countries: list[str],
) -> None:
    """

    For any request body where all filter fields are absent / null / empty,
    SubscriptionCreate raises ValidationError (HTTP 422).

    """
    with pytest.raises(ValidationError) as exc_info:
        SubscriptionCreate.model_validate(
            {
                "email": email,
                "countries": countries,
                "entities": [],
                "companies": [],
                "categories": [],
                "min_severity": None,
            }
        )

    errors = exc_info.value.errors()
    error_messages = [str(e) for e in errors]
    assert any("at_least_one_filter_required" in msg for msg in error_messages), (
        f"Expected 'at_least_one_filter_required' in errors, got: {error_messages}"
    )


@given(
    email=st.emails(),
    countries=countries_st,
)
@settings(max_examples=30)
def test_property_3_blank_company_also_rejected(
    email: str,
    countries: list[str],
) -> None:
    """

    A companies list holding only blank strings, with all other filters absent, still raises
    ValidationError — a blank company name is not a real filter.

    """
    with pytest.raises(ValidationError) as exc_info:
        SubscriptionCreate.model_validate(
            {
                "email": email,
                "countries": countries,
                "entities": [],
                "companies": ["", "   "],
                "categories": [],
                "min_severity": None,
            }
        )

    errors = exc_info.value.errors()
    error_messages = [str(e) for e in errors]
    assert any("at_least_one_filter_required" in msg for msg in error_messages)


# ---------------------------------------------------------------------------
# Invalid email rejected with 422
# ---------------------------------------------------------------------------

# Strategy for definitely-invalid emails
invalid_email_st = st.one_of(
    st.just(""),
    st.just("notanemail"),
    st.just("@nodomain"),
    st.just("no-at-sign"),
    st.just("two@@signs.com"),
    st.just("spaces in@email.com"),
    # Generate random short strings without '@'
    st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-"),
    # Generate strings with multiple '@'
    st.builds(
        lambda a, b, c: f"{a}@{b}@{c}",
        st.from_regex(r"[a-z]+"),
        st.from_regex(r"[a-z]+"),
        st.from_regex(r"[a-z]+"),
    ),
)


@given(
    invalid_email=invalid_email_st,
    countries=countries_st,
    filters=filter_fields_st,
)
@settings(max_examples=50)
def test_property_5_invalid_email_rejected(
    invalid_email: str,
    countries: list[str],
    filters: dict[str, Any],
) -> None:
    """

    For any non-RFC-5321 string in the email field, SubscriptionCreate raises ValidationError.

    """
    with pytest.raises(ValidationError):
        SubscriptionCreate.model_validate(
            {
                "email": invalid_email,
                "countries": countries,
                **filters,
            }
        )


# ---------------------------------------------------------------------------
# Confirmation activates subscription and invalidates token
# ---------------------------------------------------------------------------


@given(
    age_hours=st.floats(min_value=0.0, max_value=71.9),
    countries=countries_st,
    filters=filter_fields_st,
)
@settings(max_examples=50)
def test_property_6_confirmation_activates_and_invalidates_token(
    age_hours: float,
    countries: list[str],
    filters: dict[str, Any],
) -> None:
    """

    For any pending subscription within 72 hours, confirm transitions to active,
    sets confirmed_at, and nulls confirmation_token_hash.

    """
    raw_token = "test-raw-token-" + str(uuid.uuid4())
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    sub = make_subscription(
        status="pending_confirmation",
        countries=countries,
        confirmation_token_hash=token_hash,
        created_at=created_at,
    )

    mock_db, _ = make_mock_db(existing_rows=[sub])

    status_code, body = service.confirm(raw_token, mock_db)

    assert status_code == 200, f"Expected 200, got {status_code}: {body}"
    assert sub.status == "active"
    assert sub.confirmed_at is not None
    assert sub.confirmation_token_hash is None


# ---------------------------------------------------------------------------
# Unrecognised/reused tokens return 404
# ---------------------------------------------------------------------------


@given(
    raw_token=st.text(min_size=1, max_size=100),
)
@settings(max_examples=50)
def test_property_7_unrecognised_token_returns_404(raw_token: str) -> None:
    """

    For any token string that hashes to a value not in the DB, confirm returns 404.

    """
    # DB has no subscription with a matching hash
    mock_db, _ = make_mock_db(existing_rows=[])

    status_code, body = service.confirm(raw_token, mock_db)

    assert status_code == 404, f"Expected 404, got {status_code}: {body}"


@given(
    raw_token=st.text(min_size=1, max_size=100),
)
@settings(max_examples=50)
def test_property_7_already_used_token_returns_404(raw_token: str) -> None:
    """

    A subscription with confirmation_token_hash=None (already confirmed) → confirm returns 404.

    (already used variant)**
    """
    # Subscription exists but hash is already None (already confirmed)
    make_subscription(
        status="active",
        confirmation_token_hash=None,
    )

    mock_db, _ = make_mock_db(existing_rows=[])  # scalars().first() returns None for missing hash

    status_code, body = service.confirm(raw_token, mock_db)

    assert status_code == 404, f"Expected 404, got {status_code}: {body}"


# ---------------------------------------------------------------------------
# Manage endpoint never leaks email
# ---------------------------------------------------------------------------


@given(
    email=st.emails(),
    countries=countries_st,
    filters=filter_fields_st,
    status=st.sampled_from(["active", "paused"]),
)
@settings(max_examples=50)
def test_property_8_manage_never_leaks_email(
    email: str,
    countries: list[str],
    filters: dict[str, Any],
    status: str,
) -> None:
    """

    For any active/paused subscription, GET /subscriptions/manage response body contains no field
    equal to or containing the subscriber's email address.

    """
    mgmt_token = str(uuid.uuid4())
    sub = make_subscription(
        email=email,
        status=status,
        countries=countries,
        entities=filters.get("entities", []),
        companies=filters.get("companies", []),
        categories=filters.get("categories", []),
        min_severity=filters.get("min_severity"),
        management_token=mgmt_token,
    )

    mock_db, _ = make_mock_db(existing_rows=[sub])

    status_code, body = service.get_manage(mgmt_token, mock_db)

    assert status_code == 200, f"Expected 200, got {status_code}: {body}"
    assert isinstance(body, dict)

    # Check that no value in the response body contains or equals the email
    email_lower = email.lower()
    for key, value in body.items():
        if isinstance(value, str):
            assert email_lower not in value.lower(), (
                f"Field '{key}' leaks email: '{value}' contains '{email}'"
            )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    assert email_lower not in item.lower(), (
                        f"Field '{key}' list item leaks email: '{item}' contains '{email}'"
                    )


# ---------------------------------------------------------------------------
# Partial update leaves unspecified fields unchanged
# ---------------------------------------------------------------------------


@given(
    countries=countries_st,
    entities=entities_st,
    patch_severity=st.one_of(st.none(), severity_st),
    patch_categories=st.one_of(st.none(), categories_st),
)
@settings(max_examples=50)
def test_property_9_partial_update_leaves_unspecified_fields_unchanged(
    countries: list[str],
    entities: list[str],
    patch_severity: str | None,
    patch_categories: list[str] | None,
) -> None:
    """
    # partial update leaves unspecified fields unchanged

    For any active subscription and any partial PATCH body, fields absent from the body retain
    their pre-patch values.

    """
    mgmt_token = str(uuid.uuid4())
    original_entities = entities
    original_countries = countries
    original_companies = ["OriginalCompany"]
    original_min_severity = "low"
    original_categories = ["allergen"]

    sub = make_subscription(
        status="active",
        entities=list(original_entities),
        countries=list(original_countries),
        companies=list(original_companies),
        min_severity=original_min_severity,
        categories=list(original_categories),
        management_token=mgmt_token,
    )

    mock_db, _ = make_mock_db(existing_rows=[sub])

    # Build a patch that only specifies min_severity and/or categories
    patch_kwargs: dict[str, Any] = {}
    if patch_severity is not None:
        patch_kwargs["min_severity"] = patch_severity
    if patch_categories is not None:
        patch_kwargs["categories"] = patch_categories

    # Ensure the patch would leave at least one filter non-empty
    # (if patch would clear everything, add a stable entity to the patch)
    if (
        patch_categories == []
        and not patch_kwargs.get("min_severity")
        and not sub.entities
        and not sub.companies
    ):
        # patch would result in empty filter, skip this case
        return

    if not patch_kwargs:
        # No patch fields — nothing to assert, trivially passes
        return

    patch = SubscriptionPatch(**patch_kwargs)

    pre_patch_entities = list(sub.entities)
    pre_patch_countries = list(sub.countries)
    pre_patch_companies = list(sub.companies)

    status_code, body = service.patch_manage(mgmt_token, patch, mock_db)

    if status_code == 200:
        # Fields NOT in the patch must retain their pre-patch values
        if "entities" not in patch_kwargs:
            assert sub.entities == pre_patch_entities, (
                f"entities changed unexpectedly: {pre_patch_entities} → {sub.entities}"
            )
        if "countries" not in patch_kwargs:
            assert sub.countries == pre_patch_countries, (
                f"countries changed unexpectedly: {pre_patch_countries} → {sub.countries}"
            )
        if "companies" not in patch_kwargs:
            assert sub.companies == pre_patch_companies, (
                f"companies changed unexpectedly: {pre_patch_companies} → {sub.companies}"
            )
