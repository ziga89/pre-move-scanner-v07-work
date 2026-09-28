"""Cross-exchange price lead/lag from 1-second mid prices.

For each venue, find the lag (seconds) that maximises the correlation between
the reference venue's returns and the venue's lagged returns. A positive lag
means the venue *leads* the reference. Pure Python; computed on demand (coin
detail view) and for the top-ranked coins only.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional


def _returns(series: List[Optional[float]]) -> List[float]:
    out: List[float] = []
    last = None
    for p in series:
        if p is None or p <= 0:
            out.append(0.0)
            continue
        out.append(math.log(p / last) if last else 0.0)
        last = p
    return out


def _corr(a: List[float], b: List[float]) -> Optional[float]:
    n = len(a)
    if n < 10:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    sab = saa = sbb = 0.0
    for x, y in zip(a, b):
        dx, dy = x - ma, y - mb
        sab += dx * dy
        saa += dx * dx
        sbb += dy * dy
    if saa <= 0 or sbb <= 0:
        return None
    return sab / math.sqrt(saa * sbb)


def price_lead_lag(series: Dict[str, List[Optional[float]]], ref: str, max_lag: int = 10,
                   min_moves: int = 15) -> Dict[str, Dict[str, Optional[float]]]:
    if ref not in series:
        return {}
    r_ref = _returns(series[ref])
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for venue, s in series.items():
        if venue == ref:
            continue
        r = _returns(s)
        moves = sum(1 for x in r if x != 0.0)
        if moves < min_moves or sum(1 for x in r_ref if x != 0.0) < min_moves:
            out[venue] = {"lag_s": None, "corr": None, "note": "too few price changes"}
            continue
        best_lag, best_c = None, None
        n = len(r)
        for lag in range(-max_lag, max_lag + 1):
            # venue at t-lag vs reference at t
            if lag >= 0:
                a, b = r_ref[lag:], r[:n - lag]
            else:
                a, b = r_ref[:n + lag], r[-lag:]
            c = _corr(a, b)
            if c is not None and (best_c is None or c > best_c):
                best_lag, best_c = lag, c
        out[venue] = {"lag_s": best_lag, "corr": round(best_c, 3) if best_c is not None else None,
                      "note": ("leads" if best_lag and best_lag > 0 else "lags" if best_lag and best_lag < 0
                               else "in sync")}
    return out
