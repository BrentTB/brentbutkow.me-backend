from app.modules.recalls.entities import extract_entities


def _values(text: str, etype: str | None = None) -> set[str]:
    return {e["value"] for e in extract_entities(text) if etype is None or e["type"] == etype}


def test_allergen_synonyms_collapse_to_canonical():
    pairs = {
        (e["type"], e["value"]) for e in extract_entities("Undeclared milk, soya and groundnuts")
    }
    assert ("allergen", "milk") in pairs
    assert ("allergen", "soybeans") in pairs  # soya → soybeans
    assert ("allergen", "peanuts") in pairs  # groundnuts → peanuts


def test_tree_nut_specifics_collapse():
    assert _values("contains walnuts and almonds", "allergen") == {"tree nuts"}


def test_pathogens():
    assert _values("Possible Listeria monocytogenes contamination", "pathogen") == {"Listeria"}
    assert "E. coli" in _values("E. coli O157:H7 detected", "pathogen")


def test_hazards():
    assert _values("may contain metal", "hazard") == {"metal"}


def test_multiple_types_in_one_reason():
    types = {e["type"] for e in extract_entities("Undeclared peanuts and possible Salmonella")}
    assert types == {"allergen", "pathogen"}


def test_word_boundaries_avoid_false_positives():
    # 'eggplant' must not match 'egg'; 'fishery' must not match 'fish'.
    assert extract_entities("eggplant parmesan from the fishery") == []


def test_no_named_entity_returns_empty():
    assert extract_entities("Recalled due to a packaging defect") == []
