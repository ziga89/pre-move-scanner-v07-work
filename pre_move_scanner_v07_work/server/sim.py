"""Synthetic market world for tests, offline demos and the SIM feed backend.

It is NOT a market model for research; it only needs to produce order books
and trade tapes with controllable, realistic-looking properties:

* a price-level grid (fixed tick) with persistent quantities, so book diffs
  behave like a real L2 feed (adds, removals, consumption by trades);
* Poisson trade arrivals with lognormal sizes and a controllable buy share;
* scenario knobs: pull deeper asks while keeping the touch refilled (price
  stays flat), stop replenishment, buy pressure, price drift (a pump),
  freezing the book feed (stale), freezing the trade feed, tiny activity.

Everything is deterministic for a given seed.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Trade = Tuple[Optional[float], float, float, str, Optional[str]]


@dataclass
class SimParams:
    trade_rate: float = 1.5          # trades per second
    trade_usd: float = 400.0         # median trade notional (quote ≈ USD)
    buy_prob: float = 0.5
    size_mult: float = 1.0
    rate_mult: float = 1.0
    level_usd: float = 6000.0        # target notional per level
    near_levels: int = 2             # levels treated as "the touch"
    deep_ask_mult: float = 1.0       # multiplier on ask levels beyond the touch
    deep_bid_mult: float = 1.0
    ask_refill: float = 0.35         # mean-reversion speed per second (0..1)
    bid_refill: float = 0.35
    touch_refill: float = 0.6
    churn: float = 0.04              # random add/cancel noise
    drift_pct_per_min: float = 0.0   # scripted trend (pump) in % per minute
    anchor_k: float = 0.05           # mild mean reversion of buy prob to anchor price
    frozen: bool = False             # book feed stops (stale)
    trades_frozen: bool = False      # trade feed stops


class SimMarket:
    def __init__(self, exchange: str, symbol: str, price: float, seed: int = 1,
                 levels: int = 60, tick_bps: float = 5.0, params: Optional[SimParams] = None):
        self.exchange = exchange
        self.symbol = symbol
        base, _, quote = symbol.partition("/")
        self.base, self.quote = base, quote or "USDT"
        self.rng = random.Random(seed)
        self.p = params or SimParams()
        self.tick = price * tick_bps / 1e4
        self.levels = levels
        c = int(round(price / self.tick))
        self.anchor_idx = c
        self.bids: Dict[int, float] = {}
        self.asks: Dict[int, float] = {}
        for k in range(1, levels + 1):
            self.bids[c - k] = self._target_qty(k, False) * self._noise(0.15)
            self.asks[c + k] = self._target_qty(k, True) * self._noise(0.15)
        self._drift_acc = 0.0
        self._tid = 0

    # -- helpers
    def _noise(self, s: float) -> float:
        return math.exp(self.rng.gauss(0.0, s))

    def _price(self, idx: int) -> float:
        return idx * self.tick

    def _target_qty(self, k: int, ask: bool) -> float:
        p = self.p
        mult = 1.0
        if k > p.near_levels:
            mult = p.deep_ask_mult if ask else p.deep_bid_mult
        ref_price = self.anchor_idx * self.tick
        return max(0.0, p.level_usd * mult / ref_price)

    def mid(self) -> float:
        if not self.bids or not self.asks:
            return self.anchor_idx * self.tick
        return 0.5 * (self._price(max(self.bids)) + self._price(min(self.asks)))

    def _poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, int(round(self.rng.gauss(lam, math.sqrt(lam)))))
        L, k, prod = math.exp(-lam), 0, 1.0
        while True:
            prod *= self.rng.random()
            if prod <= L:
                return k
            k += 1

    # -- dynamics
    def _consume(self, side: str, usd: float, ts: float) -> List[Trade]:
        out: List[Trade] = []
        book = self.asks if side == "buy" else self.bids
        remaining = usd
        guard = 0
        while remaining > 1e-9 and book and guard < 50:
            guard += 1
            idx = min(book) if side == "buy" else max(book)
            px = self._price(idx)
            avail = book[idx] * px
            take = min(avail, remaining)
            amt = take / px
            book[idx] -= amt
            if book[idx] <= 1e-12:
                del book[idx]
            remaining -= take
            self._tid += 1
            out.append((ts, px, amt, side, f"{self.exchange}-{self.symbol}-{self._tid}"))
        return out

    def _shift(self, ticks: int) -> None:
        if not ticks:
            return
        self.bids = {i + ticks: q for i, q in self.bids.items()}
        self.asks = {i + ticks: q for i, q in self.asks.items()}
        self.anchor_idx += ticks

    def _replenish(self, dt: float) -> None:
        p = self.p
        bb = max(self.bids) if self.bids else self.anchor_idx - 1
        ba = min(self.asks) if self.asks else self.anchor_idx + 1
        center = (bb + ba) / 2.0
        a0 = int(math.floor(center)) + 1
        b0 = int(math.ceil(center)) - 1
        for k in range(1, self.levels + 1):
            for ask in (True, False):
                idx = a0 + k - 1 if ask else b0 - k + 1
                book = self.asks if ask else self.bids
                target = self._target_qty(k, ask)
                kappa = p.touch_refill if k <= p.near_levels else (p.ask_refill if ask else p.bid_refill)
                kappa = 1.0 - (1.0 - kappa) ** dt
                cur = book.get(idx, 0.0)
                if cur <= 0.0:
                    if self.rng.random() < kappa and target > 0:
                        book[idx] = target * self._noise(0.2)
                    continue
                new = cur + kappa * (target - cur)
                new *= self._noise(p.churn)
                if new <= target * 0.01:
                    book.pop(idx, None)
                else:
                    book[idx] = new
        # keep the dict bounded and uncrossed
        for idx in [i for i in self.asks if i > a0 + self.levels + 5 or i <= bb]:
            self.asks.pop(idx, None)
        for idx in [i for i in self.bids if i < b0 - self.levels - 5 or i >= ba]:
            self.bids.pop(idx, None)

    def step(self, ts: float, dt: float = 1.0):
        """Advance by dt seconds. Returns (bids, asks, trades); books are None when frozen."""
        p = self.p
        trades: List[Trade] = []
        if p.drift_pct_per_min:
            self._drift_acc += (p.drift_pct_per_min / 100.0 / 60.0) * dt * (self.mid() / self.tick)
            whole = int(self._drift_acc)
            if whole:
                self._shift(whole)
                self._drift_acc -= whole
        n = self._poisson(p.trade_rate * p.rate_mult * dt)
        dev_pct = (self.mid() / (self.anchor_idx * self.tick) - 1.0) * 100.0
        bp = min(0.98, max(0.02, p.buy_prob - p.anchor_k * dev_pct))
        for _ in range(n):
            side = "buy" if self.rng.random() < bp else "sell"
            usd = p.trade_usd * p.size_mult * math.exp(self.rng.gauss(0.0, 0.6))
            trades.extend(self._consume(side, usd, ts))
        self._replenish(dt)
        if p.trades_frozen:
            trades = []
        if p.frozen:
            return None, None, trades
        bids = [[self._price(i), q] for i, q in sorted(self.bids.items(), reverse=True)]
        asks = [[self._price(i), q] for i, q in sorted(self.asks.items())]
        return bids, asks, trades


# ---------------------------------------------------------------------------
# A small synthetic universe used by `mode: "sim"` and by tests.

SIM_EXCHANGES = ["simex_a", "simex_b", "simex_c", "simex_d", "simex_e"]
SIM_COINS = [
    ("ALPHA", 12.5), ("BRAVO", 0.84), ("CHARLIE", 143.0), ("DELTA", 2.31), ("ECHO", 0.052),
    ("FOXTROT", 7.9), ("GOLF", 31.4), ("HOTEL", 0.0071), ("INDIA", 1.12), ("JULIET", 460.0),
    ("KILO", 0.39), ("LIMA", 18.2), ("MIKE", 3.3), ("NOVEMBER", 0.91), ("OSCAR", 55.0),
]


def sim_coins(n: int):
    """The named coins, then generated ones (for scale tests beyond 15 assets)."""
    out = list(SIM_COINS[:n])
    i = 0
    while len(out) < n:
        i += 1
        out.append((f"GEN{i:03d}", round(0.5 + (i * 7.3) % 90, 4)))
    return out


@dataclass
class SimWorld:
    """Synthetic multi-coin, multi-venue world with optional scripted scenarios."""

    n_assets: int = 12
    venues_per_asset: int = 4
    seed: int = 7
    scenarios: bool = True
    cycle_seconds: float = 3600.0
    markets: Dict[Tuple[str, str], SimMarket] = field(default_factory=dict)

    def __post_init__(self):
        rng = random.Random(self.seed)
        for i, (coin, price) in enumerate(sim_coins(self.n_assets)):
            n_v = max(1, min(self.venues_per_asset, len(SIM_EXCHANGES)))
            exs = SIM_EXCHANGES[:n_v] if i % 3 else SIM_EXCHANGES[1:n_v + 1]
            for j, ex in enumerate(exs):
                quote = "USDT" if j % 2 == 0 else "USD"
                size_scale = 1.0 / (1 + j)  # venue 0 is the biggest
                params = SimParams(level_usd=6000.0 * size_scale * (1 + i % 4),
                                   trade_rate=1.2 * (1 + i % 3) * size_scale + 0.2,
                                   trade_usd=350.0 * (1 + i % 2))
                self.markets[(ex, f"{coin}/{quote}")] = SimMarket(
                    ex, f"{coin}/{quote}", price * (1 + rng.uniform(-0.001, 0.001)),
                    seed=rng.randint(1, 10**9), params=params)
        self.t0: Optional[float] = None

    def assets(self) -> List[str]:
        return sorted({s.split("/")[0] for _, s in self.markets})

    def markets_for(self, asset: str) -> List[SimMarket]:
        return [m for (ex, s), m in self.markets.items() if s.split("/")[0] == asset]

    def apply_scenarios(self, now: float) -> None:
        """Scripted demo anomalies cycling over time (only when scenarios=True)."""
        if not self.scenarios:
            return
        if self.t0 is None:
            self.t0 = now
        el = now - self.t0
        cycle = float(self.cycle_seconds)
        phase = (el % cycle) / cycle
        names = self.assets()
        if len(names) < 3:
            return
        # Coin 0: structural pre-move build-up in the second half-hour of each cycle.
        for m in self.markets_for(names[0]):
            if phase > 0.5:
                prog = min(1.0, (phase - 0.5) / 0.25)
                m.p.deep_ask_mult = 1.0 - 0.55 * prog
                m.p.ask_refill = 0.35 - 0.3 * prog
                m.p.buy_prob = 0.5 + 0.2 * prog
                m.p.rate_mult = 1.0 + 2.0 * prog
            else:
                m.p.deep_ask_mult, m.p.ask_refill, m.p.buy_prob, m.p.rate_mult = 1.0, 0.35, 0.5, 1.0
        # Coin 1: a pump already under way (should read LATE, not pre-move).
        for m in self.markets_for(names[1]):
            m.p.drift_pct_per_min = 0.35 if 0.6 < phase < 0.8 else 0.0
            m.p.buy_prob = 0.68 if 0.6 < phase < 0.8 else 0.5
        # Coin 2: tiny tape with 100 % buys (the $70 false-positive case).
        for m in self.markets_for(names[2]):
            if 0.3 < phase < 0.6:
                m.p.trade_rate, m.p.trade_usd, m.p.buy_prob = 0.03, 35.0, 1.0
            else:
                m.p.trade_rate, m.p.trade_usd, m.p.buy_prob = 1.2, 350.0, 0.5
