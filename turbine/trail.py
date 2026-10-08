"""Trail trimming for context_level."""

from __future__ import annotations

import math
from typing import TypeVar

T = TypeVar("T")


def trim_trail(trail: list[T], level: float) -> list[T]:
    """Return the suffix of trail visible at context_level.

    level <= 0 → empty; level >= 1 → all; otherwise last ceil(n * level).
    """
    if level <= 0.0 or not trail:
        return []
    if level >= 1.0:
        return list(trail)
    n = len(trail)
    keep = min(n, max(1, math.ceil(n * level)))
    return list(trail[n - keep :])
