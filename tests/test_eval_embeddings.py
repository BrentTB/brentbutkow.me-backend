"""The embedding-vs-TF-IDF eval's scoring helpers (scripts/eval_embeddings.py) — pure, DB-free.

The model download and DB read are exercised only by running the script; what tests must pin down
is the ground-truth grouping and the ranking metrics, since a subtle off-by-one there would silently
misreport which similarity method wins."""

from scripts.eval_embeddings import evaluate_neighbors, event_groups


def test_event_groups_keeps_only_multi_recall_events():
    groups = event_groups(["E1", None, "E2", "E1", "", "E2", "E3"])
    # Sorted by event id; the singleton E3 and the null/blank ids contribute nothing.
    assert groups == [[0, 3], [2, 5]]


def test_event_groups_empty_when_no_event_repeats():
    assert event_groups(["E1", None, "E2"]) == []


def test_evaluate_neighbors_scores_hits_and_mrr():
    # Two events: {0, 1} and {2, 3}. Query 0 finds its mate at rank 1, query 1 at rank 2, and
    # queries 2 and 3 never find each other inside their lists.
    neighbors = [
        [(1, 0.9), (2, 0.5)],
        [(3, 0.8), (0, 0.7)],
        [(0, 0.6), (1, 0.5)],
        [(1, 0.4)],
    ]
    result = evaluate_neighbors(neighbors, [[0, 1], [2, 3]], ks=(1, 2))
    assert result.queries == 4
    assert result.hit_at[1] == 0.25  # only query 0 hits at rank 1
    assert result.hit_at[2] == 0.5  # queries 0 and 1
    assert result.mrr == (1.0 + 0.5 + 0.0 + 0.0) / 4


def test_evaluate_neighbors_first_match_rank_not_any_match():
    # MRR must use the FIRST event-mate's rank: 2 has mates {1, 3}; its list holds 3 at rank 1 and
    # 1 at rank 3, so the reciprocal rank is 1, not 1/3.
    neighbors = [[], [(2, 0.9)], [(3, 0.9), (0, 0.8), (1, 0.7)], [(2, 0.9)]]
    result = evaluate_neighbors(neighbors, [[1, 2, 3]], ks=(1,))
    assert result.mrr == 1.0
    assert result.hit_at[1] == 1.0


def test_evaluate_neighbors_empty_ground_truth_is_all_zero():
    result = evaluate_neighbors([[(1, 0.5)], []], [], ks=(1, 3))
    assert result.queries == 0
    assert result.hit_at == {1: 0.0, 3: 0.0}
    assert result.mrr == 0.0
