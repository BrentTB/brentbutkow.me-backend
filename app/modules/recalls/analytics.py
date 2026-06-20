"""Offline analytics over the recall corpus — themes (NMF topics) + similarity (cosine neighbours).

One shared TF-IDF matrix over each recall's reason (weighted ~1.5×) + a heavily-stripped product
description powers both: NMF factorises it into interpretable topics (each labelled by its top terms
— no model, no LLM at read time), and the L2-normalised rows give cosine similarity for "related
recalls". Packaging boilerplate, numbers/dates, and company names are dropped so themes land on
hazards and foods, not "net wt oz" or brand names. Both are precomputed by
`scripts/build_analytics.py` into the `recall_topics` / `recall_neighbors` tables and the
`recalls.topic_id` column, so serving is plain indexed reads — sklearn is never imported by the app.

Pure compute lives in `build_analytics`; `rebuild_analytics` does the DB I/O. Deterministic
(`random_state=42`) so a rebuild on unchanged data reproduces the same topics and neighbours.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
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

# Reason weighted 2:1 over the product description (≈ reason counted 1.5× product). TF-IDF is
# count-based, so repeating a field scales its term frequencies; document frequency (IDF) is
# unchanged. Reason is the cause signal; product adds a little food context.
_REASON_WEIGHT = 2
_PRODUCT_WEIGHT = 1

# Packaging / quantity / legal-entity / format boilerplate with no bearing on the recall cause.
_DOMAIN_STOP = {
    "oz",
    "ozs",
    "lb",
    "lbs",
    "fl",
    "net",
    "wt",
    "weight",
    "upc",
    "ct",
    "count",
    "pack",
    "packed",
    "package",
    "packages",
    "packaging",
    "inc",
    "llc",
    "co",
    "ltd",
    "corp",
    "company",
    "brand",
    "brands",
    "product",
    "products",
    "item",
    "items",
    "lot",
    "lots",
    "code",
    "codes",
    "approx",
    "approximately",
    "kg",
    "kgs",
    "mg",
    "ml",
    "case",
    "cases",
    "bag",
    "bags",
    "box",
    "boxes",
    "bottle",
    "bottles",
    "jar",
    "jars",
    "can",
    "cans",
    "container",
    "containers",
    "size",
    "label",
    "labels",
    "best",
    "sell",
    "use",
    "date",
    "dates",
    "exp",
    "expiration",
    "manufactured",
    "distributed",
    "sold",
    "retail",
    "store",
    "stores",
    "number",
    "numbers",
    "description",
    "reads",
    "part",
    "include",
    "including",
    "various",
    "located",
    "marked",
    "printed",
    "master",
    "flexible",
    "individually",
    "wrapped",
    "sealed",
    "vacuum",
    "tray",
    "trays",
    "carton",
    "cartons",
    "pouch",
    "pouches",
    "sleeve",
    "sleeves",
    "clamshell",
    "film",
    "foodservice",
    "ready",
    "frozen",
    "refrigerated",
    "shelf",
    "stable",
    "variety",
    "assorted",
    "original",
    "classic",
}
_STOP = list(ENGLISH_STOP_WORDS | _DOMAIN_STOP)

# Keep only alphabetic tokens (≥2 letters) — drops pure numbers, dates, and lot/UPC codes.
_TOKEN_PATTERN = r"(?u)\b[a-zA-Z][a-zA-Z]+\b"

# max_df trims corpus-ubiquitous filler ("contains", "potential") on real corpora, but a tiny corpus
# (a test, or a new country with few recalls) would empty out — so only apply it past a doc count.
_MAX_DF = 0.3
_MAX_DF_MIN_DOCS = 200

# A term must appear in this many docs to count — higher on real corpora to shed one-off brand
# tokens, lenient on small ones so a few-doc corpus still clusters.
_MIN_DF_LARGE = 5
_LARGE_CORPUS = 500


def _strip_company(text: str, company: str | None) -> str:
    # Drop the recalling firm's name tokens so a big manufacturer can't form its own "theme".
    if not company:
        return text
    names = {word for word in re.findall(r"[a-zA-Z]+", company.lower()) if len(word) > 2}
    return " ".join(word for word in text.split() if word.lower() not in names)


def _compose_text(reason: str | None, product: str | None, company: str | None) -> str:
    reason_text = _strip_company(reason or "", company)
    product_text = _strip_company(product or "", company)
    return " ".join([reason_text] * _REASON_WEIGHT + [product_text] * _PRODUCT_WEIGHT).strip()


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
    # Skip the corpus-frequency cap on tiny corpora (every shared term is a large fraction there).
    max_df = _MAX_DF if len(corpus) >= _MAX_DF_MIN_DOCS else 1.0
    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=_NGRAM,
        min_df=min_df,
        max_df=max_df,
        max_features=_MAX_FEATURES,
        stop_words=_STOP,
        token_pattern=_TOKEN_PATTERN,
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
    """Recompute topics + neighbours and replace the materialised tables. Themes are computed **per
    country** (US and UK recall structures differ, and the dashboard is country-scoped), and
    similarity stays within a country too. Called by scripts/build_analytics.py. One transaction."""
    # Only the text + country columns feed the work (PKs load automatically; topic_id is written
    # back, not read) — so skip the heavy `raw` JSONB to bound memory over the whole corpus.
    recalls = list(
        session.scalars(
            select(Recall)
            .options(
                load_only(
                    Recall.country,
                    Recall.reason_text,
                    Recall.product_description,
                    Recall.company_name,
                )
            )
            .order_by(Recall.country, Recall.source, Recall.recall_number)
        ).all()
    )
    # Group by country, preserving the deterministic order above.
    by_country: dict[str, list[Recall]] = {}
    for recall in recalls:
        by_country.setdefault(recall.country, []).append(recall)

    for recall in recalls:
        recall.topic_id = None
    session.execute(delete(RecallNeighbor))
    session.execute(delete(RecallTopic))
    session.flush()

    topic_rows: list[dict[str, object]] = []
    neighbor_rows: list[dict[str, object]] = []
    next_topic_id = 0  # surrogate ids, unique across countries so recalls.topic_id stays one int
    for country in sorted(by_country):
        group = by_country[country]
        texts = [_compose_text(r.reason_text, r.product_description, r.company_name) for r in group]
        # Shed one-off brand tokens on a real corpus; stay lenient so a small one still clusters.
        min_df = _MIN_DF_LARGE if len(group) >= _LARGE_CORPUS else _MIN_DF
        result = build_analytics(texts, min_df=min_df)

        local_to_global: dict[int, int] = {}
        for topic in result.topics:
            local_to_global[topic.id] = next_topic_id
            topic_rows.append(
                {
                    "id": next_topic_id,
                    "country": country,
                    "label": topic.label,
                    "top_terms": topic.top_terms,
                    "size": topic.size,
                }
            )
            next_topic_id += 1

        for recall, topic_id in zip(group, result.topic_ids, strict=True):
            recall.topic_id = local_to_global[topic_id] if topic_id is not None else None

        for recall, nbrs in zip(group, result.neighbors, strict=True):
            for rank, (index, score) in enumerate(nbrs, start=1):
                neighbor = group[index]
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

    if topic_rows:
        session.execute(insert(RecallTopic), topic_rows)
    for start in range(0, len(neighbor_rows), _DB_CHUNK):
        session.execute(insert(RecallNeighbor), neighbor_rows[start : start + _DB_CHUNK])

    session.commit()
    return {"recalls": len(recalls), "topics": len(topic_rows), "neighbors": len(neighbor_rows)}
