from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.recalls.schemas import RecallCategory, RecallCountry
from app.subscriptions.matcher import recall_is_new, recall_matches
from app.subscriptions.models import SEVERITY_ORDER

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------
VALID_COUNTRIES = [c.value for c in RecallCountry]  # ['us', 'uk', 'za']
VALID_SEVERITIES = list(SEVERITY_ORDER)  # ["low","moderate","high","severe","critical"]
VALID_CATEGORIES = [c.value for c in RecallCategory]

# ---------------------------------------------------------------------------
# Fake domain objects (pure Python — no ORM, no DB)
# ---------------------------------------------------------------------------


@dataclass
class _FakeRecall:
    """Minimal stand-in for the Recall ORM model — only the fields matcher.py touches."""

    entities: list[dict]  # [{"type": str, "value": str}, ...]
    company_name: str | None
    country: str
    category: str
    severity_label: str
    report_date: date | None
    recall_initiation_date: date | None


@dataclass
class _FakeSub:
    """Minimal stand-in for the Subscription ORM model — only the fields matcher.py touches."""

    entities: list[str]
    companies: list[str]
    countries: list[str]
    categories: list[str]
    min_severity: str | None
    last_digest_at: datetime | None
    confirmed_at: datetime | None


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

country_st = st.sampled_from(VALID_COUNTRIES)
severity_st = st.sampled_from(VALID_SEVERITIES)
category_st = st.sampled_from(VALID_CATEGORIES)

# Use ASCII-only alphabet to avoid Unicode case-folding edge cases
# (e.g. 'ſ'.upper() == 'S' but 'S'.lower() == 's' ≠ 'ſ')
entity_value_st = st.text(
    min_size=1,
    max_size=30,
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
)

entity_dict_st = st.builds(
    lambda v: {"type": "allergen", "value": v},
    entity_value_st,
)


# A "matching" (recall, sub) pair: the recall satisfies every criterion on the sub.
# We build them together to guarantee consistency.
@st.composite
def matching_pair_st(draw):
    """Draw a (recall, sub) pair where recall_matches(recall, sub) must be True."""
    # Entity
    entity_value = draw(entity_value_st)
    recall_entities = [{"type": "allergen", "value": entity_value}]
    sub_entities = [entity_value]  # exact match (case-insensitive checked separately)

    # Company
    company_base = draw(
        st.text(
            min_size=1,
            max_size=40,
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        )
    )
    recall_company = company_base
    sub_company = company_base.lower()

    # Country
    country = draw(country_st)

    # Category
    category = draw(category_st)

    # Severity
    severity = draw(severity_st)

    # Dates
    report_date = draw(st.one_of(st.none(), st.dates()))
    initiation_date = draw(st.one_of(st.none(), st.dates()))

    recall = _FakeRecall(
        entities=recall_entities,
        company_name=recall_company,
        country=country,
        category=category,
        severity_label=severity,
        report_date=report_date,
        recall_initiation_date=initiation_date,
    )
    sub = _FakeSub(
        entities=sub_entities,
        companies=[sub_company],
        countries=[country],
        categories=[category],
        min_severity=severity,  # same level → index >= index → passes
        last_digest_at=None,
        confirmed_at=None,
    )
    return recall, sub


# ---------------------------------------------------------------------------
# recall matching predicate is a conjunction
# ---------------------------------------------------------------------------


@given(pair=matching_pair_st())
@settings(max_examples=200)
def test_property_10_recall_matching_is_conjunction(pair):
    """

    For any (recall, subscription) pair, recall_matches returns True iff every non-empty
    criterion is satisfied; a single failing criterion returns False.

    """
    recall, sub = pair

    # Case 1: perfectly matching pair → True
    assert recall_matches(recall, sub) is True, "Matching pair must return True"

    # Case 2a: entity mismatch → False
    mismatched_entity_sub = _FakeSub(
        entities=["__no_match_xyz__"],
        companies=sub.companies,
        countries=sub.countries,
        categories=sub.categories,
        min_severity=sub.min_severity,
        last_digest_at=sub.last_digest_at,
        confirmed_at=sub.confirmed_at,
    )
    assert recall_matches(recall, mismatched_entity_sub) is False, (
        "Entity mismatch must return False"
    )

    # Case 2b: company mismatch → False
    mismatched_company_sub = _FakeSub(
        entities=sub.entities,
        companies=["__zzznocompanymatch__"],
        countries=sub.countries,
        categories=sub.categories,
        min_severity=sub.min_severity,
        last_digest_at=sub.last_digest_at,
        confirmed_at=sub.confirmed_at,
    )
    assert recall_matches(recall, mismatched_company_sub) is False, (
        "Company mismatch must return False"
    )

    # Case 2c: country mismatch → False
    other_countries = [c for c in VALID_COUNTRIES if c != recall.country]
    if other_countries:
        mismatched_country_sub = _FakeSub(
            entities=sub.entities,
            companies=sub.companies,
            countries=other_countries,
            categories=sub.categories,
            min_severity=sub.min_severity,
            last_digest_at=sub.last_digest_at,
            confirmed_at=sub.confirmed_at,
        )
        assert recall_matches(recall, mismatched_country_sub) is False, (
            "Country mismatch must return False"
        )

    # Case 2d: category mismatch → False
    other_categories = [c for c in VALID_CATEGORIES if c != recall.category]
    if other_categories:
        mismatched_category_sub = _FakeSub(
            entities=sub.entities,
            companies=sub.companies,
            countries=sub.countries,
            categories=[other_categories[0]],
            min_severity=sub.min_severity,
            last_digest_at=sub.last_digest_at,
            confirmed_at=sub.confirmed_at,
        )
        assert recall_matches(recall, mismatched_category_sub) is False, (
            "Category mismatch must return False"
        )

    # Case 2e: severity threshold too high → False
    # Find a severity strictly above the recall's severity_label
    recall_idx = SEVERITY_ORDER.index(recall.severity_label)
    if recall_idx < len(SEVERITY_ORDER) - 1:
        higher_severity = SEVERITY_ORDER[recall_idx + 1]
        mismatched_severity_sub = _FakeSub(
            entities=sub.entities,
            companies=sub.companies,
            countries=sub.countries,
            categories=sub.categories,
            min_severity=higher_severity,
            last_digest_at=sub.last_digest_at,
            confirmed_at=sub.confirmed_at,
        )
        assert recall_matches(recall, mismatched_severity_sub) is False, (
            f"Severity below threshold "
            f"({recall.severity_label} < {higher_severity}) must return False"
        )

    # Case 3: empty subscription criteria (all filters cleared) → True for any recall
    empty_sub = _FakeSub(
        entities=[],
        companies=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
    )
    assert recall_matches(recall, empty_sub) is True, (
        "Subscription with no filters must match any recall"
    )


@given(
    entity_value=entity_value_st,
    country=country_st,
    category=category_st,
    severity=severity_st,
)
@settings(max_examples=100)
def test_property_10_case_insensitive_entity_match(entity_value, country, category, severity):
    """

    Entity matching is case-insensitive: recall entity value "Peanuts" matches subscription
    entity "peanuts" and vice versa.
    """
    recall = _FakeRecall(
        entities=[{"type": "allergen", "value": entity_value.upper()}],
        company_name=None,
        country=country,
        category=category,
        severity_label=severity,
        report_date=None,
        recall_initiation_date=None,
    )
    sub = _FakeSub(
        entities=[entity_value.lower()],
        companies=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
    )
    assert recall_matches(recall, sub) is True, (
        f"Entity match must be case-insensitive: recall has '{entity_value.upper()}', "
        f"sub has '{entity_value.lower()}'"
    )


# ---------------------------------------------------------------------------
# recall newness determined by effective date
# ---------------------------------------------------------------------------


@given(
    report_date=st.one_of(st.none(), st.dates()),
    initiation_date=st.one_of(st.none(), st.dates()),
)
@settings(max_examples=200)
def test_property_11_both_dates_null_returns_false(report_date, initiation_date):
    """

    When both date fields are None, recall_is_new must return False regardless of subscription.

    """
    recall = _FakeRecall(
        entities=[],
        company_name=None,
        country="us",
        category="allergen",
        severity_label="low",
        report_date=None,
        recall_initiation_date=None,
    )
    ref = datetime(2024, 1, 1, tzinfo=UTC)
    sub = _FakeSub(
        entities=[],
        companies=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=ref,
        confirmed_at=None,
    )
    assert recall_is_new(recall, sub) is False, "Both date fields null → must return False"


@given(
    base_date=st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 12, 31)),
    delta_days=st.integers(min_value=1, max_value=3650),
    use_last_digest=st.booleans(),
    use_both_dates=st.booleans(),
)
@settings(max_examples=200)
def test_property_11_effective_date_after_reference_returns_true(
    base_date, delta_days, use_last_digest, use_both_dates
):
    """

    When max(non-null dates) > reference.date(), recall_is_new must return True.

    """
    reference_date = base_date
    # Effective date is strictly after the reference
    effective_date = base_date + timedelta(days=delta_days)

    if use_both_dates:
        # Both non-null; effective = max, so set one to effective_date and one earlier
        report_date = effective_date
        initiation_date = base_date  # earlier, so max = effective_date
    else:
        report_date = effective_date
        initiation_date = None

    recall = _FakeRecall(
        entities=[],
        company_name=None,
        country="us",
        category="allergen",
        severity_label="low",
        report_date=report_date,
        recall_initiation_date=initiation_date,
    )

    reference_dt = datetime(
        reference_date.year, reference_date.month, reference_date.day, tzinfo=UTC
    )

    if use_last_digest:
        sub = _FakeSub(
            entities=[],
            companies=[],
            countries=[],
            categories=[],
            min_severity=None,
            last_digest_at=reference_dt,
            confirmed_at=None,
        )
    else:
        sub = _FakeSub(
            entities=[],
            companies=[],
            countries=[],
            categories=[],
            min_severity=None,
            last_digest_at=None,
            confirmed_at=reference_dt,
        )

    assert recall_is_new(recall, sub) is True, (
        f"effective_date={effective_date} > reference={reference_date} must return True"
    )


@given(
    base_date=st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 12, 31)),
    delta_days=st.integers(min_value=0, max_value=3650),
    use_last_digest=st.booleans(),
    use_both_dates=st.booleans(),
)
@settings(max_examples=200)
def test_property_11_effective_date_not_after_reference_returns_false(
    base_date, delta_days, use_last_digest, use_both_dates
):
    """

    When max(non-null dates) <= reference.date(), recall_is_new must return False.

    """
    reference_date = base_date + timedelta(days=delta_days)  # reference is after or equal
    # Effective date is at or before the reference
    effective_date = base_date  # effective <= reference

    if use_both_dates:
        report_date = effective_date
        initiation_date = effective_date  # max = effective_date
    else:
        report_date = effective_date
        initiation_date = None

    recall = _FakeRecall(
        entities=[],
        company_name=None,
        country="us",
        category="allergen",
        severity_label="low",
        report_date=report_date,
        recall_initiation_date=initiation_date,
    )

    reference_dt = datetime(
        reference_date.year, reference_date.month, reference_date.day, tzinfo=UTC
    )

    if use_last_digest:
        sub = _FakeSub(
            entities=[],
            companies=[],
            countries=[],
            categories=[],
            min_severity=None,
            last_digest_at=reference_dt,
            confirmed_at=None,
        )
    else:
        sub = _FakeSub(
            entities=[],
            companies=[],
            countries=[],
            categories=[],
            min_severity=None,
            last_digest_at=None,
            confirmed_at=reference_dt,
        )

    assert recall_is_new(recall, sub) is False, (
        f"effective_date={effective_date} <= reference={reference_date} must return False"
    )


@given(
    report_date=st.one_of(st.none(), st.dates()),
    initiation_date=st.one_of(st.none(), st.dates()),
)
@settings(max_examples=200)
def test_property_11_no_reference_returns_true(report_date, initiation_date):
    """

    When both last_digest_at and confirmed_at are None (newly activated subscription),
    recall_is_new must return True for any recall with at least one non-null date.

    """
    # Need at least one non-null date for a meaningful result
    if report_date is None and initiation_date is None:
        return  # both null → False by definition, tested separately

    recall = _FakeRecall(
        entities=[],
        company_name=None,
        country="us",
        category="allergen",
        severity_label="low",
        report_date=report_date,
        recall_initiation_date=initiation_date,
    )
    sub = _FakeSub(
        entities=[],
        companies=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
    )
    assert recall_is_new(recall, sub) is True, (
        "No reference (newly activated sub) with non-null date → must return True"
    )


@given(
    date1=st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 12, 31)),
    date2=st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 12, 31)),
    delta_days=st.integers(min_value=1, max_value=3650),
)
@settings(max_examples=200)
def test_property_11_last_digest_takes_precedence_over_confirmed_at(date1, date2, delta_days):
    """

    When last_digest_at is set, it is used as the reference rather than confirmed_at.
    A recall newer than last_digest_at but older than confirmed_at should still be "new".

    """
    # effective_date is between last_digest_at and confirmed_at:
    # last_digest_at < effective_date <= confirmed_at
    last_digest_date = date1
    effective_date = last_digest_date + timedelta(days=delta_days)

    recall = _FakeRecall(
        entities=[],
        company_name=None,
        country="us",
        category="allergen",
        severity_label="low",
        report_date=effective_date,
        recall_initiation_date=None,
    )

    last_digest_dt = datetime(
        last_digest_date.year, last_digest_date.month, last_digest_date.day, tzinfo=UTC
    )
    # confirmed_at is set to some earlier time — should NOT be used as reference
    confirmed_dt = last_digest_dt - timedelta(days=1)

    sub = _FakeSub(
        entities=[],
        companies=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=last_digest_dt,
        confirmed_at=confirmed_dt,
    )

    assert recall_is_new(recall, sub) is True, (
        f"effective_date={effective_date} > last_digest={last_digest_date}; "
        "last_digest_at should take precedence over confirmed_at"
    )
