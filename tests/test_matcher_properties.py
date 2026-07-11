from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.recalls.schemas import RecallCategory, RecallCountry
from app.subscriptions.matcher import recall_matches
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
    # EU geography — read only by the member-state narrowing criterion (default absent).
    notifying_country: str | None = None
    distribution_countries: list[str] | None = None


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
    # EU member-state narrowing — default empty so the existing property runs exercise the
    # "no constraint" path (the criterion is skipped), unchanged.
    affected_countries: list[str] | None = None


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


def test_company_filter_matches_any_of_several():
    """A multi-company subscription matches when the recall's company contains ANY entry."""
    recall = _FakeRecall(
        entities=[],
        company_name="Acme Foods Ltd",
        country="us",
        category="allergen",
        severity_label="low",
        report_date=None,
        recall_initiation_date=None,
    )
    base = dict(
        entities=[],
        countries=[],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
    )
    # One of the two names is a case-insensitive substring of the recall company → match.
    assert recall_matches(recall, _FakeSub(companies=["other corp", "acme"], **base)) is True
    # None of the names appear → no match.
    assert recall_matches(recall, _FakeSub(companies=["other corp", "globex"], **base)) is False


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
# EU member-state narrowing (affected_countries) — example-based, an EU-only refinement
# ---------------------------------------------------------------------------


def _eu_recall(notifying: str | None, distribution: list[str] | None) -> _FakeRecall:
    return _FakeRecall(
        entities=[],
        company_name=None,
        country="eu",
        category="pathogen",
        severity_label="high",
        report_date=None,
        recall_initiation_date=None,
        notifying_country=notifying,
        distribution_countries=distribution,
    )


def _eu_sub(affected: list[str] | None) -> _FakeSub:
    return _FakeSub(
        entities=[],
        companies=[],
        countries=["eu"],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
        affected_countries=affected,
    )


def test_affected_countries_empty_matches_every_eu_recall():
    # Empty narrowing = the current behaviour: every EU recall passes.
    recall = _eu_recall(notifying="FR", distribution=["ES"])
    assert recall_matches(recall, _eu_sub(None)) is True
    assert recall_matches(recall, _eu_sub([])) is True


def test_affected_countries_matches_via_notifying_or_distribution():
    de_sub = _eu_sub(["DE"])
    # Notifying country counts...
    assert recall_matches(_eu_recall("DE", None), de_sub) is True
    # ...and so does distribution membership...
    assert recall_matches(_eu_recall("FR", ["BE", "DE"]), de_sub) is True
    # ...but a recall touching neither is filtered out.
    assert recall_matches(_eu_recall("FR", ["BE", "ES"]), de_sub) is False


def test_affected_countries_is_case_insensitive():
    # Stored codes are uppercase; a lowercase subscription value must still match.
    assert recall_matches(_eu_recall("de", ["be"]), _eu_sub(["DE"])) is True


def test_affected_countries_only_constrains_eu_recalls():
    # A mixed us+eu subscription narrowed to DE: US recalls still pass (the narrowing is EU-only),
    # while EU recalls not touching DE are filtered.
    sub = _FakeSub(
        entities=[],
        companies=[],
        countries=["us", "eu"],
        categories=[],
        min_severity=None,
        last_digest_at=None,
        confirmed_at=None,
        affected_countries=["DE"],
    )
    us_recall = _FakeRecall(
        entities=[],
        company_name="Acme",
        country="us",
        category="pathogen",
        severity_label="high",
        report_date=None,
        recall_initiation_date=None,
    )
    assert recall_matches(us_recall, sub) is True  # US unaffected by the EU narrowing
    assert recall_matches(_eu_recall("FR", ["ES"]), sub) is False  # EU still narrowed to DE
    assert recall_matches(_eu_recall("DE", None), sub) is True
