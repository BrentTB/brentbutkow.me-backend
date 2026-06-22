from app.modules.recalls.schemas import RecallSource
from app.modules.recalls.seed_za import fetch_seed, normalize_seed


def test_seed_keeps_true_sources_and_unique_slugs():
    seed = fetch_seed()
    assert len(seed) >= 8
    # Each entry carries its real origin (not a generic "seed" source), so the dashboard's
    # by-source breakdown stays honest.
    sources = {e["source"] for e in seed}
    # Entries keep their true origin — woolworths/shoprite/nrcs, plus the historic ncc-administered
    # listeriosis recall — never a generic "seed" source.
    for src in (
        RecallSource.woolworths,
        RecallSource.shoprite,
        RecallSource.nrcs,
        RecallSource.ncc,
    ):
        assert src.value in sources
    assert "seed" not in sources
    assert len({e["slug"] for e in seed}) == len(seed)  # slugs unique → no PK collisions


def test_normalize_seed_enriches_from_reason():
    by_slug = {e["slug"]: e for e in fetch_seed()}

    viennas = normalize_seed(by_slug["woolworths-chicken-viennas"])
    assert viennas["source"] == "woolworths" and viennas["country"] == "za"
    assert viennas["recall_number"] == "woolworths-chicken-viennas"
    assert viennas["classification"] is None  # curated SA recalls carry no regulator class
    assert viennas["report_date"].isoformat() == "2023-05-19"
    assert {"type": "allergen", "value": "milk"} in viennas["entities"]  # undeclared milk

    # patulin was added to the gazetteer so the apple-juice recall tags as a contaminant.
    apple_juice = normalize_seed(by_slug["woolworths-100-apple-juice-200ml-cartons"])
    assert {"type": "contaminant", "value": "patulin"} in apple_juice["entities"]

    # A packaging-defect recall names no hazard entity; its true source is preserved.
    pilchards = normalize_seed(by_slug["nrcs-canned-pilchards-tomato-chilli-sauce-400g"])
    assert pilchards["source"] == "nrcs" and pilchards["entities"] == []

    # The historic listeriosis recall: a lethal pathogen + reported deaths → top severity bands.
    listeria = normalize_seed(by_slug["enterprise-foods-listeriosis-recall-2018"])
    assert listeria["source"] == "ncc"  # NCC-administered, attributed to ncc
    assert {"type": "pathogen", "value": "Listeria"} in listeria["entities"]
    assert listeria["severity_label"] in ("high", "severe")
