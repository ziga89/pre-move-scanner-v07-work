"""SIM exchange adapter: streams the synthetic SimWorld through the same feed
manager code path as ccxt (partitions, loops, health, fault injection).

Different SIM exchanges advertise different capabilities so both the
multi-symbol and per-symbol subscription paths are exercised.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..sim import SimWorld
from ..universe.catalog import Catalog


class BadSymbol(Exception):
    """Named like the ccxt exception so classify_error treats it as permanent."""


class NetworkError(Exception):
    """Named like the ccxt exception (transient)."""


SIM_HAS = {
    "simex_a": {"watchOrderBookForSymbols": True, "watchOrderBook": True, "watchTradesForSymbols": True, "watchTrades": True},
    "simex_b": {"watchOrderBookForSymbols": False, "watchOrderBook": True, "watchTradesForSymbols": False, "watchTrades": True},
    "simex_c": {"watchOrderBookForSymbols": True, "watchOrderBook": True, "watchTradesForSymbols": False, "watchTrades": True},
    "simex_d": {"watchOrderBook": True, "watchTrades": True},
    "simex_e": {"watchOrderBook": True, "watchTrades": False},
}


class SimDriver:
    def __init__(self, world: SimWorld, clock=time.time, step_s: float = 0.5):
        self.world = world
        self.clock = clock
        self.step_s = step_s
        self.books: Dict[Tuple[str, str], Tuple[list, list]] = {}
        self.book_seq: Dict[Tuple[str, str], int] = {k: 0 for k in world.markets}
        self.trades: Dict[Tuple[str, str], Deque[Dict[str, Any]]] = {k: deque(maxlen=5000) for k in world.markets}
        self.trade_seq: Dict[Tuple[str, str], int] = {k: 0 for k in world.markets}
        self.cond: Optional[asyncio.Condition] = None
        self.fail: Dict[str, int] = {}          # exchange -> remaining calls to fail
        self.steps = 0

    async def run(self) -> None:
        self.cond = asyncio.Condition()
        while True:
            now = self.clock()
            self.world.apply_scenarios(now)
            for key, m in self.world.markets.items():
                b, a, tr = m.step(now, self.step_s)
                if b is not None:
                    self.books[key] = (b, a)
                    self.book_seq[key] += 1
                if tr:
                    for ts, px, amt, side, tid in tr:
                        self.trades[key].append({"timestamp": int(ts * 1000), "price": px, "amount": amt,
                                                 "side": side, "id": tid, "symbol": key[1]})
                    self.trade_seq[key] += len(tr)
            self.steps += 1
            async with self.cond:
                self.cond.notify_all()
            await asyncio.sleep(self.step_s / max(0.01, float(getattr(self.world, "speed", 1.0))))

    async def wait_for(self, pred) -> None:
        while self.cond is None:
            await asyncio.sleep(0.01)
        async with self.cond:
            await self.cond.wait_for(pred)


class SimStreamClient:
    def __init__(self, driver: SimDriver, exchange: str):
        self.d = driver
        self.ex = exchange
        self.has = SIM_HAS.get(exchange, {"watchOrderBook": True, "watchTrades": True})
        self.book_seen: Dict[str, int] = {}
        self.trade_seen: Dict[str, int] = {}
        self.closed = False

    def _check(self, symbol: str) -> Tuple[str, str]:
        key = (self.ex, symbol)
        if key not in self.d.world.markets:
            raise BadSymbol(f"{self.ex} does not have market symbol {symbol}")
        n = self.d.fail.get(self.ex, 0)
        if n > 0:
            self.d.fail[self.ex] = n - 1
            raise NetworkError(f"{self.ex}: simulated disconnect")
        return key

    async def watch_book(self, symbol: str, limit: Optional[int] = None) -> Dict[str, Any]:
        key = self._check(symbol)
        await self.d.wait_for(lambda: self.d.book_seq[key] > self.book_seen.get(symbol, 0))
        self._check(symbol)
        self.book_seen[symbol] = self.d.book_seq[key]
        b, a = self.d.books[key]
        return {"symbol": symbol, "bids": b, "asks": a}

    async def watch_books(self, symbols: List[str], limit: Optional[int] = None) -> Dict[str, Any]:
        keys = [self._check(s) for s in symbols]
        await self.d.wait_for(lambda: any(self.d.book_seq[k] > self.book_seen.get(k[1], 0) for k in keys))
        for k in keys:
            if self.d.book_seq[k] > self.book_seen.get(k[1], 0):
                self.book_seen[k[1]] = self.d.book_seq[k]
                b, a = self.d.books[k]
                return {"symbol": k[1], "bids": b, "asks": a}
        raise NetworkError("no update")

    async def watch_trades(self, symbol: str) -> List[Dict[str, Any]]:
        key = self._check(symbol)
        self.trade_seen.setdefault(symbol, self.d.trade_seq[key])  # only trades after subscribing
        await self.d.wait_for(lambda: self.d.trade_seq[key] > self.trade_seen[symbol])
        seq = self.d.trade_seq[key]
        n_new = seq - self.trade_seen[symbol]
        self.trade_seen[symbol] = seq
        return list(self.d.trades[key])[-n_new:]

    async def watch_trades_multi(self, symbols: List[str]) -> List[Dict[str, Any]]:
        keys = [self._check(s) for s in symbols]
        for k in keys:
            self.trade_seen.setdefault(k[1], self.d.trade_seq[k])
        await self.d.wait_for(lambda: any(self.d.trade_seq[k] > self.trade_seen[k[1]] for k in keys))
        out = []
        for k in keys:
            seq = self.d.trade_seq[k]
            n_new = seq - self.trade_seen[k[1]]
            if n_new > 0:
                out.extend(list(self.d.trades[k])[-n_new:])
                self.trade_seen[k[1]] = seq
        return out

    async def close(self) -> None:
        self.closed = True


class SimAdapter:
    def __init__(self, exchange: str, driver: SimDriver):
        self.exchange = exchange
        self.driver = driver

    def has(self) -> Dict[str, Any]:
        return dict(SIM_HAS.get(self.exchange, {"watchOrderBook": True, "watchTrades": True}), fetchTickers=True)

    async def load_catalog(self) -> Catalog:
        markets, tickers = {}, {}
        now = self.driver.clock()
        for (ex, sym), m in self.driver.world.markets.items():
            if ex != self.exchange:
                continue
            markets[sym] = {"base": m.base, "quote": m.quote, "active": True, "spot": True}
            mid = m.mid()
            daily = m.p.trade_rate * m.p.trade_usd * 86400 * 1.3
            tickers[sym] = {"last": mid, "bid": mid * 0.9995, "ask": mid * 1.0005, "quoteVolume": daily,
                            "baseVolume": daily / mid, "timestamp": now}
        return Catalog(self.exchange, markets, tickers, now).index()

    def new_stream_client(self) -> SimStreamClient:
        return SimStreamClient(self.driver, self.exchange)

    async def close(self) -> None:
        return None
