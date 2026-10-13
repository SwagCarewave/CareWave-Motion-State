"""Maze game control: map the walking-level score (0-1) to a direction.

score <= 0.3 -> 상 (up), <= 0.5 -> 우 (right), <= 0.7 -> 하 (down), <= 1.0 -> 좌 (left).
No score (warm-up) or a signal problem gives no direction.
"""
from __future__ import annotations

import math

DIRECTIONS = ((0.3, "up", "상"), (0.5, "right", "우"), (0.7, "down", "하"), (1.0, "left", "좌"))


def direction(score: float | None) -> tuple[str, str] | None:
    """(code, Korean label) for a score, or None when there is no usable score."""
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return None
    for upper, code, ko in DIRECTIONS:
        if score <= upper:
            return code, ko
    return DIRECTIONS[-1][1], DIRECTIONS[-1][2]
