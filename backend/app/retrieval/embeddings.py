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


@lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    return SentenceTransformer(settings.embedding_model)


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
