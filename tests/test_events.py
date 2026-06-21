from datetime import date

from app.modules.recalls.events import EventInput, cluster_events


def _rec(
    when: date,
    *,
    pathogens: tuple[str, ...] = (),
    entities: tuple[str, ...] | None = None,
    company: str = "A",
    states: tuple[str, ...] = (),
    severity: float = 50.0,
    event_id: str | None = None,
) -> EventInput:
    return EventInput(
        pathogens=frozenset(pathogens),
        entities=entities if entities is not None else pathogens,
        report_date=when,
        company=company,
        states=states,
        severity=severity,
        event_id=event_id,
    )


def test_shared_pathogen_time_and_text_link_across_companies():
    # Three Listeria recalls from different firms, chained by cosine edges within the window — one
    # cross-company outbreak.
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",), company="A"),
        _rec(date(2026, 1, 15), pathogens=("Listeria",), company="B"),
        _rec(date(2026, 1, 28), pathogens=("Listeria",), company="C"),
    ]
    result = cluster_events(recalls, [(0, 1, 0.6), (1, 2, 0.6)])
    assert len({result.cluster_ids[i] for i in range(3)}) == 1  # all in one cluster
    cluster = result.clusters[result.cluster_ids[0]]
    assert cluster.is_outbreak  # ≥3 recalls + shared pathogen
    assert cluster.dominant_entity == "Listeria"
    assert cluster.company_count == 3


def test_no_shared_pathogen_does_not_link():
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",)),
        _rec(date(2026, 1, 2), pathogens=("Salmonella",)),
    ]
    result = cluster_events(recalls, [(0, 1, 0.9)])  # very similar text, but different pathogens
    assert result.clusters == []
    assert result.cluster_ids == [None, None]


def test_outside_time_window_does_not_link():
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",)),
        _rec(date(2026, 9, 1), pathogens=("Listeria",)),  # ~8 months later
    ]
    result = cluster_events(recalls, [(0, 1, 0.9)])
    assert result.clusters == []


def test_below_min_score_does_not_link():
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",)),
        _rec(date(2026, 1, 5), pathogens=("Listeria",)),
    ]
    result = cluster_events(recalls, [(0, 1, 0.2)])  # weak similarity
    assert result.clusters == []


def test_same_event_id_links_regardless_of_text():
    # An FDA event groups recalls even with no cosine edge between them — and an allergen-only event
    # is a plain event, not an outbreak.
    recalls = [
        _rec(date(2026, 1, 1), entities=("milk",), event_id="EV1"),
        _rec(date(2026, 1, 2), entities=("milk",), event_id="EV1"),
    ]
    result = cluster_events(recalls, [])
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert len(cluster.members) == 2
    assert not cluster.is_outbreak
    assert cluster.dominant_entity == "milk"


def test_two_recall_pathogen_cluster_is_event_not_outbreak():
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",)),
        _rec(date(2026, 1, 5), pathogens=("Listeria",)),
    ]
    result = cluster_events(recalls, [(0, 1, 0.7)])
    assert len(result.clusters) == 1
    assert not result.clusters[0].is_outbreak  # size 2 is below the outbreak floor


def test_lone_pathogen_tag_does_not_make_an_outbreak():
    # A pathogen named in just one member of an allergen cascade must not flip it to an outbreak.
    recalls = [
        _rec(date(2026, 1, 1), entities=("milk",), event_id="EV"),
        _rec(date(2026, 1, 2), entities=("milk",), event_id="EV"),
        _rec(
            date(2026, 1, 3), pathogens=("Listeria",), entities=("Listeria", "milk"), event_id="EV"
        ),
    ]
    result = cluster_events(recalls, [])
    cluster = result.clusters[0]
    assert len(cluster.members) == 3
    assert not cluster.is_outbreak  # Listeria appears in only 1 member → not a shared driver
    assert cluster.dominant_entity == "milk"  # shared by all three


def test_cluster_metadata():
    recalls = [
        _rec(
            date(2026, 1, 1),
            pathogens=("Salmonella",),
            company="A",
            states=("CA", "NY"),
            severity=60,
        ),
        _rec(
            date(2026, 1, 10),
            pathogens=("Salmonella",),
            company="B",
            states=("NY", "TX"),
            severity=80,
        ),
        _rec(
            date(2026, 1, 20), pathogens=("Salmonella",), company="A", states=("CA",), severity=70
        ),
    ]
    result = cluster_events(recalls, [(0, 1, 0.7), (1, 2, 0.7)])
    cluster = result.clusters[result.cluster_ids[0]]
    assert cluster.company_count == 2  # A, B
    assert cluster.state_count == 3  # CA, NY, TX
    assert cluster.first_date == date(2026, 1, 1)
    assert cluster.last_date == date(2026, 1, 20)
    assert cluster.severity_max == 80.0
    assert "Salmonella" in cluster.label and "3 recalls" in cluster.label


def test_singletons_and_empty_get_no_clusters():
    recalls = [
        _rec(date(2026, 1, 1), pathogens=("Listeria",)),
        _rec(date(2026, 1, 2), pathogens=("Salmonella",)),
    ]
    result = cluster_events(recalls, [])  # no edges, no shared event_id
    assert result.clusters == []
    assert result.cluster_ids == [None, None]

    empty = cluster_events([], [])
    assert empty.clusters == []
    assert empty.cluster_ids == []
