from app.modules.recalls.schemas import RecallCategory, RecallClass, SeverityLabel
from app.modules.recalls.severity import score_severity


def _ent(type_: str, value: str) -> dict[str, str]:
    return {"type": type_, "value": value}


def test_class_i_is_always_severe():
    # Class I anchors at 75, so even the least-severe cause stays in the severe band.
    score, label = score_severity(
        classification=RecallClass.class_i.value,
        category=RecallCategory.mislabeling.value,
        entities=[],
    )
    assert score >= 75
    assert label == SeverityLabel.severe.value


def test_class_iii_is_low():
    score, label = score_severity(
        classification=RecallClass.class_iii.value,
        category=RecallCategory.mislabeling.value,
        entities=[],
    )
    assert score < 35
    assert label == SeverityLabel.low.value


def test_cause_breaks_ties_within_a_class():
    # Same classification, deadlier cause ⇒ higher score (the nudge).
    pathogen, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.pathogen.value,
        entities=[],
    )
    mislabel, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.mislabeling.value,
        entities=[],
    )
    assert pathogen > mislabel


def test_deadliest_entity_adds_a_bonus():
    base, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.pathogen.value,
        entities=[],
    )
    with_listeria, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.pathogen.value,
        entities=[_ent("pathogen", "Listeria")],
    )
    assert with_listeria > base


def test_nationwide_distribution_outweighs_a_single_state():
    nationwide, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        distribution_pattern="Nationwide",
    )
    single, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        states=["CA"],
    )
    assert nationwide > single


def test_international_does_not_trip_the_nationwide_rule():
    # "international" contains the substring "national" but must not earn the nationwide bonus.
    intl, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        distribution_pattern="International distribution only",
    )
    single, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        states=["CA"],
    )
    assert intl == single


def test_many_states_add_breadth():
    wide, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        states=["CA", "NY", "TX", "FL", "WA", "OR"],
    )
    one, _ = score_severity(
        classification=RecallClass.class_ii.value,
        category=RecallCategory.allergen.value,
        entities=[],
        states=["CA"],
    )
    assert wide > one


def test_falls_back_to_category_base_without_a_classification():
    pathogen, _ = score_severity(
        classification=None, category=RecallCategory.pathogen.value, entities=[]
    )
    other, _ = score_severity(classification=None, category=RecallCategory.other.value, entities=[])
    assert pathogen > other


def test_unknown_classification_is_treated_like_a_missing_one():
    bogus, _ = score_severity(
        classification="Bogus Class", category=RecallCategory.pathogen.value, entities=[]
    )
    missing, _ = score_severity(
        classification=None, category=RecallCategory.pathogen.value, entities=[]
    )
    assert bogus == missing


def test_score_is_clamped_to_100():
    score, label = score_severity(
        classification=RecallClass.class_i.value,
        category=RecallCategory.pathogen.value,
        entities=[_ent("pathogen", "Listeria")],
        states=["CA", "NY", "TX", "FL", "WA", "OR"],
        distribution_pattern="Nationwide",
    )
    assert score == 100.0
    assert label == SeverityLabel.severe.value


def test_uk_alert_types_land_on_the_same_scale():
    # UK vocabulary maps onto the same scale: action-required outranks a plain allergy alert.
    fafa, _ = score_severity(
        classification=RecallClass.food_alert_for_action.value,
        category=RecallCategory.pathogen.value,
        entities=[],
    )
    aa, _ = score_severity(
        classification=RecallClass.allergy_alert.value,
        category=RecallCategory.allergen.value,
        entities=[],
    )
    assert fafa > aa
