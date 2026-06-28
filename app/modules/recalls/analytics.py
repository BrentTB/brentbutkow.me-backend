"""Offline analytics over the recall corpus — themes (NMF topics) + similarity (cosine neighbours).

One shared TF-IDF matrix over each recall's reason (weighted ~2×) + a heavily-stripped product
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
from typing import cast

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sqlalchemy import Table, bindparam, delete, insert, select, update
from sqlalchemy.orm import Session, load_only

from app.modules.recalls.models import (
    Recall,
    RecallAnalyticsBuild,
    RecallNeighbor,
    RecallTopic,
)

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

# Reason weighted 2:1 over the product description (reason counted 2× product). TF-IDF is
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
# Risk-statement / symptom / affected-population boilerplate. UK FSA alerts are templated ("makes it
# unsafe to eat", "possible health risk", "symptoms ... fever, diarrhoea", "people with weakened
# immune systems"), which buries the hazard; plus generic US filler, titles, and a couple of brand
# tokens that slip past company-name stripping. Hazard/allergen/food words are kept on purpose.
_BOILERPLATE_STOP = {
    # generic risk-statement filler
    "risk",
    "possible",
    "health",
    "make",
    "makes",
    "making",
    "made",
    "unsafe",
    "eat",
    "edible",
    "presence",
    "present",
    "presents",
    "listed",
    "cause",
    "caused",
    "usually",
    "safety",
    "contains",
    "contain",
    "containing",
    "constituents",
    "allergy",
    "allergies",
    "intolerance",
    "potential",
    "potentially",
    "packaged",
    "ingredient",
    "ingredients",
    "recalled",
    "firm",
    "premium",
    "select",
    "high",
    # symptoms — generic across pathogens, so they swamp the pathogen name
    "symptoms",
    "symptom",
    "diarrhoea",
    "diarrhea",
    "abdominal",
    "cramps",
    "fever",
    "pain",
    "ache",
    "aches",
    "temperature",
    "muscle",
    "vomiting",
    "nausea",
    "sickness",
    "headache",
    # affected populations / vulnerability
    "people",
    "person",
    "babies",
    "baby",
    "pregnant",
    "women",
    "woman",
    "men",
    "immune",
    "weakened",
    "elderly",
    "old",
    "age",
    "aged",
    "ages",
    "vulnerable",
    "systems",
    "system",
    "children",
    "child",
    # titles + brand tokens that slip past company-name stripping
    "mr",
    "mrs",
    "ms",
    "dr",
    "st",
    "vikki",
    "loard",
}
_STOP = list(ENGLISH_STOP_WORDS | _DOMAIN_STOP | _BOILERPLATE_STOP)

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

# Topics need a real corpus to be trustworthy: below this the max_df filter is off (one-off brand
# tokens then dominate the themes), so for a low-volume country like South Africa we skip NMF and
# surface no themes; neighbours still build (they degrade gracefully). rebuild_analytics applies
# this per country, while build_analytics defaults lower so small test corpora keep their topics.
_MIN_TOPIC_CORPUS = 200


def _strip_company(text: str, company: str | None) -> str:
    # Drop the recalling firm's name tokens so a big manufacturer can't form its own "theme".
    if not company:
        return text
    names = {word for word in re.findall(r"[a-zA-Z]+", company.lower()) if len(word) > 2}
    return " ".join(word for word in text.split() if word.lower() not in names)


def _compose_text(reason: str, product: str, company: str | None) -> str:
    reason_text = _strip_company(reason, company)
    product_text = _strip_company(product, company)
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


def _norm_token(token: str) -> str:
    # Light singularising for de-duplication only (peanut/peanuts, egg/eggs) — not real stemming.
    # Guard short words and double-s endings so "less"/"glass"/"gas" survive intact.
    if len(token) >= 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _top_terms(features: np.ndarray, weights: np.ndarray, n_terms: int) -> list[str]:
    """Rank a topic's terms, dropping redundant ones so no word repeats in the label.

    The reason field is up-weighted by repetition (see `_compose_text`), which manufactures
    pure-repetition bigrams ("egg egg") and lets one word surface both as a unigram and inside a
    bigram ("salmonella" + "salmonella contamination"). Both read as noise. Walk the ranked terms
    and keep one only if every word in it (singularised) is new to the label so far."""
    chosen: list[str] = []
    used: set[str] = set()
    for index in np.argsort(weights)[::-1]:
        if weights[index] <= 0:  # NMF weights are non-negative; nothing useful past zero
            break
        term = str(features[index])
        tokens = [_norm_token(t) for t in term.split()]
        if len(tokens) == 2 and tokens[0] == tokens[1]:
            continue  # "egg egg" — pure repetition artifact of the reason up-weighting
        if any(token in used for token in tokens):
            continue  # a word already shown (as a unigram or inside an earlier term)
        chosen.append(term)
        used.update(tokens)
        if len(chosen) >= n_terms:
            break
    return chosen


def build_analytics(
    texts: list[str],
    *,
    n_topics: int = _N_TOPICS,
    n_terms: int = _N_TERMS,
    n_neighbors: int = _N_NEIGHBORS,
    min_df: int = _MIN_DF,
    min_topic_docs: int = _MIN_DOCS,
) -> AnalyticsResult:
    """Factor `texts` into topics + nearest neighbours. Docs with no usable text (and the whole
    corpus when it's too small or all stop-words) get no topic and no neighbours, never an error.

    Topics are only factored when the corpus has at least `min_topic_docs` usable documents; below
    that NMF themes are one-off brand noise, so no topics are produced while neighbours still build
    (they degrade gracefully). The caller sets the floor — `rebuild_analytics` uses a high one per
    country; the default stays low so small corpora (tests, the request path) keep their topics."""
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

    # Themes only on a large-enough corpus; below the floor NMF would surface one-off brand noise,
    # so skip it (no topics) — the neighbours below still build for the same matrix.
    topics: list[TopicInfo] = []
    if len(corpus) >= min_topic_docs:
        topic_count = max(1, min(n_topics, matrix.shape[0], matrix.shape[1]))
        model = NMF(n_components=topic_count, init="nndsvda", random_state=42, max_iter=_MAX_ITER)
        weights = model.fit_transform(matrix)
        features = vectorizer.get_feature_names_out()

        assignments = weights.argmax(axis=1)
        rowsums = weights.sum(axis=1)
        topic_terms = [
            _top_terms(features, model.components_[component], n_terms)
            for component in range(topic_count)
        ]

        for position, original in enumerate(nonempty):
            if rowsums[position] <= 0:
                continue  # an all-zero row has no real topic
            component = int(assignments[position])
            # Only keep the assignment when the recall actually contains one of the topic's *label*
            # terms. NMF's argmax otherwise files low-signal recalls (e.g. a kombucha bottle-cap
            # recall) under whichever topic loads least-badly, giving a "curry · chicken · powder"
            # chip unconnected to the recall. No match → no theme, rather than a misleading one.
            label = topic_terms[component][:3]
            if any(term in corpus[position].lower() for term in label):
                topic_ids[original] = component

        sizes = Counter(topic for topic in topic_ids if topic is not None)
        topics = [
            TopicInfo(
                id=component,
                label=" · ".join(topic_terms[component][:3]),
                top_terms=topic_terms[component],
                size=int(sizes.get(component, 0)),
            )
            for component in range(topic_count)
        ]

    corpus_neighbors = _compute_neighbors(matrix, n_neighbors)
    for position, original in enumerate(nonempty):
        neighbors[original] = [(nonempty[j], score) for j, score in corpus_neighbors[position]]
    return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors, topics=topics)


def _slugify(label: str) -> str:
    # Stable, readable URL key from the terms, e.g. "Listeria · deli" → "listeria-deli".
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _unique_slug(label: str, topic_id: int, seen: set[str]) -> str:
    # Disambiguate the rare case where two topics in one country share a term-slug.
    base = _slugify(label) or f"theme-{topic_id}"
    slug, suffix = base, 2
    while slug in seen:
        slug, suffix = f"{base}-{suffix}", suffix + 1
    seen.add(slug)
    return slug


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

    session.execute(delete(RecallNeighbor))
    session.execute(delete(RecallTopic))
    session.flush()

    topic_rows: list[dict[str, object]] = []
    neighbor_rows: list[dict[str, object]] = []
    # Collect each recall's new topic id and write them in one bulk UPDATE at the end (below) rather
    # than mutating the ORM rows — topic_id is a derived field, so its write must NOT bump
    # recalls.updated_at (the "source data changed" signal status() reads to decide staleness).
    topic_ids: list[dict[str, object]] = []
    next_topic_id = 0  # surrogate ids, unique across countries so recalls.topic_id stays one int
    for country in sorted(by_country):
        group = by_country[country]
        texts = [_compose_text(r.reason_text, r.product_description, r.company_name) for r in group]
        # Shed one-off brand tokens on a real corpus; stay lenient so a small one still clusters.
        min_df = _MIN_DF_LARGE if len(group) >= _LARGE_CORPUS else _MIN_DF
        # Skip themes for a low-volume country (no topics, neighbours still build); US/UK clear it.
        result = build_analytics(texts, min_df=min_df, min_topic_docs=_MIN_TOPIC_CORPUS)

        local_to_global: dict[int, int] = {}
        seen_slugs: set[str] = set()
        for topic in result.topics:
            local_to_global[topic.id] = next_topic_id
            topic_rows.append(
                {
                    "id": next_topic_id,
                    "country": country,
                    "slug": _unique_slug(topic.label, next_topic_id, seen_slugs),
                    "label": topic.label,
                    "top_terms": topic.top_terms,
                    "size": topic.size,
                }
            )
            next_topic_id += 1

        for recall, topic_id in zip(group, result.topic_ids, strict=True):
            topic_ids.append(
                {
                    "b_source": recall.source,
                    "b_number": recall.recall_number,
                    "b_topic": local_to_global[topic_id] if topic_id is not None else None,
                }
            )

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

    # Write topic_id while preserving updated_at: setting updated_at to itself keeps the column in
    # the UPDATE's SET clause, so the onupdate=func.now() default doesn't fire and a topic-id-only
    # change can't masquerade as a source change to status(). A Core table UPDATE (not the ORM
    # update(Recall)) keeps this a plain executemany, not an ORM bulk-update-by-primary-key.
    recall_table = cast(Table, Recall.__table__)
    for start in range(0, len(topic_ids), _DB_CHUNK):
        session.execute(
            update(recall_table)
            .where(recall_table.c.source == bindparam("b_source"))
            .where(recall_table.c.recall_number == bindparam("b_number"))
            .values(topic_id=bindparam("b_topic"), updated_at=recall_table.c.updated_at),
            topic_ids[start : start + _DB_CHUNK],
        )

    # Stamp the build marker from the DB clock (server_default=func.now()). Because the writes above
    # leave updated_at untouched, the newest updated_at reflects only real source changes — all of
    # which predate this build — so status()'s `max(updated_at) > built_at` can't false-flag.
    session.add(RecallAnalyticsBuild())
    session.commit()
    # The Core bulk UPDATE above bypassed the identity map, so the loaded recalls still hold their
    # old topic_id in memory; expire them so any later read reloads from the DB.
    session.expire_all()
    return {"recalls": len(recalls), "topics": len(topic_rows), "neighbors": len(neighbor_rows)}
