"""Decide which groups become notes, where they go and in what order."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass
class Selected:
    items: list[Any]  # articles (anything with source_name, section, when)
    section: str
    is_front: bool


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
) -> list[Selected]:
    """Front page, then sections in TOML order; ranked by distinct outlets, then articles in
    the group (an outlet covering it twice), then recency. `limits` caps single sections."""
    limits = limits or {}
    ranked = sorted(
        groups,
        key=lambda g: (len({a.source_name for a in g}), len(g), max(a.when for a in g)),
        reverse=True,
    )
    front = [Selected(g, group_section(g, sections), True) for g in ranked[:front_size]]

    by_section: dict[str, list[Selected]] = {}
    for g in ranked[front_size:]:
        section = group_section(g, sections)
        bucket = by_section.setdefault(section, [])
        if len(bucket) < limits.get(section, per_section):
            bucket.append(Selected(g, section, False))

    order = {name: i for i, name in enumerate(sections)}
    rest = sorted(by_section.items(), key=lambda kv: order.get(kv[0], len(order)))
    return front + [s for _, bucket in rest for s in bucket]
