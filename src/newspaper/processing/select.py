"""Decide which groups become notes, where they go and in what order."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Interest:
    phrase: str  # the reader's phrase the model picked
    liked: bool  # False: a topic the reader is not interested in


@dataclass
class Selected:
    items: list[Any]  # articles (anything with source_name, section, when)
    section: str
    is_front: bool
    interest: Interest | None = None


def group_section(items: list[Any], sections: list[str]) -> str:
    """Most common section; ties go to the section listed first in the TOML."""
    counts = Counter(a.section for a in items)
    best = max(counts.values())
    order = {name: i for i, name in enumerate(sections)}
    return min((s for s, c in counts.items() if c == best), key=lambda s: order.get(s, len(order)))


def select_groups(
    groups: list[list[Any]],
    sections: list[str],
    front_size: int,
    per_section: int,
    limits: dict[str, int] | None = None,
    interests: list[Interest | None] | None = None,
) -> list[Selected]:
    """Front page, then sections in TOML order; ranked by distinct outlets, then articles in
    the group (an outlet covering it twice), then recency. `limits` caps single sections.
    `interests` (one per group; None is neutral) sends "no" groups to the end and never to the
    front page, and puts "sí" groups first among those covered by as many outlets."""
    limits = limits or {}
    interests = interests or [None] * len(groups)

    def disliked(i: Interest | None) -> bool:
        return i is not None and not i.liked

    def rank(pair: tuple[list[Any], Interest | None]) -> tuple:
        g, i = pair
        liked = i is not None and i.liked
        outlets = len({a.source_name for a in g})
        return (not disliked(i), outlets, liked, len(g), max(a.when for a in g))

    ranked = sorted(zip(groups, interests, strict=True), key=rank, reverse=True)
    # "No" groups sort last, so the front page is a prefix of the others.
    n_front = min(front_size, sum(not disliked(i) for _, i in ranked))
    front = [Selected(g, group_section(g, sections), True, i) for g, i in ranked[:n_front]]

    by_section: dict[str, list[Selected]] = {}
    for g, i in ranked[n_front:]:
        section = group_section(g, sections)
        bucket = by_section.setdefault(section, [])
        if len(bucket) < limits.get(section, per_section):
            bucket.append(Selected(g, section, False, i))

    order = {name: i for i, name in enumerate(sections)}
    rest = sorted(by_section.items(), key=lambda kv: order.get(kv[0], len(order)))
    return front + [s for _, bucket in rest for s in bucket]
