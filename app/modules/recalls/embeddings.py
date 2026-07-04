"""Static neural embeddings for recall text — Model2Vec `potion-base-8M`. No LLM, no torch.

The text backbone for the analytics build (neighbours, themes, event edges). A static-embedding
model was chosen over a transformer deliberately: the API host is tiny (0.1 CPU / 512 MB), and
Model2Vec is numpy-only with a ~30 MB artifact, so the door stays open to request-time query
embedding (semantic search) later. Benchmarked against the previous TF-IDF representation on FDA
`event_id` ground truth — see scripts/eval_embeddings.py and model/embedding_eval_card.md.

Offline-only, like sklearn: imported by the analytics build and scripts, never on the request
path. The model downloads from Hugging Face on first use and is cached locally after that.
"""

import numpy as np

MODEL_NAME = "minishlab/potion-base-8M"


def embed_texts(texts: list[str], model_name: str = MODEL_NAME) -> np.ndarray:
    """One L2-normalised float32 embedding row per text, so dot products are cosine similarities.

    An empty (or fully out-of-vocabulary) text embeds to the zero vector and stays zero after the
    norm guard — it can never clear a similarity threshold, mirroring how such rows already get no
    neighbours and no theme."""
    # Lazy: keeps importing this module (and analytics) free of the model dependency, so the app
    # and the DB-free test suite never touch it.
    from model2vec import StaticModel

    model = StaticModel.from_pretrained(model_name)
    embeddings = np.asarray(model.encode(list(texts)), dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.maximum(norms, 1e-12)
