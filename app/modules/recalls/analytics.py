"""Offline analytics over the recall corpus — themes (k-means over neural embeddings) + similarity
(cosine neighbours in embedding space).

Each recall's reason + a heavily-stripped product description is embedded with Model2Vec
`potion-base-8M` (app/modules/recalls/embeddings.py). The L2-normalised rows power both features:
k-means clusters them into themes — membership is semantic closeness to the theme's centroid, gated
so a recall gets *no* theme rather than a misleading one — and dot products give cosine similarity
for "related recalls". The two features weight the text differently (see the weight constants):
neighbours keep product context (a recall is "like" another by cause *and* product), while themes
up-weight the reason so a long product blurb can't drag a short cause into a generic product
cluster. Theme labels come from TF-IDF terms over each cluster's members (c-TF-IDF style), so the
curated stop-word lists keep names on hazards and foods, not "net wt oz" or brand names. The
neighbour representation is benchmarked against the previous pure-TF-IDF build on FDA event_id
ground truth (scripts/eval_embeddings.py): hit@1 0.966 vs 0.942. Both features are precomputed by
`scripts/build_analytics.py` into the `recall_topics` / `recall_neighbors` tables and the
`recalls.topic_id` column, so serving is plain indexed reads — neither sklearn nor the embedding
model is ever imported by the app.

Pure compute lives in `build_analytics` (texts + embedding rows in; no model, no DB — testable
with synthetic vectors); `rebuild_analytics` embeds each country's corpus and does the DB I/O.
Deterministic (`random_state=42`) so a rebuild on unchanged data reproduces the same topics and
neighbours.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import cast

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sqlalchemy import Table, bindparam, delete, insert, select, update
from sqlalchemy.orm import Session, load_only

from app.modules.recalls.embeddings import embed_texts
from app.modules.recalls.models import (
    Recall,
    RecallAnalyticsBuild,
    RecallNeighbor,
    RecallTopic,
)

# TF-IDF — used only to *label* clusters; mirrors the category classifier's vectoriser.
_NGRAM = (1, 2)
_MIN_DF = 2
_MAX_FEATURES = 20_000

# Themes: how many k-means clusters, how many label terms each, and the restart budget (n_init
# k-means++ seedings; the best inertia wins — deterministic under the fixed random_state).
_N_TOPICS = 16
_N_TERMS = 8
_KMEANS_N_INIT = 10

# A recall keeps its theme only when its embedding's cosine to the cluster centroid clears this —
# below it, no theme rather than a misleading one. Calibrated on the real corpus (Jul 2026):
# 0.55 covers ~93% of US / ~95% of UK / ~90% of CA recalls, above the old keyword-gated NMF
# coverage while dropping the worst-fitting tail.
_MIN_TOPIC_SIM = 0.55

# Similarity: nearest neighbours kept per recall. 6 = the similar-recalls endpoint's cap (its
# `limit` Query is bounded at 6, so it can never ask for more than are stored) and comfortably above
# _NOVELTY_K; trimmed from 8 because the neighbour table is derived storage that grows K× faster
# than the corpus.
_N_NEIGHBORS = 6  # keep in sync with the /similar `limit` Query ceiling in router.py

# Novelty — 1 − the mean cosine of a recall's top-_NOVELTY_K neighbours, missing slots floored at
# cosine 0. A recall whose nearest neighbours are all far away (or which has fewer than _NOVELTY_K
# positive-cosine neighbours at all) is unlike anything else in the corpus — a first-of-its-kind
# hazard or product — and scores as maximally novel, so the "unusual recalls" feed surfaces exactly
# these. Scored only once the corpus holds at least this many other recalls to compare against.
_NOVELTY_K = 3

# Below this many usable documents there's nothing meaningful to factor or compare.
_MIN_DOCS = 3

# Rows per executemany when rewriting the neighbour table.
_DB_CHUNK = 1000

# Reason is up-weighted over the product description by repetition. Repetition scales both
# representations the same way: TF-IDF term frequencies (IDF unchanged) and the token-mean embedding
# (reason tokens carry proportionally more mass). Reason is the cause signal; product adds food
# context. Two weightings, because themes and neighbours answer different questions:
#   * NEIGHBOURS / events (2:1) — "what recall is like this one?" wants product in the mix (two
#     recalls of the same product line are related); this is the ratio benchmarked in
#     scripts/eval_embeddings.py against FDA event_id.
#   * THEMES (4:1) — "what is the cause?" A long product blurb (packaging, weights, origin)
#     otherwise drowns a short cause ("Undeclared fish.") and drags the recall into a generic
#     product cluster; up-weighting reason lands it on its actual cause. Product is kept (not
#     dropped) so a theme can still lean on food context where the reason is sparse.
_REASON_WEIGHT = 2
_PRODUCT_WEIGHT = 1
_THEME_REASON_WEIGHT = 4
_THEME_PRODUCT_WEIGHT = 1

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
# Non-English function words. RASFF subjects are English but product names ride along in their
# native language ("foglie di curry", "en producten"), and sklearn's stop list is English-only —
# which let "di · en · alkaloids" ship as an EU theme label. High-frequency FR/IT/ES/PT/NL/DE/PL
# articles, prepositions and conjunctions only; anything that doubles as a food/hazard term in
# English (e.g. "dal" the lentil) stays out. Single letters never tokenize (_TOKEN_PATTERN ≥2).
_FOREIGN_STOP = {
    # French
    "le",
    "la",
    "les",
    "des",
    "du",
    "au",
    "aux",
    "et",
    "ou",
    "pour",
    "sur",
    "sous",
    "avec",
    "sans",
    "un",
    "une",
    # Italian
    "di",
    "del",
    "della",
    "delle",
    "dei",
    "degli",
    "dello",
    "il",
    "lo",
    "gli",
    "ed",
    "con",
    "senza",
    "alla",
    "alle",
    "dalla",
    "tra",
    "fra",
    # Spanish
    "el",
    "los",
    "las",
    "uno",
    "una",
    "sin",
    "para",
    "por",
    # Portuguese
    "da",
    "das",
    "dos",
    "ao",
    "aos",
    "em",
    "uma",
    # Dutch
    "en",
    "van",
    "het",
    "met",
    "voor",
    "der",
    "ter",
    "op",
    # German
    "und",
    "die",
    "den",
    "dem",
    "von",
    "zu",
    "aus",
    "bei",
    "im",
    "nach",
    # Polish
    "na",
    "od",
    "po",
    "przez",
    "ze",
    "bez",
}
_STOP = list(ENGLISH_STOP_WORDS | _DOMAIN_STOP | _BOILERPLATE_STOP | _FOREIGN_STOP)

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
# tokens then dominate the themes), so for a low-volume country like South Africa we skip
# clustering and surface no themes; neighbours still build (they degrade gracefully).
# rebuild_analytics applies
# this per country, while build_analytics defaults lower so small test corpora keep their topics.
_MIN_TOPIC_CORPUS = 200


def _corpus_min_df(doc_count: int) -> int:
    # Shed one-off brand tokens on a real corpus; stay lenient so a small one still clusters.
    return _MIN_DF_LARGE if doc_count >= _LARGE_CORPUS else _MIN_DF


def _make_vectorizer(doc_count: int, min_df: int = _MIN_DF) -> TfidfVectorizer:
    # The one place the TF-IDF text representation is defined, so offline evaluation
    # (scripts/eval_embeddings.py) compares candidates against exactly what production builds.
    # Skip the corpus-frequency cap on tiny corpora (every shared term is a large fraction there).
    max_df = _MAX_DF if doc_count >= _MAX_DF_MIN_DOCS else 1.0
    return TfidfVectorizer(
        lowercase=True,
        ngram_range=_NGRAM,
        min_df=min_df,
        max_df=max_df,
        max_features=_MAX_FEATURES,
        stop_words=_STOP,
        token_pattern=_TOKEN_PATTERN,
    )


def _strip_company(text: str, company: str | None) -> str:
    # Drop the recalling firm's name tokens so a big manufacturer can't form its own "theme".
    if not company:
        return text
    names = {word for word in re.findall(r"[a-zA-Z]+", company.lower()) if len(word) > 2}
    return " ".join(word for word in text.split() if word.lower() not in names)


def _compose_text(
    reason: str,
    product: str,
    company: str | None,
    *,
    reason_weight: int = _REASON_WEIGHT,
    product_weight: int = _PRODUCT_WEIGHT,
) -> str:
    reason_text = _strip_company(reason, company)
    product_text = _strip_company(product, company)
    return " ".join([reason_text] * reason_weight + [product_text] * product_weight).strip()


@dataclass
class TopicInfo:
    id: int
    label: str
    top_terms: list[str]
    size: int


@dataclass
class AnalyticsResult:
    # Aligned 1:1 with the input `texts`: the topic each doc belongs to (None if it has no usable
    # text), its ranked nearest neighbours as (index-into-texts, cosine score) pairs, and its
    # novelty in [0, 1] (None when it has too few neighbours to judge).
    topic_ids: list[int | None]
    neighbors: list[list[tuple[int, float]]]
    novelty: list[float | None] = field(default_factory=list)
    topics: list[TopicInfo] = field(default_factory=list)


def _novelty(neighbor_scores: list[tuple[int, float]], available: int) -> float | None:
    # Neighbours arrive sorted by descending cosine, so [:k] is the closest k. High mean cosine →
    # crowded neighbourhood → ordinary; low → isolated → novel. A recall with fewer than _NOVELTY_K
    # positive-cosine neighbours is the *most* isolated, so its missing slots floor at cosine 0
    # (maximally novel) rather than nulling the score — otherwise the "unusual recalls" feed would
    # drop exactly the recalls it exists to surface. None only when the corpus itself holds fewer
    # than _NOVELTY_K other recalls, so there is genuinely nothing to compare against.
    if available < _NOVELTY_K:
        return None
    top = [score for _, score in neighbor_scores[:_NOVELTY_K]]
    top += [0.0] * (_NOVELTY_K - len(top))
    return round(1.0 - sum(top) / _NOVELTY_K, 4)


def _compute_neighbors(
    matrix: csr_matrix | np.ndarray, n_neighbors: int
) -> list[list[tuple[int, float]]]:
    # Rows must be L2-normalised (TfidfVectorizer's default; dense embedding callers normalise
    # before calling), so a dot product is cosine similarity. Done in row-blocks to bound peak
    # memory at (block x n) instead of the full (n x n) similarity matrix.
    count = matrix.shape[0]
    out: list[list[tuple[int, float]]] = [[] for _ in range(count)]
    if count < 2:
        return out
    keep = min(n_neighbors, count - 1)
    block = 512
    transposed = matrix.T
    for start in range(0, count, block):
        products = matrix[start : start + block] @ transposed
        sims = products if isinstance(products, np.ndarray) else products.toarray()
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


def _cluster_topics(
    corpus: list[str],
    vectors: np.ndarray,
    *,
    n_topics: int,
    n_terms: int,
    min_df: int,
    min_topic_sim: float,
) -> tuple[list[int | None], list[TopicInfo]] | None:
    """K-means themes in embedding space, each labelled by its members' distinguishing TF-IDF terms.

    Membership is semantic: a recall belongs to the cluster whose centroid its embedding is nearest,
    and keeps the assignment only when that cosine clears `min_topic_sim` — below it the recall gets
    no theme rather than a misleading one (the successor to the old NMF keyword-overlap gate, which
    let one shared label word claim a recall). TF-IDF is used only to *name* clusters — terms ranked
    by their mean weight over the cluster's members (c-TF-IDF style), so the curated stop-word lists
    keep labels on hazards and foods. Returns None when the label vocabulary is empty (min_df /
    stop-words ate everything) — unnameable themes are worse than none."""
    vectorizer = _make_vectorizer(len(corpus), min_df)
    matrix = vectorizer.fit_transform(corpus)
    if matrix.shape[1] == 0:
        return None
    features = vectorizer.get_feature_names_out()

    topic_count = max(1, min(n_topics, len(corpus)))
    model = KMeans(n_clusters=topic_count, n_init=_KMEANS_N_INIT, random_state=42)
    labels = model.fit_predict(vectors)
    # Cosine to the (re-normalised) centroid gates the assignment; the vectors themselves are
    # already unit-length, so this is a plain row-wise dot product.
    centroids = model.cluster_centers_
    centroids = centroids / np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-12)
    sims = np.einsum("ij,ij->i", vectors.astype(np.float64), centroids[labels])

    assignments = [
        int(label) if sim >= min_topic_sim else None
        for label, sim in zip(labels, sims, strict=True)
    ]
    sizes = Counter(assignment for assignment in assignments if assignment is not None)

    topics: list[TopicInfo] = []
    for component in range(topic_count):
        members = [i for i, label in enumerate(labels) if label == component]
        weights = np.asarray(matrix[members].mean(axis=0)).ravel()
        top_terms = _top_terms(features, weights, n_terms)
        topics.append(
            TopicInfo(
                id=component,
                label=" · ".join(top_terms[:3]),
                top_terms=top_terms,
                size=int(sizes.get(component, 0)),
            )
        )
    return assignments, topics


def build_analytics(
    texts: list[str],
    embeddings: np.ndarray,
    *,
    theme_texts: list[str] | None = None,
    theme_embeddings: np.ndarray | None = None,
    n_topics: int = _N_TOPICS,
    n_terms: int = _N_TERMS,
    n_neighbors: int = _N_NEIGHBORS,
    min_df: int = _MIN_DF,
    min_topic_docs: int = _MIN_DOCS,
    min_topic_sim: float = _MIN_TOPIC_SIM,
) -> AnalyticsResult:
    """Cluster the corpus into themes + nearest neighbours from its L2-normalised embedding rows.

    Neighbours use `texts`/`embeddings`; themes cluster on (and are labelled from)
    `theme_texts`/`theme_embeddings`, which default to the same. `rebuild_analytics` passes a
    reason-dominant embedding for themes (cause-focused) while keeping the product-aware one for
    neighbours — see the weight constants. All four arrays are aligned 1:1 with `texts`.

    Docs with no usable text (and the whole corpus when it's too small) get no topic and no
    neighbours, never an error. Themes are only clustered when the corpus has at least
    `min_topic_docs` usable documents; below that they'd be one-off brand noise, so no topics are
    produced while neighbours still build (they degrade gracefully). The caller sets the floor —
    `rebuild_analytics` uses a high one per country; the default stays low so small corpora (tests)
    keep their topics."""
    theme_texts = texts if theme_texts is None else theme_texts
    theme_embeddings = embeddings if theme_embeddings is None else theme_embeddings
    for name, array in (("embeddings", embeddings), ("theme_embeddings", theme_embeddings)):
        if len(texts) != array.shape[0]:
            raise ValueError(f"{len(texts)} texts but {array.shape[0]} {name} rows")
    if len(theme_texts) != len(texts):
        raise ValueError(f"{len(texts)} texts but {len(theme_texts)} theme_texts")
    topic_ids: list[int | None] = [None] * len(texts)
    neighbors: list[list[tuple[int, float]]] = [[] for _ in texts]
    novelty: list[float | None] = [None] * len(texts)

    # A recall is empty for both weightings together (both are empty only when reason and product
    # are both blank), so one nonempty index set aligns the neighbour and theme arrays alike.
    nonempty = [i for i, text in enumerate(texts) if text and text.strip()]
    if len(nonempty) < _MIN_DOCS:
        return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors, novelty=novelty)

    vectors = embeddings[nonempty]

    topics: list[TopicInfo] = []
    theme_corpus = [theme_texts[i] for i in nonempty]
    if len(theme_corpus) >= min_topic_docs:
        clustered = _cluster_topics(
            theme_corpus,
            theme_embeddings[nonempty],
            n_topics=n_topics,
            n_terms=n_terms,
            min_df=min_df,
            min_topic_sim=min_topic_sim,
        )
        if clustered is not None:
            assignments, topics = clustered
            for position, original in enumerate(nonempty):
                if assignments[position] is not None:
                    topic_ids[original] = assignments[position]

    corpus_neighbors = _compute_neighbors(vectors, n_neighbors)
    available = len(nonempty) - 1  # other recalls each doc could neighbour with
    for position, original in enumerate(nonempty):
        neighbors[original] = [(nonempty[j], score) for j, score in corpus_neighbors[position]]
        novelty[original] = _novelty(neighbors[original], available)
    return AnalyticsResult(topic_ids=topic_ids, neighbors=neighbors, novelty=novelty, topics=topics)


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
    similarity stays within a country too. Each country's corpus is embedded twice — a
    product-aware text for neighbours and a reason-dominant one for themes (see the weight
    constants). Called by scripts/build_analytics.py. One transaction."""
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
        neighbor_texts = [
            _compose_text(r.reason_text, r.product_description, r.company_name) for r in group
        ]
        theme_texts = [
            _compose_text(
                r.reason_text,
                r.product_description,
                r.company_name,
                reason_weight=_THEME_REASON_WEIGHT,
                product_weight=_THEME_PRODUCT_WEIGHT,
            )
            for r in group
        ]
        # Skip themes for a low-volume country (no topics, neighbours still build); US/UK clear it.
        result = build_analytics(
            neighbor_texts,
            embed_texts(neighbor_texts),
            theme_texts=theme_texts,
            theme_embeddings=embed_texts(theme_texts),
            min_df=_corpus_min_df(len(group)),
            min_topic_docs=_MIN_TOPIC_CORPUS,
        )

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

        for recall, topic_id, novelty in zip(group, result.topic_ids, result.novelty, strict=True):
            topic_ids.append(
                {
                    "b_source": recall.source,
                    "b_number": recall.recall_number,
                    "b_topic": local_to_global[topic_id] if topic_id is not None else None,
                    "b_novelty": novelty,
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

    # Write the derived topic_id + novelty_score while preserving updated_at: setting updated_at to
    # itself keeps the column in the UPDATE's SET clause, so the onupdate=func.now() default doesn't
    # fire and a derived-column change can't masquerade as a source change to status(). A Core table
    # UPDATE (not the ORM update(Recall)) keeps this a plain executemany, not an ORM
    # bulk-update-by-primary-key.
    recall_table = cast(Table, Recall.__table__)
    for start in range(0, len(topic_ids), _DB_CHUNK):
        session.execute(
            update(recall_table)
            .where(recall_table.c.source == bindparam("b_source"))
            .where(recall_table.c.recall_number == bindparam("b_number"))
            .values(
                topic_id=bindparam("b_topic"),
                novelty_score=bindparam("b_novelty"),
                updated_at=recall_table.c.updated_at,
            ),
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
