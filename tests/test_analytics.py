from app.modules.recalls.analytics import build_analytics


def test_groups_similar_docs_and_finds_nearest_neighbours():
    texts = [
        "Listeria monocytogenes found in deli meat",
        "Listeria contamination detected in sliced deli meat",
        "Undeclared peanuts in chocolate cookies",
        "Undeclared peanut allergen in cookie dough",
        "Metal fragments found in frozen pizza",
        "",  # no usable text
    ]
    result = build_analytics(texts, n_topics=3, n_neighbors=2, min_df=1)

    # The empty doc gets no topic and no neighbours — never an error.
    assert result.topic_ids[5] is None
    assert result.neighbors[5] == []

    # Each near-duplicate's top neighbour is its pair, scored in (0, 1].
    assert result.neighbors[0][0][0] == 1
    assert result.neighbors[2][0][0] == 3
    assert 0 < result.neighbors[0][0][1] <= 1

    # Near-duplicates share a theme; a different hazard lands in a different theme.
    assert result.topic_ids[0] == result.topic_ids[1]
    assert result.topic_ids[2] == result.topic_ids[3]
    assert result.topic_ids[0] != result.topic_ids[2]

    # Topics are labelled by their terms, and every usable doc is assigned to exactly one.
    assert all(topic.top_terms and topic.label for topic in result.topics)
    assert sum(topic.size for topic in result.topics) == 5


def test_too_small_a_corpus_returns_no_topics_or_neighbours():
    result = build_analytics(["only one document here"], n_topics=3)
    assert result.topics == []
    assert all(topic is None for topic in result.topic_ids)
    assert all(neighbours == [] for neighbours in result.neighbors)


def test_topics_gated_below_corpus_floor_but_neighbours_kept():
    # A low-volume country (few recalls) gets no themes — NMF would be brand noise — but the
    # similar-recall neighbours still build, since they degrade gracefully.
    texts = [
        "Listeria monocytogenes found in deli meat",
        "Listeria contamination detected in sliced deli meat",
        "Undeclared peanuts in chocolate cookies",
        "Undeclared peanut allergen in cookie dough",
    ]
    result = build_analytics(texts, n_neighbors=2, min_df=1, min_topic_docs=100)

    assert result.topics == []
    assert all(topic is None for topic in result.topic_ids)
    # Neighbours are unaffected by the topic floor: each near-duplicate still pairs with its twin.
    assert result.neighbors[0][0][0] == 1
    assert result.neighbors[2][0][0] == 3
