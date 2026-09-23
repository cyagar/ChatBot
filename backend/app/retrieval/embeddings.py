"""Local sentence-transformer embeddings.

Runs entirely on this machine (CPU) so the retrieval half of the system works
with no AI provider key. The model is loaded lazily and cached, because import
cost is significant and most unit tests don't need it.
"""

from __future__ import annotations

import struct
from functools import lru_cache

import numpy as np

from app.config import get_settings


def embedding_fingerprint() -> str:
    """P1-15 (external review, 2026-09-21): embeddings.model_name used to
    store only settings.embedding_model (e.g. "BAAI/bge-small-en-v1.5"),
    never the revision -- bumping embedding_model_revision to a different
    commit of the same repo (different weights, a different vector space)
    left every existing row's model_name identical, so nothing detected
    that its vectors were now incompatible with fresh ones, and
    vector_search() compared them anyway. This fingerprint is what actually
    gets written and filtered on now: same model name AND revision, not
    just the name."""
    settings = get_settings()
    return f"{settings.embedding_model}@{settings.embedding_model_revision}"


@lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    try:
        return SentenceTransformer(settings.embedding_model, revision=settings.embedding_model_revision)
    except Exception as e:
        # Surface this as a clear, actionable failure rather than whatever
        # huggingface_hub/urllib raises three layers down -- the Docker image
        # bakes this model in at build time specifically so this path is only
        # ever hit by a misconfigured/offline deployment, not normal use
        # (independent review concern #18: no silent first-use download).
        raise RuntimeError(
            f"Could not load embedding model {settings.embedding_model!r} "
            f"(revision {settings.embedding_model_revision!r}). If this is a fresh "
            "environment, the model wasn't baked into the image/cache as expected, "
            "and this deployment has no route to download it now."
        ) from e


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    model = get_model()
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    # bge models expect this instruction prefix on the query side only.
    return embed_texts([f"Represent this sentence for searching relevant passages: {text}"])[0]


def vector_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)
