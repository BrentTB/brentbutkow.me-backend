from app.modules.recalls.categorize import categorize, label_category
from app.modules.recalls.schemas import RecallCategory


def test_flags_allergens():
    assert categorize("Product contains undeclared milk and soy.") == RecallCategory.allergen


def test_flags_pathogens():
    assert (
        categorize("Potential contamination with Listeria monocytogenes.")
        == RecallCategory.pathogen
    )


def test_flags_foreign_material():
    assert categorize("Product may contain metal fragments.") == RecallCategory.foreign_material


def test_flags_mislabeling():
    assert (
        categorize("Product is misbranded due to an incorrect label.") == RecallCategory.mislabeling
    )


def test_falls_back_to_other():
    assert categorize("Product quality defect of unknown origin.") == RecallCategory.other


# A named pathogen outranks an incidental ingredient allergen — the bug behind "raw milk cheese
# recalled for E. coli" landing in `allergen`. The keyword baseline still mislabels it (milk wins by
# priority); the entity-aware labeler is what fixes it.
def test_label_category_lets_a_named_pathogen_outrank_an_ingredient_allergen():
    text = "Raw milk Peppercorn cheese is recalled due to E.coli O26:H11"
    assert categorize(text) == RecallCategory.allergen  # the mislabel the model learned
    assert label_category(text) == RecallCategory.pathogen  # entity-aware fix


def test_label_category_keeps_a_genuine_undeclared_allergen():
    assert label_category("Product contains undeclared milk.") == RecallCategory.allergen


def test_label_category_flags_a_physical_hazard():
    text = "Recalled because the product may contain metal fragments."
    assert label_category(text) == RecallCategory.foreign_material


def test_label_category_falls_back_to_keywords_when_no_entity_matches():
    assert (
        label_category("Product is misbranded due to an incorrect label.")
        == RecallCategory.mislabeling
    )


def test_label_category_flags_a_chemical_contaminant():
    # A named chemical/drug contaminant is its own class — it used to vanish into `other`.
    assert (
        label_category("Shrimp found to contain the antibiotic chloramphenicol.")
        == RecallCategory.contaminant
    )
    assert categorize("Shrimp found to contain the antibiotic chloramphenicol.") == (
        RecallCategory.other
    )
