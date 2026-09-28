"""Feed-layer interfaces shared by the ccxt, SIM and test adapters."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

# (exchange_ts_seconds or None, price, amount, side, id)
TradeTuple = Tuple[Optional[float], float, float, str, Optional[str]]

# Exceptions that mean "this market/request can never work" (ccxt class names).
PERMANENT_ERRORS = {"BadSymbol", "NotSupported", "BadRequest", "ArgumentsRequired", "InvalidOrder",
                    "PermissionDenied", "AccountNotEnabled", "MarketClosed"}


def classify_error(exc: BaseException) -> str:
    names = {c.__name__ for c in type(exc).__mro__}
    if names & PERMANENT_ERRORS:
        return "permanent"
    return "transient"


def trades_from_ccxt(trades: Sequence[Dict[str, Any]]) -> Dict[str, List[TradeTuple]]:
    """Group ccxt trade dicts by symbol and convert to tuples."""
    out: Dict[str, List[TradeTuple]] = {}
    for t in trades or []:
        ts = t.get("timestamp")
        out.setdefault(t.get("symbol"), []).append(
            ((ts / 1000.0) if ts else None, float(t.get("price") or 0.0), float(t.get("amount") or 0.0),
             str(t.get("side") or ""), str(t["id"]) if t.get("id") is not None else None))
    return out


class FeedSink(Protocol):
    def on_book(self, exchange: str, symbol: str, bids, asks, recv_ts: float, resync: bool) -> None: ...
    def on_trades(self, exchange: str, symbol: str, trades: List[TradeTuple], recv_ts: float) -> None: ...
    def on_market_status(self, exchange: str, symbol: str, status: str, reason: str, ts: float) -> None: ...


class StreamClient(Protocol):
    has: Dict[str, Any]
    async def watch_book(self, symbol: str, limit: Optional[int]) -> Dict[str, Any]: ...
    async def watch_books(self, symbols: List[str], limit: Optional[int]) -> Dict[str, Any]: ...
    async def watch_trades(self, symbol: str) -> List[Dict[str, Any]]: ...
    async def watch_trades_multi(self, symbols: List[str]) -> List[Dict[str, Any]]: ...
    async def close(self) -> None: ...


class ExchangeAdapter(Protocol):
    exchange: str
    def has(self) -> Dict[str, Any]: ...
    async def load_catalog(self): ...
    def new_stream_client(self) -> StreamClient: ...
    async def close(self) -> None: ...
