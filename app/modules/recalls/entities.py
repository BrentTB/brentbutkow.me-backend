"""Entity extraction from recall reason text — allergens, pathogens, foreign-material hazards.

Closed regulatory vocabularies (FDA Big-9 + UK FSA 14 allergens, the named foodborne pathogens,
the common physical hazards) make dictionary matching accurate and fully explainable — no model,
no LLM. Each entry maps a canonical display value to the synonyms that should resolve to it.
"""

import re

from app.modules.recalls.schemas import EntityType

# (type, canonical value, synonyms). Word-boundary matched, case-insensitive. Order is the output
# order: allergens, then pathogens, then hazards.
_GAZETTEER: list[tuple[EntityType, str, tuple[str, ...]]] = [
    (EntityType.allergen, "milk", ("milk",)),
    (EntityType.allergen, "egg", ("egg", "eggs")),
    (EntityType.allergen, "fish", ("fish",)),
    (EntityType.allergen, "crustaceans", ("crustacean", "crustaceans", "shellfish")),
    (EntityType.allergen, "molluscs", ("mollusc", "molluscs", "mollusk", "mollusks")),
    (EntityType.allergen, "peanuts", ("peanut", "peanuts", "groundnut", "groundnuts")),
    (
        EntityType.allergen,
        "tree nuts",
        (
            "tree nut",
            "tree nuts",
            "almond",
            "almonds",
            "walnut",
            "walnuts",
            "cashew",
            "cashews",
            "pecan",
            "pecans",
            "hazelnut",
            "hazelnuts",
            "pistachio",
            "pistachios",
            "macadamia",
            "brazil nut",
        ),
    ),
    (EntityType.allergen, "soybeans", ("soy", "soya", "soybean", "soybeans")),
    (EntityType.allergen, "gluten", ("gluten", "wheat", "barley", "rye", "spelt")),
    (EntityType.allergen, "sesame", ("sesame", "tahini")),
    (EntityType.allergen, "celery", ("celery",)),
    (EntityType.allergen, "mustard", ("mustard",)),
    (EntityType.allergen, "lupin", ("lupin", "lupine")),
    (
        EntityType.allergen,
        "sulphites",
        ("sulphite", "sulphites", "sulfite", "sulfites", "sulphur dioxide", "sulfur dioxide"),
    ),
    (EntityType.pathogen, "Listeria", ("listeria", "monocytogenes")),
    (EntityType.pathogen, "Salmonella", ("salmonella",)),
    (EntityType.pathogen, "E. coli", ("e. coli", "e.coli", "escherichia", "stec", "o157")),
    (EntityType.pathogen, "Clostridium botulinum", ("botulism", "clostridium")),
    (EntityType.pathogen, "Hepatitis A", ("hepatitis",)),
    (EntityType.pathogen, "Norovirus", ("norovirus",)),
    (EntityType.pathogen, "Cyclospora", ("cyclospora",)),
    (EntityType.pathogen, "Cronobacter", ("cronobacter",)),
    (EntityType.pathogen, "Campylobacter", ("campylobacter",)),
    (EntityType.pathogen, "Staphylococcus", ("staphylococcus", "staph aureus")),
    (EntityType.hazard, "metal", ("metal",)),
    (EntityType.hazard, "plastic", ("plastic",)),
    (EntityType.hazard, "glass", ("glass",)),
    (EntityType.hazard, "rubber", ("rubber",)),
    (EntityType.hazard, "wood", ("wood", "wooden")),
    (EntityType.hazard, "bone", ("bone",)),
    (EntityType.hazard, "stone", ("stone", "stones", "rock")),
    (EntityType.hazard, "insect", ("insect", "insects", "beetle", "larvae")),
]

_COMPILED: list[tuple[EntityType, str, re.Pattern[str]]] = [
    (
        etype,
        value,
        re.compile(r"\b(?:" + "|".join(re.escape(s) for s in synonyms) + r")\b", re.IGNORECASE),
    )
    for etype, value, synonyms in _GAZETTEER
]


def extract_entities(reason_text: str) -> list[dict[str, str]]:
    # Match against the reason — *why* the recall happened — not the product, so we don't flag
    # "milk" on a milk-chocolate Listeria recall.
    return [
        {"type": etype.value, "value": value}
        for etype, value, pattern in _COMPILED
        if pattern.search(reason_text)
    ]
