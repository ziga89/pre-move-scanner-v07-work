"""ccxt / ccxt.pro adapter.

ccxt is imported lazily so the rest of the scanner (and the test-suite) runs
without it. REST (catalog) and websocket (stream) use the SAME unified
symbols, so a pair discovered via fetch_tickers is exactly the pair streamed.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..universe.catalog import Catalog, market_from_ccxt, ticker_from_ccxt

CCXT_IDS = {"gate": "gate", "htx": "htx", "cryptocom": "cryptocom"}  # our id -> ccxt id when they differ


def ccxt_available() -> bool:
    try:
        import ccxt  # noqa: F401
        import ccxt.pro  # noqa: F401
        return True
    except Exception:
        return False


class CcxtStreamClient:
    def __init__(self, ex):
        self.ex = ex
        self.has = dict(getattr(ex, "has", {}) or {})

    async def watch_book(self, symbol: str, limit: Optional[int]) -> Dict[str, Any]:
        ob = await (self.ex.watch_order_book(symbol, limit) if limit else self.ex.watch_order_book(symbol))
        return {"symbol": ob.get("symbol", symbol), "bids": ob["bids"], "asks": ob["asks"],
                "nonce": ob.get("nonce"), "timestamp": ob.get("timestamp")}

    async def watch_books(self, symbols: List[str], limit: Optional[int]) -> Dict[str, Any]:
        ob = await (self.ex.watch_order_book_for_symbols(symbols, limit) if limit
                    else self.ex.watch_order_book_for_symbols(symbols))
        return {"symbol": ob.get("symbol"), "bids": ob["bids"], "asks": ob["asks"],
                "nonce": ob.get("nonce"), "timestamp": ob.get("timestamp")}

    async def watch_trades(self, symbol: str) -> List[Dict[str, Any]]:
        return await self.ex.watch_trades(symbol)

    async def watch_trades_multi(self, symbols: List[str]) -> List[Dict[str, Any]]:
        return await self.ex.watch_trades_for_symbols(symbols)

    async def close(self) -> None:
        await self.ex.close()


class CcxtAdapter:
    def __init__(self, exchange: str, module: Any = None, pro_module: Any = None):
        self.exchange = exchange
        self.ccxt_id = CCXT_IDS.get(exchange, exchange)
        self._mod = module
        self._pro = pro_module
        self._rest = None
        self._markets = None
        self._currencies = None
        self._has: Optional[Dict[str, Any]] = None

    def _modules(self):
        if self._mod is None or self._pro is None:
            import ccxt.async_support as rest  # type: ignore
            import ccxt.pro as pro  # type: ignore
            self._mod = self._mod or rest
            self._pro = self._pro or pro
        return self._mod, self._pro

    def _options(self) -> Dict[str, Any]:
        return {"enableRateLimit": True, "options": {"defaultType": "spot", "newUpdates": True}}

    def has(self) -> Dict[str, Any]:
        if self._has is None:
            _, pro = self._modules()
            cls = getattr(pro, self.ccxt_id)
            inst = cls(self._options())
            self._has = dict(inst.has)
        return self._has

    async def load_catalog(self) -> Catalog:
        rest_mod, _ = self._modules()
        cat = Catalog(self.exchange, fetched_ts=time.time())
        try:
            if self._rest is None:
                self._rest = getattr(rest_mod, self.ccxt_id)(self._options())
            markets = await self._rest.load_markets()
            self._markets, self._currencies = markets, getattr(self._rest, "currencies", None)
            spot = {}
            for sym, m in markets.items():
                mm = market_from_ccxt(m)
                if mm:
                    spot[sym] = mm
            cat.markets = spot
            try:
                tickers = await self._rest.fetch_tickers()
            except Exception:
                # some exchanges require explicit symbols
                tickers = await self._rest.fetch_tickers(list(spot)[:1000])
            cat.tickers = {s: ticker_from_ccxt(t) for s, t in (tickers or {}).items() if s in spot}
        except Exception as exc:
            cat.error = f"{type(exc).__name__}: {exc}"[:300]
        return cat.index()

    def new_stream_client(self) -> CcxtStreamClient:
        _, pro = self._modules()
        ex = getattr(pro, self.ccxt_id)(self._options())
        if self._markets is not None:
            try:
                ex.set_markets(self._markets, self._currencies)
            except Exception:
                pass
        return CcxtStreamClient(ex)

    async def close(self) -> None:
        if self._rest is not None:
            try:
                await self._rest.close()
            except Exception:
                pass
            self._rest = None
