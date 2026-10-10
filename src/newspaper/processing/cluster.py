"""Group articles that cover the same event."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def cluster(vectors: Sequence[Sequence[float]], threshold: float) -> list[list[int]]:
    """Indices grouped by cosine similarity >= threshold, joined transitively.

    ponytail: single-linkage union-find can chain A~B~C into one group; fine at a few hundred
    articles a day with a high threshold. Upgrade to average-linkage if chaining shows up.
    """
    if len(vectors) == 0:
        return []
    v = np.asarray(vectors, dtype=np.float32)
    v /= np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)
    similar = np.triu((v @ v.T) >= threshold, k=1)

    parent = list(range(len(v)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in zip(*np.nonzero(similar), strict=True):
        parent[find(int(i))] = find(int(j))

    groups: dict[int, list[int]] = {}
    for i in range(len(v)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])
