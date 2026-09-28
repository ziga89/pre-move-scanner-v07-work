"""Rolling robust baselines per market and metric.

Baselines are built from 1-minute records (not 1-second samples), which keeps
memory ~60x smaller than v0.6 and lets them be rebuilt from SQLite after a
restart.

The reference window is *lagged*: the main 2 h baseline ends `lag` minutes ago,
so an anomaly that is unfolding right now is compared with a clean reference
instead of slowly absorbing itself. Minutes with poor data quality (stale feed,
resync) are excluded.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..util import clamp, robust_stats
from .rings import MinuteRing

# Minute-record fields persisted and rehydrated.
MINUTE_RAW = (
    "mid_close", "mid_high", "mid_low", "spread_bps",
    "bid_depth_05", "ask_depth_05", "bid_depth_1", "ask_depth_1", "bid_depth_2", "ask_depth_2",
    "imbalance_1", "buy_usd", "sell_usd", "trades", "max_trade_usd",
    "ask_added", "ask_removed", "bid_added", "bid_removed", "ask_cancel_proxy",
    "slippage_bps", "valid_frac", "book_msgs",
)
# Derived per-minute values used by baselines.
MINUTE_DERIVED = ("vol", "depth_1", "buy_share", "ask_repl", "cancel_intensity", "ask_net_pct", "ask_refill")
MINUTE_FIELDS = MINUTE_RAW + MINUTE_DERIVED

# Metric -> (relative floor for scale, absolute floor for scale)
BASELINE_METRICS: Dict[str, Tuple[float, float]] = {
    "ask_depth_1": (0.05, 1.0),
    "bid_depth_1": (0.05, 1.0),
    "ask_depth_05": (0.05, 1.0),
    "depth_1": (0.05, 1.0),
    "spread_bps": (0.10, 0.05),
    "vol": (0.10, 10.0),
    "trades": (0.10, 0.5),
    "buy_share": (0.0, 0.03),
    "imbalance_1": (0.0, 0.03),
    "slippage_bps": (0.10, 0.05),
    "ask_repl": (0.0, 0.10),
    "cancel_intensity": (0.10, 0.005),
    "ask_net_pct": (0.0, 0.005),
    "ask_refill": (0.0, 0.05),
}

MIN_MATERIAL_REMOVAL_USD = 250.0


def derive_minute(rec: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """Add derived fields to a raw minute record (in place) and return it."""
    buy = rec.get("buy_usd") or 0.0
    sell = rec.get("sell_usd") or 0.0
    vol = buy + sell
    rec["vol"] = vol
    b1, a1 = rec.get("bid_depth_1"), rec.get("ask_depth_1")
    rec["depth_1"] = (b1 + a1) if (b1 is not None and a1 is not None) else None
    # Buy share only from minutes with enough prints to mean something.
    rec["buy_share"] = (buy / vol) if vol >= 200.0 and (rec.get("trades") or 0) >= 3 else None
    removed = rec.get("ask_removed") or 0.0
    material = max(MIN_MATERIAL_REMOVAL_USD, 0.03 * (a1 or 0.0))
    rec["ask_repl"] = ((rec.get("ask_added") or 0.0) / removed) if removed >= material else None
    cp = rec.get("ask_cancel_proxy")
    rec["cancel_intensity"] = (cp / a1) if (cp is not None and a1) else None
    # Churn-independent: net change of resting ask liquidity caused by order
    # flow (excludes band re-centering), as a fraction of ask depth.
    net = (rec.get("ask_added") or 0.0) - removed
    rec["ask_net_pct"] = (net / a1) if a1 else None
    # Refill after fills = (added - cancels) / fills = 1 + net / fills.
    fills = buy
    rec["ask_refill"] = (1.0 + net / fills) if fills >= material else None
    return rec


class Baselines:
    """Median + robust scale for each metric over short / main / long windows."""

    def __init__(self, ecfg: Dict):
        self.short_min = int(ecfg.get("baseline_short_minutes", 30))
        self.main_min = int(ecfg.get("baseline_minutes", 120))
        self.long_min = int(ecfg.get("baseline_long_minutes", 1440))
        self.lag_min = int(ecfg.get("baseline_lag_minutes", 10))
        self.min_minutes = int(ecfg.get("min_baseline_minutes", 20))
        self.min_valid = float(ecfg.get("min_valid_fraction", 0.8))
        self.short: Dict[str, Tuple[float, float, int]] = {}
        self.main: Dict[str, Tuple[float, float, int]] = {}
        self.long: Dict[str, Tuple[float, float, int]] = {}
        self.valid_minutes = 0
        self.long_valid_minutes = 0
        self.computed_at: Optional[int] = None

    @property
    def warm(self) -> bool:
        return self.valid_minutes >= self.min_minutes

    def _window(self, ring: MinuteRing, now_min_ts: int, minutes: int, lag: int):
        end = now_min_ts - lag * 60
        start = end - minutes * 60
        out = {}
        for metric, (rel, absf) in BASELINE_METRICS.items():
            # Zero-activity minutes are kept: they are real information
            # (v0.6 dropped them, which biased volume baselines upward).
            vals = ring.values(metric, start, end, "valid_frac", self.min_valid)
            med, scale = robust_stats(vals, rel, absf)
            if med is not None:
                out[metric] = (med, scale, len(vals))
        n_valid = len(ring.values("mid_close", start, end, "valid_frac", self.min_valid))
        return out, n_valid

    def recompute(self, ring: MinuteRing, now_min_ts: int, include_long: bool = False) -> None:
        self.short, _ = self._window(ring, now_min_ts, self.short_min, self.lag_min)
        self.main, self.valid_minutes = self._window(ring, now_min_ts, self.main_min, self.lag_min)
        if include_long or not self.long:
            self.long, self.long_valid_minutes = self._window(ring, now_min_ts, self.long_min, self.lag_min)
        self.computed_at = now_min_ts

    # ---- accessors ----
    def med(self, metric: str, which: str = "main") -> Optional[float]:
        t = getattr(self, which).get(metric)
        return t[0] if t else None

    def ratio(self, metric: str, value: Optional[float], which: str = "main",
              min_n: int = 5) -> Optional[float]:
        t = getattr(self, which).get(metric)
        if value is None or not t or t[2] < min_n or t[0] <= 0:
            return None
        return value / t[0]

    def z(self, metric: str, value: Optional[float], which: str = "main", min_n: int = 5) -> Optional[float]:
        t = getattr(self, which).get(metric)
        if value is None or not t or t[2] < min_n:
            return None
        return clamp((value - t[0]) / t[1], -20.0, 20.0)

    def delta(self, metric: str, value: Optional[float], which: str = "main", min_n: int = 5) -> Optional[float]:
        t = getattr(self, which).get(metric)
        if value is None or not t or t[2] < min_n:
            return None
        return value - t[0]

    def summary(self) -> Dict[str, Dict[str, float]]:
        return {k: {"median": round(v[0], 6), "scale": round(v[1], 6), "n": v[2]} for k, v in self.main.items()}
