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


def test_contaminants():
    assert _values("elevated levels of chloramphenicol", "contaminant") == {"chloramphenicol"}
    assert _values("recalled for pesticide residue (glyphosate)", "contaminant") == {"pesticide"}
    assert _values("scombrotoxin (histamine) fish poisoning", "contaminant") == {"histamine"}


def test_eu_scientific_contaminants():
    # EU RASFF vocabulary that used to fall through to "other". Real reason texts from the feed.
    assert _values("Acetamiprid in pears from Turkey", "contaminant") == {"pesticide"}
    assert _values("Perchlorate in herbal tea from Poland", "contaminant") == {"chlorate"}
    assert _values("Tropane alkaloids in cumin", "contaminant") == {"alkaloids"}
    assert _values("Delta-9-tetrahydrocannabinol in olive oil", "contaminant") == {"cannabinoids"}
    assert _values("Migration of Bisphenol S from pizza boxes", "contaminant") == {
        "food-contact migration"
    }


def test_more_eu_contaminants_and_pathogens():
    # Process contaminants, illegal dyes, vet-drug residues, metals, and pathogens seen in EU RASFF.
    assert _values("3-MCPD and glycidyl esters in corn snack", "contaminant") == {
        "process contaminant"
    }
    assert _values("Elevated benzo(a)pyrene in smoked sausage", "contaminant") == {
        "process contaminant"
    }
    assert _values("Unauthorized food color Sudan I in sauces", "contaminant") == {"illegal dye"}
    assert _values("Dexamethasone above the MRL in beef", "contaminant") == {
        "veterinary drug residue"
    }
    assert _values("Nickel in asparagus from Spain", "contaminant") == {"nickel"}
    assert _values("Formetanate in nectarines", "contaminant") == {"pesticide"}
    assert _values("Vibrio parahaemolyticus in live mussels", "pathogen") == {"Vibrio"}
    assert _values("Bacillus cereus in dried mushrooms", "pathogen") == {"Bacillus cereus"}
    assert _values("Anisakis in wild smoked salmon", "pathogen") == {"Anisakis"}


def test_sudan_dye_does_not_match_the_country():
    # "Sudan I" is a dye; "from Sudan" (the country) must not trigger the illegal-dye entity.
    values = _values("Aflatoxin in peanuts from Sudan")
    assert "illegal dye" not in values
    assert "aflatoxin" in values


def test_mycotoxin_plurals_and_spellings_match():
    # Word-boundary matching used to miss the plural / non-English spellings, so an incidental food
    # word decided the category. The mycotoxin must win as the contaminant.
    for text in ("excessive aflatoxins in almond powder", "Aflatoxine in Erdnüssen", "mycotoxins"):
        assert "aflatoxin" in _values(text, "contaminant"), text
    # The aflatoxin/almond case: contaminant is present alongside the incidental allergen.
    both = {e["type"] for e in extract_entities("aflatoxins in almond powder")}
    assert "contaminant" in both


def test_multiple_types_in_one_reason():
    types = {e["type"] for e in extract_entities("Undeclared peanuts and possible Salmonella")}
    assert types == {"allergen", "pathogen"}


def test_word_boundaries_avoid_false_positives():
    # 'eggplant' must not match 'egg'; 'fishery' must not match 'fish'.
    assert extract_entities("eggplant parmesan from the fishery") == []


def test_no_named_entity_returns_empty():
    assert extract_entities("Recalled due to a packaging defect") == []
