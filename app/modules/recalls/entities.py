"""Entity extraction from recall reason text — allergens, pathogens, hazards, contaminants.

Closed regulatory vocabularies (FDA Big-9 + UK FSA 14 allergens, the named foodborne pathogens,
the common physical hazards, and the named chemical / drug / heavy-metal / toxin / pesticide
contaminants) make dictionary matching accurate and fully explainable — no model, no LLM. Each
entry maps a canonical display value to the synonyms that should resolve to it.
"""

import re
from typing import TypedDict

from app.modules.recalls.schemas import EntityType


class Entity(TypedDict):
    type: str  # an EntityType value
    value: str  # canonical display value


# (type, canonical value, synonyms). Word-boundary matched, case-insensitive. Order is the output
# order: allergens, then pathogens, then hazards, then contaminants.
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
    # Contaminants — chemical / drug / heavy-metal / toxin / pesticide. Not microbial (→ pathogen),
    # not an allergen, not a physical object (→ hazard).
    (EntityType.contaminant, "chloramphenicol", ("chloramphenicol",)),
    (EntityType.contaminant, "nitrofuran", ("nitrofuran", "nitrofurans")),
    (
        EntityType.contaminant,
        "undeclared drug",
        (
            "undeclared drug",
            "hidden drug",
            "drug residue",
            "antibiotic residue",
            "veterinary drug",
            "active pharmaceutical",
            "sildenafil",
            "tadalafil",
            "vardenafil",
            "dmaa",
            "ephedrine",
            "ephedra",
        ),
    ),
    (EntityType.contaminant, "arsenic", ("arsenic",)),
    (
        EntityType.contaminant,
        "lead",
        ("elevated lead", "lead contamination", "lead poisoning", "contains lead"),
    ),
    (EntityType.contaminant, "cadmium", ("cadmium",)),
    (EntityType.contaminant, "mercury", ("mercury",)),
    (EntityType.contaminant, "histamine", ("histamine", "scombrotoxin", "scombroid")),
    (EntityType.contaminant, "aflatoxin", ("aflatoxin", "mycotoxin", "ochratoxin")),
    (EntityType.contaminant, "patulin", ("patulin",)),
    (
        EntityType.contaminant,
        "marine biotoxin",
        (
            "marine biotoxin",
            "domoic",
            "okadaic",
            "ciguatera",
            "paralytic shellfish",
            "amnesic shellfish",
            "diarrhetic shellfish",
            "tetrodotoxin",
        ),
    ),
    (EntityType.contaminant, "Cereulide", ("cereulide", "emetic toxin")),
    (
        EntityType.contaminant,
        "pesticide",
        ("pesticide", "pesticides", "chlorpyrifos", "malathion", "glyphosate", "carbofuran"),
    ),
    (EntityType.contaminant, "ethylene oxide", ("ethylene oxide",)),
    (
        EntityType.contaminant,
        "chemical contamination",
        (
            "chemical contamination",
            "cleaning solution",
            "cleaning chemical",
            "industrial chemical",
            "sanitizer",
            "caustic",
            "sodium hydroxide",
            "petroleum",
            "hydrocarbon",
        ),
    ),
    (EntityType.contaminant, "melamine", ("melamine",)),
    (EntityType.contaminant, "dioxin", ("dioxin",)),
    (EntityType.contaminant, "benzene", ("benzene",)),
]

_COMPILED: list[tuple[EntityType, str, re.Pattern[str]]] = [
    (
        etype,
        value,
        re.compile(r"\b(?:" + "|".join(re.escape(s) for s in synonyms) + r")\b", re.IGNORECASE),
    )
    for etype, value, synonyms in _GAZETTEER
]


def extract_entities(reason_text: str) -> list[Entity]:
    # Match against the reason — *why* the recall happened — not the product, so we don't flag
    # "milk" on a milk-chocolate Listeria recall.
    return [
        {"type": etype.value, "value": value}
        for etype, value, pattern in _COMPILED
        if pattern.search(reason_text)
    ]
