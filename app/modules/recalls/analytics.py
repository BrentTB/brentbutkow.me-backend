"""Offline analytics over the recall corpus — themes (NMF topics) + similarity (cosine neighbours).

One shared TF-IDF matrix over each recall's reason + product text powers both: NMF factorises it
into interpretable topics (each labelled by its top terms — no model, no LLM at read time), and the
L2-normalised rows give cosine similarity for "related recalls". Both are precomputed by
`scripts/build_analytics.py` into the `recall_topics` / `recall_neighbors` tables and the
`recalls.topic_id` column, so serving is plain indexed reads — sklearn is never imported by the app.

Pure compute lives in `build_analytics`; `rebuild_analytics` does the DB I/O. Deterministic
(`random_state=42`) so a rebuild on unchanged data reproduces the same topics and neighbours.
"""

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session, load_only

from app.modules.recalls.models import Recall, RecallNeighbor, RecallTopic

# TF-IDF — mirrors the category classifier's vectoriser so the text representation is consistent.
_NGRAM = (1, 2)
_MIN_DF = 2
_MAX_FEATURES = 20_000

# NMF themes: how many topics to factor into, how many terms describe each, and the solver budget.
_N_TOPICS = 16
_N_TERMS = 8
_MAX_ITER = 400

# Similarity: nearest neighbours kept per recall.
_N_NEIGHBORS = 8

# Below this many usable documents there's nothing meaningful to factor or compare.
_MIN_DOCS = 3

# Rows per executemany when rewriting the neighbour table.
_DB_CHUNK = 1000


@dataclass
class TopicInfo:
    id: int
    label: str
    top_terms: list[str]
    size: int


@dataclass
class AnalyticsResult:
    # Aligned 1:1 with the input `texts`: the topic each doc belongs to (None if it has no usable
    # text), and its ranked nearest neighbours as (index-into-texts, cosine score) pairs.
    topic_ids: list[int | None]
    neighbors: list[list[tuple[int, float]]]
    topics: list[TopicInfo] = field(default_factory=list)


def _compute_neighbors(matrix: csr_matrix, n_neighbors: int) -> list[list[tuple[int, float]]]:
    # Rows are L2-normalised, so a dot product is cosine similarity. Done in row-blocks to bound
    # peak memory at (block x n) instead of the full (n x n) similarity matrix.
    count = matrix.shape[0]
    out: list[list[tuple[int, float]]] = [[] for _ in range(count)]
    if count < 2:
        return out
    keep = min(n_neighbors, count - 1)
    block = 512
    transposed = matrix.T
    for start in range(0, count, block):
        sims = (matrix[start : start + block] @ transposed).toarray()
        for offset in range(sims.shape[0]):
            index = start + offset
            row = sims[offset]
            row[index] = -1.0  # never return the doc itself
            top = np.argpartition(row, -keep)[-keep:]
            top = top[np.argsort(row[top])[::-1]]
            out[index] = [(int(j), float(row[j])) for j in top if row[j] > 0.0]
    return out


def build_analytics(
    texts: list[str],
    *,
    n_topics: int = _N_TOPICS,
    n_terms: int = _N_TERMS,
    n_neighbors: int = _N_NEIGHBORS,
    min_df: int = _MIN_DF,
) -> AnalyticsResult:
    """Factor `texts` into topics + nearest neighbours. Docs with no usable text (and the whole
    corpus when it's too small or all stop-words) get no topic and no neighbours, never an error."""
    topic_ids: list[int | None] = [None] * len(texts)
    neighbors: list[list[tuple[int, float]]] = [[] for _ in texts]

    nonempty = [i for i, text in enumerate(texts) if text and text.strip()]
    if len(nonempty) < _MIN_DOCS:
        return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors)

    corpus = [texts[i] for i in nonempty]
    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=_NGRAM,
        min_df=min_df,
        max_features=_MAX_FEATURES,
        stop_words="english",
    )
    matrix = vectorizer.fit_transform(corpus)
    if matrix.shape[1] == 0:  # vocabulary emptied by min_df / stop-words
        return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors)

    topic_count = max(1, min(n_topics, matrix.shape[0], matrix.shape[1]))
    model = NMF(n_components=topic_count, init="nndsvda", random_state=42, max_iter=_MAX_ITER)
    weights = model.fit_transform(matrix)
    features = vectorizer.get_feature_names_out()

    assignments = weights.argmax(axis=1)
    rowsums = weights.sum(axis=1)
    for position, original in enumerate(nonempty):
        if rowsums[position] > 0:  # an all-zero row has no real topic
            topic_ids[original] = int(assignments[position])

    sizes = Counter(topic for topic in topic_ids if topic is not None)
    topics = []
    for component in range(topic_count):
        order = np.argsort(model.components_[component])[::-1][:n_terms]
        terms = [str(features[j]) for j in order]
        topics.append(
            TopicInfo(
                id=component,
                label=" · ".join(terms[:3]),
                top_terms=terms,
                size=int(sizes.get(component, 0)),
            )
        )

    corpus_neighbors = _compute_neighbors(matrix, n_neighbors)
    for position, original in enumerate(nonempty):
        neighbors[original] = [(nonempty[j], score) for j, score in corpus_neighbors[position]]
    return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors, topics=topics)


def rebuild_analytics(session: Session) -> dict[str, int]:
    """Recompute topics + neighbours over every stored recall and replace the materialised tables.
    Called by scripts/build_analytics.py after ingest. Whole thing is one transaction."""
    # Only the text columns feed the matrix (the PK loads automatically, and topic_id is written
    # back, not read) — so skip the heavy `raw` JSONB to bound memory over the whole corpus.
    recalls = list(
        session.scalars(
            select(Recall)
            .options(load_only(Recall.reason_text, Recall.product_description))
            .order_by(Recall.source, Recall.recall_number)
        ).all()
    )
    texts = [f"{r.reason_text or ''} {r.product_description or ''}".strip() for r in recalls]
    result = build_analytics(texts)

    for recall, topic_id in zip(recalls, result.topic_ids, strict=True):
        recall.topic_id = topic_id

    session.execute(delete(RecallNeighbor))
    session.execute(delete(RecallTopic))
    session.flush()
    if result.topics:
        session.execute(
            insert(RecallTopic),
            [
                {"id": t.id, "label": t.label, "top_terms": t.top_terms, "size": t.size}
                for t in result.topics
            ],
        )

    neighbor_rows: list[dict[str, object]] = []
    for recall, nbrs in zip(recalls, result.neighbors, strict=True):
        for rank, (index, score) in enumerate(nbrs, start=1):
            neighbor = recalls[index]
            neighbor_rows.append(
                {
                    "source": recall.source,
                    "recall_number": recall.recall_number,
                    "rank": rank,
                    "neighbor_source": neighbor.source,
                    "neighbor_number": neighbor.recall_number,
                    "score": round(float(score), 4),
                }
            )
    for start in range(0, len(neighbor_rows), _DB_CHUNK):
        session.execute(insert(RecallNeighbor), neighbor_rows[start : start + _DB_CHUNK])

    session.commit()
    return {"recalls": len(recalls), "topics": len(result.topics), "neighbors": len(neighbor_rows)}
