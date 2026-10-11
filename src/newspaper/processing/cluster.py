"""Group articles that cover the same event."""

from __future__ import annotations

import math
from collections.abc import Sequence


def cluster(vectors: Sequence[Sequence[float]], threshold: float) -> list[list[int]]:
    """Indices grouped by cosine similarity >= threshold, joined transitively.

    ponytail: single-linkage union-find can chain A~B~C into one group; fine at a few hundred
    articles a day with a high threshold. Upgrade to average-linkage if chaining shows up.
    ponytail: pure-Python O(n²) dot products (math.sumprod runs in C): ~16 ms for a real
    edition's 42 articles, ~0.8 s for 300. Bring numpy back if a day ever has thousands.
    """
    units = []
    for v in vectors:
        norm = math.sqrt(math.sumprod(v, v)) or 1.0
        units.append([x / norm for x in v])

    parent = list(range(len(units)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if math.sumprod(units[i], units[j]) >= threshold:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(units)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])
