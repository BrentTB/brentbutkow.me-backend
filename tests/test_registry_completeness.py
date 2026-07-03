"""Every RecallCountry member must appear in every parallel country registry.

Adding a country touches several independent registries (see the CLAUDE.md checklist). PR #26
added Canada and PR #27 had to patch what it missed, so each check here names the exact registry
that's out of sync.
"""

import inspect
import re

from app.modules.admin.schemas import RecallCounts
from app.modules.recalls import service
from app.modules.recalls.schemas import RecallCountry
from app.subscriptions import email
from scripts import ingest_all

COUNTRIES = {member.value for member in RecallCountry}


def test_service_country_sources_cover_every_country() -> None:
    assert set(service._COUNTRY_SOURCES) == COUNTRIES, (
        "_COUNTRY_SOURCES in app/modules/recalls/service.py is out of sync with RecallCountry"
    )
    empty = [country for country, sources in service._COUNTRY_SOURCES.items() if not sources]
    assert not empty, f"countries with no ingest sources in service._COUNTRY_SOURCES: {empty}"


def test_email_agency_names_cover_every_country() -> None:
    assert set(email._COUNTRY_SOURCES) == COUNTRIES, (
        "_COUNTRY_SOURCES in app/subscriptions/email.py is out of sync with RecallCountry — "
        "digest/confirmation footers would omit the new country's agency"
    )
    empty = [
        country for country, names in email._COUNTRY_SOURCES.items() if not names or not all(names)
    ]
    assert not empty, f"countries with no agency display name in email._COUNTRY_SOURCES: {empty}"
    assert len(email._ALL_SOURCES) == len(set(email._ALL_SOURCES)), (
        f"duplicate agency names in email._ALL_SOURCES: {email._ALL_SOURCES}"
    )


def test_admin_recall_counts_has_a_field_per_country() -> None:
    missing = COUNTRIES - set(RecallCounts.model_fields)
    assert not missing, (
        f"RecallCounts in app/modules/admin/schemas.py is missing per-country fields: "
        f"{sorted(missing)}"
    )


def test_ingest_all_runs_every_service_source() -> None:
    # Each runner is a thin wrapper around _run_ingest_job(session, source="...") — read the
    # source id out of its body rather than maintaining yet another country→runner mapping here.
    expected = {src for sources in service._COUNTRY_SOURCES.values() for src in sources}
    ran = set()
    for _label, run in ingest_all._INGESTS:
        match = re.search(r'source="([a-z_]+)"', inspect.getsource(run))
        assert match, (
            f"could not find a source id in {run.__name__} — if the runner no longer wraps "
            f"_run_ingest_job(source=...), update this test's extraction"
        )
        ran.add(match.group(1))
    missing = expected - ran
    extra = ran - expected
    assert not missing, f"sources with no entry in scripts/ingest_all._INGESTS: {sorted(missing)}"
    assert not extra, f"_INGESTS runs sources unknown to service._COUNTRY_SOURCES: {sorted(extra)}"
