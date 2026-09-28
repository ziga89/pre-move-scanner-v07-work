"""ccxt / ccxt.pro adapter.

ccxt is imported lazily so the rest of the scanner (and the test-suite) runs
without it. REST (catalog) and websocket (stream) use the SAME unified
symbols, so a pair discovered via fetch_tickers is exactly the pair streamed.

Client lifecycle (see CcxtStreamClient): every stream client is one ccxt.pro
instance, owned by one feed-manager partition, bound to the running event loop
before its first watch, closed exactly once and never reused after close.
"""
from __future__ import annotations

import asyncio
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


class StreamClientClosed(Exception):
    """A watch was attempted on a stream client that was already closed."""


class StreamClientLoopError(RuntimeError):
    """A stream client was used from a different event loop than the one it is bound to."""


class CcxtStreamClient:
    """One ccxt.pro instance used by one partition.

    ccxt binds an instance to the event loop in `open()`. ccxt calls `open()` from REST
    requests, `client()` and `watch_multiple()`, but some exchanges schedule tasks before
    any of those: KuCoin's watchers call `negotiate()` -> `spawn()` ->
    `self.asyncio_loop.create_task(...)` first. When the markets were shared from the
    catalog (`set_markets`), no REST request has run, `asyncio_loop` is still None and every
    KuCoin subscription fails with
    AttributeError: 'NoneType' object has no attribute 'create_task'.
    `_ready()` therefore opens the instance inside the running loop before the first watch.
    """

    def __init__(self, ex, exchange: str = ""):
        self.ex = ex
        self.exchange = exchange or str(getattr(ex, "id", "") or "")
        self.has = dict(getattr(ex, "has", {}) or {})
        self.closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _ready(self) -> None:
        if self.closed:
            raise StreamClientClosed(f"{self.exchange} stream client was closed")
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._loop is not None:
            raise StreamClientLoopError(f"{self.exchange} stream client is bound to another event loop")
        opener = getattr(self.ex, "open", None)
        if callable(opener) and getattr(self.ex, "asyncio_loop", None) is None:
            opener()
        bound = getattr(self.ex, "asyncio_loop", loop)
        if bound is not None and bound is not loop:
            raise StreamClientLoopError(f"{self.exchange} ccxt instance is bound to another event loop")
        self._loop = loop

    @staticmethod
    def _alias(symbol: Optional[str], requested: List[str]) -> Optional[str]:
        """Map a symbol reported by the exchange back to the one that was subscribed.

        Coinbase merged its USD and USDC books: a BASE/USDC subscription is served under
        BASE-USD, and ccxt resolves the USDC request with a book labelled BASE/USD. Without
        this mapping the multi-symbol loop would discard every update for the USDC market.
        """
        if symbol is None or symbol in requested:
            return symbol
        if symbol.endswith("/USD") and symbol + "C" in requested:
            return symbol + "C"
        if symbol.endswith("/USDC") and symbol[:-1] in requested:
            return symbol[:-1]
        return symbol

    async def watch_book(self, symbol: str, limit: Optional[int]) -> Dict[str, Any]:
        self._ready()
        ob = await (self.ex.watch_order_book(symbol, limit) if limit else self.ex.watch_order_book(symbol))
        return {"symbol": self._alias(ob.get("symbol") or symbol, [symbol]), "bids": ob["bids"], "asks": ob["asks"],
                "nonce": ob.get("nonce"), "timestamp": ob.get("timestamp")}

    async def watch_books(self, symbols: List[str], limit: Optional[int]) -> Dict[str, Any]:
        self._ready()
        ob = await (self.ex.watch_order_book_for_symbols(symbols, limit) if limit
                    else self.ex.watch_order_book_for_symbols(symbols))
        return {"symbol": self._alias(ob.get("symbol"), symbols), "bids": ob["bids"], "asks": ob["asks"],
                "nonce": ob.get("nonce"), "timestamp": ob.get("timestamp")}

    def _alias_trades(self, trades, symbols: List[str]) -> List[Dict[str, Any]]:
        out = []
        for t in trades or []:
            s = t.get("symbol")
            a = self._alias(s, symbols)
            out.append(t if a == s else dict(t, symbol=a))
        return out

    async def watch_trades(self, symbol: str) -> List[Dict[str, Any]]:
        self._ready()
        return self._alias_trades(await self.ex.watch_trades(symbol), [symbol])

    async def watch_trades_multi(self, symbols: List[str]) -> List[Dict[str, Any]]:
        self._ready()
        return self._alias_trades(await self.ex.watch_trades_for_symbols(symbols), symbols)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
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
        self.market_share_error = ""

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
        """A fresh ccxt.pro instance (never a reused or closed one) for one partition generation."""
        _, pro = self._modules()
        opts = self._options()
        try:  # bind to the loop that will run it (ccxt's documented `asyncio_loop` option)
            opts["asyncio_loop"] = asyncio.get_running_loop()
        except RuntimeError:
            pass
        ex = getattr(pro, self.ccxt_id)(opts)
        if self._markets is not None:
            # Share the catalog's markets: no load_markets REST call per partition / reconnect.
            try:
                ex.set_markets(self._markets, self._currencies)
            except Exception as exc:  # the instance then loads its markets itself
                self.market_share_error = f"{type(exc).__name__}: {exc}"[:200]
        return CcxtStreamClient(ex, self.exchange)

    async def close(self) -> None:
        if self._rest is not None:
            try:
                await self._rest.close()
            except Exception:
                pass
            self._rest = None
