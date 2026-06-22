from app.modules.recalls import ncc_za
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.ncc_za import NccRecord, is_food_recall, normalize_ncc
from app.modules.recalls.schemas import RecallCategory
from app.modules.recalls.severity import score_severity

# Trimmed from real NCC WordPress REST API posts (thencc.org.za/wp-json/wp/v2/posts).
BUTTANUTT = {
    "id": 1,
    "slug": "product-recall-buttanutt-peanut-butter",
    "link": "https://thencc.org.za/product-recall-buttanutt-peanut-butter/",
    "date": "2026-02-09T16:43:59",
    "title": {"rendered": "Product recall &#8211; Buttanutt peanut butter"},
    "content": {
        "rendered": (
            "<p>The National Consumer Commission (NCC) has received a product recall notification "
            "from a peanut butter manufacturer, ButtaNutt (Pty) Ltd. The recall is a result of "
            "higher-than-legally-acceptable levels of aflatoxin detected in the product.</p>\n"
            "<p>The affected products failed to meet the quality standards set out under the "
            "Department of Health&#8217;s Regulation R.1145 Governing Tolerance of Fungus-Produced "
            "Toxins in Foodstuffs.</p>\n"
            "<table><tbody><tr><td>Product</td><td>Best Before</td></tr></tbody></table>"
        )
    },
    "excerpt": {"rendered": "<p>The NCC has received a product recall&hellip;</p>"},
    "categories": [119],
    "tags": [],
}

# Aptamil names its hazard (cereulide) deep in the body, and its title has no food word, so the
# filter must scan the full content, not just the title or a short reason slice.
APTAMIL = {
    "id": 2,
    "slug": "product-safety-recall-nutricia-aptamil-aptajunior",
    "link": "https://thencc.org.za/product-safety-recall-nutricia-aptamil/",
    "date": "2025-11-03T10:00:00",
    "title": {
        "rendered": "Product safety recall &#8211; Nutricia Aptamil Nutribiotik 2 and Aptajunior 3"
    },
    "content": {
        "rendered": (
            "<p>The National Consumer Commission (NCC) alerts consumers of the "
            "recall of Nutricia Aptamil Nutribiotik 2 and Aptajunior Nutribiotik 3, "
            "as notified by Nutricia Southern Africa. The manufacturer informed the "
            "NCC that the recall affects 2989 units. Nutricia indicated that a raw "
            "material used in production may carry traces of cereulide. Cereulide is "
            "a toxin that, at high exposure, can cause symptoms.</p>"
        )
    },
    "excerpt": {"rendered": "<p>The NCC alerts consumers&hellip;</p>"},
    "categories": [119],
    "tags": [],
}

# A boilerplate stub: a recall-prefixed post whose body is only a "contact us" placeholder (the real
# write-up lives in a separate, non-prefixed post). The food signal is in the title.
MCCAIN_STUB = {
    "id": 3,
    "slug": "product-recall-mccain-beans-and-spar-stir-fry-products",
    "link": "https://thencc.org.za/product-recall-mccain-beans-and-spar-stir-fry-products/",
    "date": "2024-08-01T09:00:00",
    "title": {"rendered": "Product Recall: McCain Beans and Spar Stir Fry Products"},
    "content": {
        "rendered": (
            "<p>To contact The National Consumer Commission about this and other Product Recalls, "
            "use any of the channels that can be found on our Contact page.</p>"
        )
    },
    "excerpt": {"rendered": ""},
    "categories": [119],
    "tags": [],
}

HINO_TRUCKS = {
    "id": 4,
    "slug": "product-recall-hino-700-series-trucks",
    "title": {"rendered": "Product recall &#8211; Hino 700 Series trucks"},
    "content": {
        "rendered": (
            "<p>The NCC notifies consumers about a product recall of certain Hino 700 "
            "Series trucks, as notified by Toyota South Africa Motors (Pty) Ltd.</p>"
        )
    },
    "excerpt": {"rendered": ""},
}

PET_FOOD = {
    "id": 5,
    "slug": "product-safety-recall-115-045-rcl-various-dry-pet-foods",
    "title": {"rendered": "Product safety recall &#8211; 115 045 RCL various dry pet foods"},
    "content": {
        "rendered": "<p>The NCC notifies consumers of the recall of various brands of dry dog and "
        "cat food products, as communicated by RCL Foods.</p>"
    },
    "excerpt": {"rendered": ""},
}

# A retailer-initiated recall published only as a "media-statement-…recall…" post (no dedicated
# product-recall-* twin) — the broadened filter must catch these. Title carries "Media Statement:" +
# "Product Safety Recall" boilerplate around the product.
HUMMUS_MS = {
    "id": 7,
    "slug": "media-statement-deli-hummus-range-product-safety-recall",
    "link": "https://thencc.org.za/media-statement-deli-hummus-range-product-safety-recall/",
    "date": "2024-10-03T09:00:00",
    "title": {"rendered": "Media Statement: Deli Hummus range Product Safety Recall"},
    "content": {
        "rendered": (
            "<p>The National Consumer Commission (NCC) notifies consumers of a product safety "
            "recall of the Deli Hummus range, as notified by the Shoprite Group, after Listeria "
            "monocytogenes was detected in certain batches.</p>"
        )
    },
    "excerpt": {"rendered": ""},
}

# Title-only deny: a food recall whose BODY mentions a non-food word ("truck") must still be kept,
# because the deny-list reads only the title (the product class is always named there).
CLOVER_MS = {
    "id": 8,
    "slug": "media-statement-clover-peanut-butter-recall",
    "title": {"rendered": "Media Statement: Clover Peanut Butter Recall"},
    "content": {
        "rendered": (
            "<p>Clover is recalling peanut butter over aflatoxin levels. Affected stock was "
            "distributed by truck to retailers nationwide.</p>"
        )
    },
    "excerpt": {"rendered": ""},
}

# A periodic digest that lists many recalls in one post — skipped (not a single product recall),
# even though its body mentions food hazards.
DIGEST = {
    "id": 9,
    "slug": "media-statement-update-on-20-product-recalls-administered-during-quarter",
    "title": {
        "rendered": "Media Statement: Update on 20 product recalls administered this quarter"
    },
    "content": {
        "rendered": "<p>This quarter's recalls included peanut butter and listeria cases.</p>"
    },
    "excerpt": {"rendered": ""},
}

# A non-recall media statement (an inspection notice) — no "recall" in the slug, so not ingested,
# even though its body mentions peanut butter.
INSPECTION = {
    "id": 10,
    "slug": "media-statement-ncc-conducts-market-monitoring-inspections",
    "title": {"rendered": "Media Statement: NCC conducts market monitoring inspections"},
    "content": {
        "rendered": "<p>The NCC inspected food products including peanut butter at retailers.</p>"
    },
    "excerpt": {"rendered": ""},
}


def _normalize(monkeypatch, raw, category=RecallCategory.contaminant):
    # Isolate normalization from the ML model — assert mapping, not the classifier's output.
    monkeypatch.setattr(ncc_za, "classify", lambda text: (category, 0.9))
    return normalize_ncc(NccRecord.model_validate(raw))


def test_normalize_buttanutt(monkeypatch):
    row = _normalize(monkeypatch, BUTTANUTT)
    assert row["source"] == "ncc"
    assert row["country"] == "za"
    assert (
        row["recall_number"] == "product-recall-buttanutt-peanut-butter"
    )  # slug is the identifier
    assert row["source_url"].startswith("https://thencc.org.za")
    assert row["classification"] is None  # NCC issues no FDA-style class
    assert row["state"] is None and row["states"] is None  # no geography
    assert row["report_date"].isoformat() == "2026-02-09"
    # Prefix stripped off the title to get the product; the en-dash entity is decoded.
    assert row["product_description"] == "Buttanutt peanut butter"
    # Reason is the article lede (hazard named), not the trailing product table.
    assert "aflatoxin" in row["reason_text"].lower()
    assert "Product" not in row["reason_text"].split()[-3:]  # table text excluded
    assert row["company_name"] == "ButtaNutt (Pty) Ltd"
    assert {"type": "contaminant", "value": "aflatoxin"} in row["entities"]
    # No classification, no geography → severity rests on category + entities + reason. Re-derive.
    expected_score, expected_label = score_severity(
        classification=None,
        category=RecallCategory.contaminant.value,
        entities=extract_entities(row["reason_text"]),
        reason_text=row["reason_text"],
    )
    assert row["severity_score"] == expected_score
    assert row["severity_label"] == expected_label


def test_normalize_boilerplate_stub_falls_back_to_title(monkeypatch):
    row = _normalize(monkeypatch, MCCAIN_STUB, category=RecallCategory.other)
    # The placeholder body is not used as the reason; the cleaned title stands in for it.
    assert not row["reason_text"].lower().startswith("to contact")
    assert (
        row["reason_text"]
        == row["product_description"]
        == "McCain Beans and Spar Stir Fry Products"
    )


def test_normalize_finds_hazard_named_deep_in_body(monkeypatch):
    row = _normalize(monkeypatch, APTAMIL)
    assert "cereulide" in row["reason_text"].lower()
    assert {"type": "contaminant", "value": "Cereulide"} in row["entities"]
    assert row["company_name"] == "Nutricia Southern Africa"


def test_is_food_recall_keeps_human_food():
    # Includes a media-statement-only recall (HUMMUS_MS) and a title-with-no-food-word recall whose
    # body names the hazard (APTAMIL → cereulide).
    for fixture in (BUTTANUTT, MCCAIN_STUB, APTAMIL, HUMMUS_MS):
        assert is_food_recall(NccRecord.model_validate(fixture))


def test_is_food_recall_deny_reads_title_only():
    # A deny word ("truck") in the BODY must not drop a food recall — the deny-list reads the title.
    assert is_food_recall(NccRecord.model_validate(CLOVER_MS))


def test_is_food_recall_drops_non_food_and_non_recalls():
    assert not is_food_recall(NccRecord.model_validate(HINO_TRUCKS))  # vehicle
    assert not is_food_recall(NccRecord.model_validate(PET_FOOD))  # pet food, not human food
    assert not is_food_recall(
        NccRecord.model_validate(INSPECTION)
    )  # not a recall (no "recall" slug)
    assert not is_food_recall(NccRecord.model_validate(DIGEST))  # periodic digest, not one recall


def test_normalize_media_statement_recall(monkeypatch):
    # The broadened filter ingests media-statement recalls; the title boilerplate
    # ("Media Statement: … Product Safety Recall") is stripped to the product.
    row = _normalize(monkeypatch, HUMMUS_MS, category=RecallCategory.pathogen)
    assert row["source"] == "ncc" and row["country"] == "za"
    assert row["recall_number"] == "media-statement-deli-hummus-range-product-safety-recall"
    assert row["product_description"] == "Deli Hummus range"
    assert "listeria" in row["reason_text"].lower()
    assert {"type": "pathogen", "value": "Listeria"} in row["entities"]


def test_dedupe_prefers_product_recall_over_media_statement_twin():
    def rec(slug):
        return NccRecord.model_validate(
            {"slug": slug, "title": {"rendered": "x"}, "content": {"rendered": ""}}
        )

    pr = rec("product-recall-hino-700-series-trucks")
    ms = rec(
        "media-statement-recall-of-hino-700-series-trucks"
    )  # same recall, other slug convention
    deduped = ncc_za._dedupe([ms, pr])
    assert len(deduped) == 1
    assert deduped[0].slug == "product-recall-hino-700-series-trucks"  # the dedicated post wins
    # Two genuinely different recalls are both kept.
    assert len(ncc_za._dedupe([pr, rec("product-recall-buttanutt-peanut-butter")])) == 2
