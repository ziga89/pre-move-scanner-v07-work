"""Band-limited L2 book state.

Only levels within ±band% of mid are kept. Each update is diffed against the
previous band to measure liquidity added/removed per side. Three artefacts that
inflated v0.6's add/remove numbers are excluded:

* far-from-mid levels (outside the band) are ignored entirely;
* levels that merely enter/leave the band because the mid moved are ignored
  (the diff only covers the price range inside BOTH the old and new band);
* levels beyond the visible depth of a truncated book are ignored (the diff
  only covers the price range both snapshots actually show).

The first update after a (re)connect is a snapshot: it is stored, never diffed,
so reconnects cannot create phantom removal/replenishment spikes.

Quantities are in base units and notionals in quote units; the caller converts
quote→USD.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

Level = Tuple[float, float]


class BandBook:
    __slots__ = (
        "band", "flow_band", "bids", "asks", "bid_levels", "ask_levels", "best_bid", "best_ask", "mid",
        "bid_floor", "ask_ceiling", "bid_cov_price", "ask_cov_price", "updates",
    )

    def __init__(self, band_pct: float = 2.0, flow_band_pct: Optional[float] = None):
        self.band = float(band_pct) / 100.0
        # Add/remove accounting (replenishment, cancel proxy) uses the near-touch band.
        self.flow_band = min(self.band, float(flow_band_pct) / 100.0) if flow_band_pct else self.band
        self.reset()

    def reset(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.bid_levels: List[Level] = []   # best (highest) first
        self.ask_levels: List[Level] = []   # best (lowest) first
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.mid = 0.0
        self.bid_floor = 0.0      # lowest bid price the band covers
        self.ask_ceiling = 0.0    # highest ask price the band covers
        self.bid_cov_price = 0.0  # deepest visible bid inside the band
        self.ask_cov_price = 0.0  # deepest visible ask inside the band
        self.updates = 0

    @property
    def ready(self) -> bool:
        return self.mid > 0

    @staticmethod
    def _take_side(levels: Sequence, limit_price: float, is_bid: bool):
        # Levels are sorted best-first; find the band edge, then convert in one pass.
        n = len(levels)
        cut = n
        if is_bid:
            for i in range(n):
                if levels[i][0] < limit_price:
                    cut = i
                    break
        else:
            for i in range(n):
                if levels[i][0] > limit_price:
                    cut = i
                    break
        ordered = [(float(l[0]), float(l[1])) for l in levels[:cut] if l[1] > 0]
        out = dict(ordered)
        # If the visible book ran out before the band edge, coverage stops at the deepest level.
        if cut == n:
            cov = ordered[-1][0] if ordered else limit_price
        else:
            cov = limit_price
        return out, ordered, cov

    def update(self, bids: Sequence, asks: Sequence, snapshot: bool = False
               ) -> Optional[Tuple[float, float, float, float]]:
        """Apply a full (sorted) book view.

        Returns (bid_added, bid_removed, ask_added, ask_removed) in quote
        notional, or None when no diff is valid (first update, snapshot,
        crossed/empty book).
        """
        if not bids or not asks:
            return None
        bb = float(bids[0][0])
        ba = float(asks[0][0])
        if bb <= 0 or ba <= 0 or ba <= bb:
            return None  # crossed or invalid; ignore rather than corrupt state
        mid = 0.5 * (bb + ba)
        floor = mid * (1.0 - self.band)
        ceiling = mid * (1.0 + self.band)
        new_b, blv, bcov = self._take_side(bids, floor, True)
        new_a, alv, acov = self._take_side(asks, ceiling, False)

        flows = None
        if self.mid > 0 and not snapshot:
            # Diff only inside the intersection of old/new covered ranges and the
            # (narrower) flow band around mid.
            fb = self.flow_band
            a_lim = min(self.ask_cov_price, acov, mid * (1.0 + fb), self.mid * (1.0 + fb))
            b_lim = max(self.bid_cov_price, bcov, mid * (1.0 - fb), self.mid * (1.0 - fb))
            old_a, old_b = self.asks, self.bids
            da = [(q - old_a.get(p, 0.0)) * p for p, q in alv if p <= a_lim]
            a_add = sum(d for d in da if d > 0)
            a_rem = -sum(d for d in da if d < 0) + sum(q * p for p, q in self.ask_levels if p <= a_lim and p not in new_a)
            db = [(q - old_b.get(p, 0.0)) * p for p, q in blv if p >= b_lim]
            b_add = sum(d for d in db if d > 0)
            b_rem = -sum(d for d in db if d < 0) + sum(q * p for p, q in self.bid_levels if p >= b_lim and p not in new_b)
            flows = (b_add, b_rem, a_add, a_rem)

        self.bids, self.asks = new_b, new_a
        self.bid_levels, self.ask_levels = blv, alv
        self.best_bid, self.best_ask, self.mid = bb, ba, mid
        self.bid_floor, self.ask_ceiling = floor, ceiling
        self.bid_cov_price, self.ask_cov_price = bcov, acov
        self.updates += 1
        return flows

    # ---- read-side metrics (computed at sample time, not per update) ----

    def depths(self) -> Tuple[float, float, float, float, float, float]:
        """(bid05, ask05, bid1, ask1, bid2, ask2) quote notional within ±0.5/1/2 %."""
        m = self.mid
        if m <= 0:
            return (0.0,) * 6
        b05 = m * 0.995
        b1 = m * 0.99
        b2 = m * 0.98
        a05 = m * 1.005
        a1 = m * 1.01
        a2 = m * 1.02
        bd05 = bd1 = bd2 = 0.0
        for p, q in self.bid_levels:
            if p < b2:
                break
            n = p * q
            bd2 += n
            if p >= b1:
                bd1 += n
                if p >= b05:
                    bd05 += n
        ad05 = ad1 = ad2 = 0.0
        for p, q in self.ask_levels:
            if p > a2:
                break
            n = p * q
            ad2 += n
            if p <= a1:
                ad1 += n
                if p <= a05:
                    ad05 += n
        return bd05, ad05, bd1, ad1, bd2, ad2

    def coverage_pct(self) -> float:
        """How far (in %) the visible book reaches on its shallower side, capped at the band."""
        if self.mid <= 0:
            return 0.0
        bid_reach = (self.mid - self.bid_cov_price) / self.mid if self.bid_cov_price else 0.0
        ask_reach = (self.ask_cov_price - self.mid) / self.mid if self.ask_cov_price else 0.0
        return round(100.0 * min(self.band, max(0.0, min(bid_reach, ask_reach))), 4)

    def spread_bps(self) -> float:
        if self.mid <= 0:
            return 0.0
        return (self.best_ask - self.best_bid) / self.mid * 1e4

    def buy_slippage_bps(self, order_quote: float) -> Optional[float]:
        """Average-price slippage (bps vs mid) of a market buy of `order_quote`.

        None when the visible band cannot fill the order (treated by callers as
        'exceeds visible liquidity').
        """
        if self.mid <= 0 or order_quote <= 0:
            return None
        remaining = order_quote
        base_bought = 0.0
        for p, q in self.ask_levels:
            notional = p * q
            take = notional if notional < remaining else remaining
            base_bought += take / p
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > 1e-9 or base_bought <= 0:
            return None
        avg = order_quote / base_bought
        return (avg / self.mid - 1.0) * 1e4

    def histogram(self, bins: int = 20) -> Dict[str, List[float]]:
        """Depth histogram across the band (quote notional per bucket), for the UI."""
        out = {"bid": [0.0] * bins, "ask": [0.0] * bins, "band_pct": self.band * 100}
        if self.mid <= 0:
            return out
        width = self.band / bins
        for p, q in self.bid_levels:
            i = int(((self.mid - p) / self.mid) / width)
            if 0 <= i < bins:
                out["bid"][i] += p * q
        for p, q in self.ask_levels:
            i = int(((p - self.mid) / self.mid) / width)
            if 0 <= i < bins:
                out["ask"][i] += p * q
        return out
