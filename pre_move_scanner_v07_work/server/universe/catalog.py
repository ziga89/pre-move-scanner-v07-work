"""Exchange market catalog: every spot pair and its 24h ticker, per exchange.

One `load_markets` + one `fetch_tickers` call per exchange gives the 24h
volume of *every* coin on that exchange — ~15 calls instead of ~200 per-coin
calls, and the symbols are exactly the ones the realtime feed will use.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Catalog:
    exchange: str
    markets: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # symbol -> {base, quote, active, spot}
    tickers: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # symbol -> {last, bid, ask, quoteVolume, baseVolume, timestamp}
    fetched_ts: float = 0.0
    error: str = ""
    by_base: Dict[str, List[str]] = field(default_factory=dict)

    def index(self) -> "Catalog":
        self.by_base = {}
        for sym, m in self.markets.items():
            if not m.get("spot", True):
                continue
            self.by_base.setdefault(str(m.get("base", "")).upper(), []).append(sym)
        return self

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Catalog":
        c = cls(exchange=d["exchange"], markets=d.get("markets", {}), tickers=d.get("tickers", {}),
                fetched_ts=d.get("fetched_ts", time.time()), error=d.get("error", ""))
        return c.index()

    def to_dict(self) -> Dict[str, Any]:
        return {"exchange": self.exchange, "markets": self.markets, "tickers": self.tickers,
                "fetched_ts": self.fetched_ts, "error": self.error}

    def summary(self) -> Dict[str, Any]:
        return {"exchange": self.exchange, "markets": len(self.markets), "tickers": len(self.tickers),
                "fetched_ts": self.fetched_ts, "error": self.error}


def market_from_ccxt(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reduce a ccxt market dict to what discovery needs; None for non-spot."""
    if not m.get("spot") and m.get("type") not in (None, "spot"):
        return None
    if m.get("type") and m.get("type") != "spot":
        return None
    return {"base": str(m.get("base", "")).upper(), "quote": str(m.get("quote", "")).upper(),
            "active": m.get("active") is not False, "spot": True, "id": m.get("id")}


def ticker_from_ccxt(t: Dict[str, Any]) -> Dict[str, Any]:
    ts = t.get("timestamp")
    return {"last": t.get("last") or t.get("close"), "bid": t.get("bid"), "ask": t.get("ask"),
            "quoteVolume": t.get("quoteVolume"), "baseVolume": t.get("baseVolume"),
            "timestamp": (ts / 1000.0) if ts else None}
