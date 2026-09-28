"""Small numeric helpers shared by the engine, scoring and intel layers.

Everything here is pure standard library so the core can be tested without
third-party packages.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

MAD_TO_SIGMA = 1.4826


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x != x:  # NaN
        return lo
    return lo if x < lo else hi if x > hi else x


def ramp(x: Optional[float], start: float, full: float) -> float:
    """Linear 0..1 ramp: 0 at `start`, 1 at `full` (either direction)."""
    if x is None:
        return 0.0
    if full == start:
        return 1.0 if x >= full else 0.0
    return clamp((x - start) / (full - start))


def safe_div(a: float, b: float, default: Optional[float] = None) -> Optional[float]:
    if not b:
        return default
    return a / b


def median(values: Sequence[float]) -> Optional[float]:
    n = len(values)
    if not n:
        return None
    s = sorted(values)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def robust_stats(values: Sequence[float], rel_floor: float = 0.05,
                 abs_floor: float = 1e-12) -> Tuple[Optional[float], Optional[float]]:
    """Median and a MAD-based sigma estimate with a floor.

    The floor stops near-constant series (e.g. a market-maker quoting the same
    size all day) from producing enormous z-scores on tiny changes.
    """
    n = len(values)
    if not n:
        return None, None
    s = sorted(values)
    mid = n // 2
    med = s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])
    dev = sorted(abs(v - med) for v in s)
    mad = dev[mid] if n % 2 else 0.5 * (dev[mid - 1] + dev[mid])
    scale = max(MAD_TO_SIGMA * mad, rel_floor * abs(med), abs_floor)
    return med, scale


def pct_change(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Percent change from a to b, or None when undefined."""
    if not a or b is None:
        return None
    return (b / a - 1.0) * 100.0


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def rnd(x: Optional[float], nd: int = 4) -> Optional[float]:
    if x is None or not finite(x):
        return None
    return round(x, nd)


def weighted_mean(pairs: Iterable[Tuple[Optional[float], float]]) -> Optional[float]:
    num = den = 0.0
    for v, w in pairs:
        if v is None or w <= 0:
            continue
        num += v * w
        den += w
    return num / den if den else None


def chunked(seq: List, size: int) -> List[List]:
    size = max(1, int(size))
    return [seq[i:i + size] for i in range(0, len(seq), size)]
