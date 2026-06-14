from app.modules.recalls.schemas import RecallCategory

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
            "foreign object",
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
        ["mislabel", "misbranded", "incorrect label", "wrong label", "labeling error"],
    ),
]


def categorize(reason_text: str) -> RecallCategory:
    text = reason_text.lower()
    for category, keywords in _RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return RecallCategory.other
