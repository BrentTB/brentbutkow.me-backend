"""build_analytics — themes (k-means over embedding rows) + neighbours, on synthetic vectors.

The tests hand-craft small L2-normalised embeddings instead of loading the real model: the pure
function must be exercisable offline (CI has no Hugging Face access), and synthetic vectors make
the expected geometry — who clusters with whom, who clears the centroid gate — exact rather than
model-dependent."""

import numpy as np
import pytest

from app.modules.recalls.analytics import _compute_neighbors, _novelty, build_analytics


def _rows(*vectors: tuple[float, ...]) -> np.ndarray:
    rows = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


def test_compute_neighbors_accepts_dense_embedding_rows():
    # The neighbour code serves both the sparse TF-IDF eval baseline and the shipped dense
    # embedding rows (scripts/eval_embeddings.py compares the two).
    rows = _rows((1.0, 0.0), (0.96, 0.28), (0.0, 1.0))
    neighbors = _compute_neighbors(rows, n_neighbors=2)

    # Row 1 sits between the other two, so it's everyone's nearest neighbour; the orthogonal pair
    # (rows 0 and 2) never surface each other because a 0.0 score doesn't clear the > 0 cut.
    assert [idx for idx, _ in neighbors[0]] == [1]
    assert neighbors[0][0][1] == pytest.approx(0.96)
    assert neighbors[2][0][0] == 1
    assert [idx for idx, _ in neighbors[1]] == [0, 2]
    assert all(0 < score <= 1 for nbrs in neighbors for _, score in nbrs)


def test_groups_similar_docs_and_finds_nearest_neighbours():
    texts = [
        "Listeria monocytogenes found in deli meat",
        "Listeria contamination detected in sliced deli meat",
        "Undeclared peanuts in chocolate cookies",
        "Undeclared peanut allergen in cookie dough",
        "Metal fragments found in frozen pizza",
        "",  # no usable text
    ]
    # Two tight pairs + one loner, on three near-orthogonal directions; the empty doc is all-zero.
    embeddings = _rows(
        (1.0, 0.1, 0.0),
        (0.98, 0.2, 0.0),
        (0.1, 1.0, 0.0),
        (0.2, 0.98, 0.0),
        (0.0, 0.1, 1.0),
        (0.0, 0.0, 0.0),
    )
    result = build_analytics(texts, embeddings, n_topics=3, n_neighbors=2, min_df=1)

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

    # Topics are labelled by their members' TF-IDF terms; every usable doc clears the centroid
    # gate here (tight clusters), so the sizes cover the whole nonempty corpus.
    assert all(topic.top_terms and topic.label for topic in result.topics)
    assert sum(topic.size for topic in result.topics) == 5
    listeria_topic = result.topics[result.topic_ids[0]]
    assert any("listeria" in term for term in listeria_topic.top_terms)


def test_centroid_gate_drops_far_from_theme_recalls():
    texts = [
        "Listeria monocytogenes found in deli meat",
        "Listeria contamination detected in sliced deli meat",
        "Metal fragments found in frozen pizza",
    ]
    # Forced into one cluster (n_topics=1): the pair hugs the centroid, the orthogonal outlier
    # doesn't. With the gate it keeps no theme rather than a misleading one.
    embeddings = _rows((1.0, 0.05, 0.0), (1.0, -0.05, 0.0), (0.0, 0.0, 1.0))
    result = build_analytics(texts, embeddings, n_topics=1, min_df=1, min_topic_sim=0.8)

    assert result.topic_ids[0] == result.topic_ids[1] == 0
    assert result.topic_ids[2] is None
    assert result.topics[0].size == 2  # gated size counts only kept assignments

    # Loosening the gate admits the outlier — the constant is what does the gating.
    loose = build_analytics(texts, embeddings, n_topics=1, min_df=1, min_topic_sim=0.1)
    assert loose.topic_ids[2] == 0
    assert loose.topics[0].size == 3


def test_embeddings_must_align_with_texts():
    with pytest.raises(ValueError):
        build_analytics(["a", "b"], _rows((1.0, 0.0)))


def test_novelty_is_one_minus_mean_of_top_k():
    # Neighbours arrive sorted by descending cosine; novelty averages the closest _NOVELTY_K (3).
    assert _novelty([(0, 0.9), (1, 0.8), (2, 0.7), (3, 0.1)], available=6) == pytest.approx(0.2)
    # A crowded neighbourhood (all near 1.0) is ordinary → low novelty.
    assert _novelty([(0, 1.0), (1, 1.0), (2, 1.0)], available=6) == pytest.approx(0.0)
    # Fewer than K positive neighbours: the missing slots floor at cosine 0, so an isolated recall
    # scores as *more* novel rather than dropping out of the "unusual recalls" feed.
    assert _novelty([(0, 0.9), (1, 0.8)], available=6) == pytest.approx(0.4333)
    assert _novelty([], available=6) == pytest.approx(1.0)
    # None only when the corpus itself is too small to hold _NOVELTY_K other recalls to compare.
    assert _novelty([(0, 0.9), (1, 0.8)], available=2) is None
    assert _novelty([], available=0) is None


def test_build_analytics_scores_novelty_and_leaves_isolated_recalls_most_novel():
    # Two tight clusters plus one point weakly related to everything: it is the most novel. The
    # corpus holds >= _NOVELTY_K other recalls, so every recall is scored (None only for a corpus
    # too small to compare).
    texts = ["a", "b", "c", "d", "e", "f", "g"]
    embeddings = _rows(
        (1.0, 0.02, 0.02),
        (1.0, 0.03, 0.01),
        (0.98, 0.05, 0.0),
        (0.02, 1.0, 0.02),
        (0.01, 1.0, 0.03),
        (0.0, 0.98, 0.05),
        (0.35, 0.35, 0.5),  # weakly aligned to both clusters — isolated, but still has neighbours
    )
    result = build_analytics(texts, embeddings, n_neighbors=3, min_df=1, min_topic_docs=100)

    assert len(result.novelty) == len(texts)
    assert all(0.0 <= n <= 1.0 for n in result.novelty if n is not None)
    isolated = result.novelty[6]
    assert isolated is not None
    # The isolated point is more novel than any cluster member.
    assert all(isolated > result.novelty[i] for i in range(6))


def test_theme_embeddings_drive_clustering_independently_of_neighbours():
    # rebuild_analytics feeds a reason-dominant embedding for themes and a product-aware one for
    # neighbours. Craft the two so they disagree: under the neighbour vectors docs {0,1} pair and
    # {2,3} pair, but under the theme vectors the grouping is {0,2} vs {1,3}. Themes must follow the
    # theme vectors; neighbours must follow the neighbour vectors.
    texts = ["listeria deli", "peanut cookie", "listeria cheese", "peanut candy"]
    neighbor_emb = _rows((1.0, 0.05), (1.0, -0.05), (-1.0, 0.05), (-1.0, -0.05))
    theme_emb = _rows((0.05, 1.0), (0.05, -1.0), (-0.05, 1.0), (-0.05, -1.0))
    result = build_analytics(
        texts,
        neighbor_emb,
        theme_texts=texts,
        theme_embeddings=theme_emb,
        n_topics=2,
        n_neighbors=1,
        min_df=1,
    )

    # Neighbours follow neighbor_emb: 0↔1 and 2↔3.
    assert result.neighbors[0][0][0] == 1
    assert result.neighbors[2][0][0] == 3
    # Themes follow theme_emb: 0 with 2 (top halves), 1 with 3 (bottom halves).
    assert result.topic_ids[0] == result.topic_ids[2]
    assert result.topic_ids[1] == result.topic_ids[3]
    assert result.topic_ids[0] != result.topic_ids[1]


def test_theme_embeddings_must_align_with_texts():
    with pytest.raises(ValueError):
        build_analytics(
            ["a", "b", "c"],
            _rows((1.0, 0.0), (0.0, 1.0), (1.0, 1.0)),
            theme_embeddings=_rows((1.0, 0.0), (0.0, 1.0)),
        )


def test_too_small_a_corpus_returns_no_topics_or_neighbours():
    result = build_analytics(["only one document here"], _rows((1.0, 0.0)), n_topics=3)
    assert result.topics == []
    assert all(topic is None for topic in result.topic_ids)
    assert all(neighbours == [] for neighbours in result.neighbors)


def test_topics_gated_below_corpus_floor_but_neighbours_kept():
    # A low-volume country (few recalls) gets no themes — clusters there would be one-off brand
    # noise — but the similar-recall neighbours still build, since they degrade gracefully.
    texts = [
        "Listeria monocytogenes found in deli meat",
        "Listeria contamination detected in sliced deli meat",
        "Undeclared peanuts in chocolate cookies",
        "Undeclared peanut allergen in cookie dough",
    ]
    embeddings = _rows((1.0, 0.1), (0.98, 0.2), (0.1, 1.0), (0.2, 0.98))
    result = build_analytics(texts, embeddings, n_neighbors=2, min_df=1, min_topic_docs=100)

    assert result.topics == []
    assert all(topic is None for topic in result.topic_ids)
    # Neighbours are unaffected by the topic floor: each near-duplicate still pairs with its twin.
    assert result.neighbors[0][0][0] == 1
    assert result.neighbors[2][0][0] == 3
