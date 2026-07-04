"""Benchmark the shipped embedding similarity against the previous TF-IDF, on event ground truth.

Ground truth is free: openFDA's `event_id` links recalls belonging to the same real-world event
(one contaminated ingredient recalled across many downstream products). For every recall in a
multi-recall event, a good similarity method should rank its event-mates among the nearest
neighbours. This script composes the corpus text exactly as production does
(app/modules/recalls/analytics.py), builds top-k neighbour lists twice — once with the pre-swap
TF-IDF, once with the shipped Model2Vec embeddings — scores both against the event pairs (hit@k,
MRR), prints the comparison, and writes a methodology card next to the classifier's model_card.md.
This is the eval that justified the embeddings swap; rerun it to regression-check the analytics
text pipeline, or with --model to audition a different embedding model (e.g. after a hosting
upgrade makes a transformer viable).

Run locally with DATABASE_URL set (the model downloads from Hugging Face on first use):

    python -m scripts.eval_embeddings [--neighbors 8] [--model minishlab/potion-base-8M]

Honest limits: same-event recalls often share near-verbatim reason text, so absolute scores flatter
both methods — the *gap* between them is the signal. Only openFDA publishes event_id, so the eval
is US-only (FSIS rows stay in the corpus as realistic distractors). A same-event miss can still be
a reasonable neighbour, and cross-event neighbour quality is not judged at all.
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.db import SessionLocal
from app.modules.recalls.analytics import (
    _compose_text,
    _compute_neighbors,
    _corpus_min_df,
    _make_vectorizer,
)
from app.modules.recalls.classifier import MODEL_PATH
from app.modules.recalls.embeddings import MODEL_NAME, embed_texts
from app.modules.recalls.models import Recall

_COUNTRY = "us"  # only openFDA carries event_id, so ground truth exists only in the US corpus
_DEFAULT_NEIGHBORS = 8  # matches production's kept-neighbour count
_HIT_KS = (1, 3, 8)

# Ranked (index, cosine score) neighbour lists, aligned 1:1 with the corpus — the shape
# analytics._compute_neighbors returns.
Neighbors = list[list[tuple[int, float]]]


@dataclass
class EvalResult:
    queries: int
    hit_at: dict[int, float]
    mrr: float


def event_groups(event_ids: Sequence[str | None]) -> list[list[int]]:
    """Corpus indices grouped by event_id, keeping only groups where ≥2 recalls share the event."""
    groups: dict[str, list[int]] = {}
    for index, event_id in enumerate(event_ids):
        if event_id:
            groups.setdefault(event_id, []).append(index)
    return [members for _, members in sorted(groups.items()) if len(members) >= 2]


def evaluate_neighbors(
    neighbors: Neighbors, groups: list[list[int]], ks: tuple[int, ...] = _HIT_KS
) -> EvalResult:
    """Score ranked neighbour lists against same-event ground truth.

    Every member of every group is one query; its targets are the group's other members. The rank
    of the first target in the query's list drives hit@k (a target within the top k) and MRR
    (1/rank, 0 when no target appears) — rewarding methods that surface an event-mate early."""
    ranks: list[int | None] = []
    for members in groups:
        for query in members:
            targets = set(members) - {query}
            rank = next(
                (pos for pos, (idx, _) in enumerate(neighbors[query], start=1) if idx in targets),
                None,
            )
            ranks.append(rank)
    if not ranks:
        return EvalResult(queries=0, hit_at=dict.fromkeys(ks, 0.0), mrr=0.0)
    hit_at = {k: sum(1 for r in ranks if r is not None and r <= k) / len(ranks) for k in ks}
    mrr = sum(1.0 / r for r in ranks if r is not None) / len(ranks)
    return EvalResult(queries=len(ranks), hit_at=hit_at, mrr=mrr)


def format_table(results: dict[str, EvalResult], ks: tuple[int, ...]) -> str:
    header = "".join(["method".ljust(28)] + [f"hit@{k}".rjust(9) for k in ks] + ["MRR".rjust(9)])
    lines = [header]
    for name, result in results.items():
        cells = [f"{result.hit_at[k]:.3f}".rjust(9) for k in ks] + [f"{result.mrr:.3f}".rjust(9)]
        lines.append("".join([name.ljust(28)] + cells))
    return "\n".join(lines)


def _load_corpus() -> tuple[list[str], list[str | None]]:
    # Same text recipe as production analytics; recalls whose composed text is empty are excluded
    # (they get no vector under either method). The heavy `raw` JSONB is never loaded.
    session = SessionLocal()
    try:
        recalls = session.scalars(
            select(Recall)
            .options(
                load_only(
                    Recall.event_id,
                    Recall.reason_text,
                    Recall.product_description,
                    Recall.company_name,
                )
            )
            .where(Recall.country == _COUNTRY)
            .order_by(Recall.source, Recall.recall_number)
        ).all()
    finally:
        session.close()
    texts: list[str] = []
    event_ids: list[str | None] = []
    for recall in recalls:
        text = _compose_text(recall.reason_text, recall.product_description, recall.company_name)
        if text:
            texts.append(text)
            event_ids.append(recall.event_id)
    return texts, event_ids


def _write_card(
    corpus_size: int, groups: list[list[int]], table: str, model_name: str, n_neighbors: int
) -> None:
    card = f"""# Recall similarity — TF-IDF vs static embeddings

**Question:** would neural embeddings beat the shipped TF-IDF at ranking related recalls, on this
corpus? Candidate: Model2Vec `{model_name}` (static token embeddings, numpy-only inference —
chosen because the API host is 0.1 CPU / 512 MB; revisit a transformer on a hosting upgrade).

**Ground truth:** openFDA `event_id` groups recalls from the same real-world event. Each recall in
a multi-recall event queries its top-{n_neighbors} neighbours; hit@k asks whether an event-mate
appears in the top k, MRR rewards it appearing early. US corpus: {corpus_size} recalls with usable
text, {len(groups)} events with ≥2 recalls, {sum(len(g) for g in groups)} queries.

```
{table}
```

**Honest limits:** same-event recalls often share near-verbatim reason text, so absolute scores
flatter both methods — the gap is the signal. Ground truth is US-only (other sources publish no
event grouping), a same-event miss can still be a reasonable neighbour, and cross-event neighbour
quality is not judged. Generated by `scripts/eval_embeddings.py`.
"""
    (MODEL_PATH.parent / "embedding_eval_card.md").write_text(card)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare TF-IDF vs Model2Vec recall similarity.")
    parser.add_argument("--model", default=MODEL_NAME, help="Model2Vec model to evaluate")
    parser.add_argument(
        "--neighbors", type=int, default=_DEFAULT_NEIGHBORS, help="neighbours kept per recall"
    )
    args = parser.parse_args()
    ks = tuple(k for k in _HIT_KS if k <= args.neighbors)

    texts, event_ids = _load_corpus()
    groups = event_groups(event_ids)
    if not groups:
        raise SystemExit("No multi-recall FDA events found — ingest/backfill the US corpus first.")
    print(
        f"Corpus: {len(texts)} US recalls with usable text; {len(groups)} events with >=2 "
        f"recalls -> {sum(len(g) for g in groups)} queries"
    )

    vectorizer = _make_vectorizer(len(texts), _corpus_min_df(len(texts)))
    tfidf_neighbors = _compute_neighbors(vectorizer.fit_transform(texts), args.neighbors)
    tfidf = evaluate_neighbors(tfidf_neighbors, groups, ks)
    embedding_neighbors = _compute_neighbors(embed_texts(texts, args.model), args.neighbors)
    embedded = evaluate_neighbors(embedding_neighbors, groups, ks)

    model_label = f"{args.model} ({'shipped' if args.model == MODEL_NAME else 'candidate'})"
    table = format_table({"tf-idf (previous)": tfidf, model_label: embedded}, ks)
    print(table)
    _write_card(len(texts), groups, table, args.model, args.neighbors)
    print(f"Wrote {MODEL_PATH.parent / 'embedding_eval_card.md'}")


if __name__ == "__main__":
    main()
