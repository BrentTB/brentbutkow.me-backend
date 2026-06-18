from app.modules.recalls.entities import extract_entities
from app.modules.recalls.schemas import EntityType, RecallCategory

# Keyword baseline (v1). v2 replaces this with a trained text classifier.
# Order = priority: the first rule whose keyword appears in the reason text wins.
_RULES: list[tuple[RecallCategory, list[str]]] = [
    (
        RecallCategory.allergen,
        [
            "undeclared",
            "allergen",
            "peanut",
            "tree nut",
            "milk",
            "soy",
            "egg",
            "wheat",
            "gluten",
            "sesame",
            "shellfish",
            "sulfite",
        ],
    ),
    (
        RecallCategory.pathogen,
        [
            "listeria",
            "salmonella",
            "e. coli",
            "escherichia coli",
            "botulism",
            "clostridium",
            "norovirus",
            "hepatitis a",
            "cronobacter",
            "staphylococcus",
            "pathogen",
        ],
    ),
    (
        RecallCategory.foreign_material,
        [
            "foreign material",
            "foreign matter",
            "foreign object",
            "foreign body",
            "extraneous",
            "metal",
            "plastic",
            "glass",
            "rubber",
            "wood",
        ],
    ),
    (
        RecallCategory.mislabeling,
        [
            "mislabel",
            "misbranded",
            "incorrect label",
            "wrong label",
            "labeling error",
            "incorrect ingredient",
            "ingredient statement",
            "wrong product",
        ],
    ),
]


def categorize(reason_text: str) -> RecallCategory:
    text = reason_text.lower()
    for category, keywords in _RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return RecallCategory.other


# Entity-type → category priority for the weak-supervision labeler. A named pathogen, contaminant,
# or physical hazard in the reason is the recall's cause with near-certainty; a bare allergen is
# lower-precision (an ingredient like "milk" can be incidental in the text), so it sits last.
# Pathogen wins ties, then contaminant (a named chemical/drug/toxin), then physical hazard.
_ENTITY_CATEGORY: list[tuple[EntityType, RecallCategory]] = [
    (EntityType.pathogen, RecallCategory.pathogen),
    (EntityType.contaminant, RecallCategory.contaminant),
    (EntityType.hazard, RecallCategory.foreign_material),
    (EntityType.allergen, RecallCategory.allergen),
]


def label_category(reason_text: str) -> RecallCategory:
    """Weak-supervision label: trust the typed entity gazetteer first (a closed, high-precision
    regulatory vocabulary), then fall back to the keyword baseline for mislabeling / no-entity rows.

    This is what fixes the "raw milk cheese recalled for E. coli" class — the named pathogen decides
    the category, so an incidental ingredient word ("milk") can't outrank the actual cause.
    """
    present = {entity["type"] for entity in extract_entities(reason_text)}
    for etype, category in _ENTITY_CATEGORY:
        if etype.value in present:
            return category
    return categorize(reason_text)
