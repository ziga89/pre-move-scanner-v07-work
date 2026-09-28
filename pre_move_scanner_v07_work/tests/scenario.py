"""Scenario harness: SIM venues → MarketState → AssetState on a virtual clock."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from tests.helpers import T0, cfg as make_cfg, fx_one
from server.engine.asset_state import AssetState
from server.engine.market_state import MarketState
from server.sim import SimMarket, SimParams


class Scenario:
    def __init__(self, n_venues: int = 4, price: float = 100.0, seed: int = 11, config=None,
                 venue_scale: Optional[List[float]] = None, params: Optional[Dict] = None):
        self.cfg = config or make_cfg(engine={"min_baseline_minutes": 20, "baseline_lag_minutes": 10})
        self.venues: List[SimMarket] = []
        self.states: List[MarketState] = []
        scale = venue_scale or [1.0, 0.8, 0.6, 0.45, 0.35][:n_venues]
        for i in range(n_venues):
            p = SimParams(level_usd=6000.0 * scale[i], trade_rate=1.5 * scale[i] + 0.3,
                          trade_usd=400.0, **(params or {}))
            m = SimMarket(f"ex{i}", "COIN/USDT", price, seed=seed + i, params=p)
            self.venues.append(m)
            self.states.append(MarketState("COIN", f"ex{i}", "COIN/USDT", "USDT", fx_one,
                                           self.cfg["engine"], self.cfg["feeds"], now=T0, stagger=i * 7))
        self.asset = AssetState("COIN", self.cfg)
        self.t = T0
        self.results: List[Dict] = []

    def run(self, seconds: int, control: Optional[Callable[[float, "Scenario"], None]] = None,
            record_every: int = 1) -> Dict:
        res = None
        for i in range(seconds * 2):
            ts = self.t + 0.5
            self.t = ts
            if control:
                control(ts, self)
            for m, st in zip(self.venues, self.states):
                b, a, tr = m.step(ts, 0.5)
                if b:
                    st.on_book(b, a, ts)
                st.on_trades(tr, ts)
            if i % 2 == 1:
                feats = [st.tick(ts) for st in self.states]
                res = self.asset.update(ts, feats, len(self.states))
                if (i // 2) % record_every == 0:
                    self.results.append(res)
        return res

    def each(self, fn: Callable[[SimMarket], None], venues=None):
        for i, m in enumerate(self.venues):
            if venues is None or i in venues:
                fn(m)
