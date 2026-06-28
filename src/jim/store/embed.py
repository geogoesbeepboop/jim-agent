"""Lightweight local embeddings for the semantic insight cache.

Deliberately dependency-free: a hashed char-trigram bag, L2-normalized. It gives
real *lexical* similarity (near-identical queries land close) with zero external
API. Swap ``embed`` for a real model (Voyage/OpenAI/local sentence-transformers)
to get semantic similarity — the pgvector column and search are unchanged.
"""

from __future__ import annotations

import zlib

import numpy as np

EMBED_DIM = 256


def _stable_hash(s: str) -> int:
    # crc32 is deterministic across processes; Python's str hash is not.
    return zlib.crc32(s.encode("utf-8"))


def embed(text: str) -> list[float]:
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    t = f"  {text.lower().strip()}  "
    for i in range(len(t) - 2):
        gram = t[i : i + 3]
        vec[_stable_hash(gram) % EMBED_DIM] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))
