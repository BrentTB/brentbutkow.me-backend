from app.modules.recalls.categorize import categorize
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
