"""Exchange capability matrix (v0.7 amendment 3).

Exchanges do NOT share one subscription model. For each exchange the feed
manager combines:

  1. a static policy (this file): connection/subscription limits and whether
     multi-symbol calls are *allowed* by our policy — values are conservative
     assumptions from exchange docs, marked `verified: False` until
     tools/selftest.py confirms them against the live exchange;
  2. the runtime `has` flags of the installed ccxt version
     (watchOrderBookForSymbols / watchOrderBook / watchTradesForSymbols /
     watchTrades) — a mode is only used when ccxt actually implements it;
  3. user overrides from config `feeds.exchange_overrides`.

Result per exchange: book_mode / trade_mode in {"multi", "single", "none"},
partition size, multi-call chunk size, subscribe pacing and book depth.
If a multi-symbol call fails with a symbol error, the manager falls back to
per-symbol mode for that chunk so one bad symbol cannot take the rest down.
"""
from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

# Conservative defaults. max_symbols_per_connection counts SYMBOLS (each uses
# a book and a trade subscription).
STATIC_POLICY: Dict[str, Dict[str, Any]] = {
    "binance":   {"multi": True,  "per_conn": 100, "per_call": 50, "pace_s": 0.05, "book_limit": None,
                  "notes": "diff-depth stream + REST snapshot; 1024 streams/conn exchange limit"},
    "okx":       {"multi": True,  "per_conn": 100, "per_call": 50, "pace_s": 0.10, "book_limit": None,
                  "notes": "books channel (400 levels); subscription-request rate limits"},
    "bybit":     {"multi": True,  "per_conn": 50,  "per_call": 10, "pace_s": 0.10, "book_limit": 200,
                  "notes": "spot: max 10 args per subscribe request; depth 1/50/200"},
    "coinbase":  {"multi": True,  "per_conn": 30,  "per_call": 30, "pace_s": 0.10, "book_limit": None,
                  "notes": "Advanced Trade level2 / market_trades"},
    "kraken":    {"multi": True,  "per_conn": 50,  "per_call": 50, "pace_s": 0.10, "book_limit": 100,
                  "notes": "book depth 10/25/100/500/1000"},
    "kucoin":    {"multi": True,  "per_conn": 50,  "per_call": 50, "pace_s": 0.10, "book_limit": None,
                  "notes": "topic limits per connection and per subscribe message"},
    "gate":      {"multi": True,  "per_conn": 50,  "per_call": 20, "pace_s": 0.10, "book_limit": None, "notes": ""},
    "mexc":      {"multi": False, "per_conn": 15,  "per_call": 1,  "pace_s": 0.15, "book_limit": None,
                  "requires": "google.protobuf",   # ccxt decodes MEXC's spot stream as protobuf
                  "notes": "≈30 subscriptions per connection; binary protobuf stream"},
    "bitget":    {"multi": True,  "per_conn": 50,  "per_call": 20, "pace_s": 0.10, "book_limit": None,
                  "notes": "recommended < 50 channels per connection"},
    "htx":       {"multi": False, "per_conn": 50,  "per_call": 1,  "pace_s": 0.10, "book_limit": None, "notes": "gzip frames"},
    "cryptocom": {"multi": True,  "per_conn": 50,  "per_call": 20, "pace_s": 0.10, "book_limit": None, "notes": ""},
    "upbit":     {"multi": True,  "per_conn": 50,  "per_call": 50, "pace_s": 0.10, "book_limit": None, "notes": "KRW quotes"},
    "bitfinex":  {"multi": False, "per_conn": 12,  "per_call": 1,  "pace_s": 0.20, "book_limit": 100,
                  "notes": "≈25 channels per connection"},
    "bitstamp":  {"multi": False, "per_conn": 50,  "per_call": 1,  "pace_s": 0.10, "book_limit": None, "notes": ""},
    "bitrue":    {"multi": False, "per_conn": 20,  "per_call": 1,  "pace_s": 0.20, "book_limit": None,
                  "notes": "limited ccxt websocket coverage; trades may be unavailable"},
}
# Synthetic exchanges used by `mode: "sim"` and tests (fast pacing).
for _sim in ("simex_a", "simex_b", "simex_c", "simex_d", "simex_e"):
    STATIC_POLICY[_sim] = {"multi": True, "per_conn": 50, "per_call": 20, "pace_s": 0.005, "book_limit": None,
                           "notes": "synthetic SIM exchange"}

DEFAULT_POLICY = {"multi": False, "per_conn": 20, "per_call": 1, "pace_s": 0.2, "book_limit": None,
                  "notes": "no static policy: conservative per-symbol mode"}


@dataclass
class ExchangeCaps:
    exchange: str
    book_mode: str
    trade_mode: str
    max_symbols_per_connection: int
    max_symbols_per_call: int
    subscribe_pace_s: float
    book_limit: Optional[int]
    notes: str = ""
    verified: bool = False
    runtime_has: Dict[str, Any] = field(default_factory=dict)
    missing_requirement: str = ""     # Python module the exchange's stream needs but is not installed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _truthy(v: Any) -> bool:
    return v is True or v == "emulated"


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):   # parent package missing
        return False


def resolve_caps(exchange: str, has: Optional[Dict[str, Any]], overrides: Optional[Dict[str, Any]] = None
                 ) -> ExchangeCaps:
    pol = dict(DEFAULT_POLICY)
    pol.update(STATIC_POLICY.get(exchange, {}))
    has = has or {}
    ov = (overrides or {}).get(exchange, {}) or {}

    def mode(multi_key: str, single_key: str, allow_multi: bool) -> str:
        if allow_multi and _truthy(has.get(multi_key)):
            return "multi"
        if _truthy(has.get(single_key)):
            return "single"
        return "none"

    allow_multi = bool(ov.get("multi", pol["multi"]))
    caps = ExchangeCaps(
        exchange=exchange,
        book_mode=ov.get("book_mode") or mode("watchOrderBookForSymbols", "watchOrderBook", allow_multi),
        trade_mode=ov.get("trade_mode") or mode("watchTradesForSymbols", "watchTrades", allow_multi),
        max_symbols_per_connection=int(ov.get("max_symbols_per_connection", pol["per_conn"])),
        max_symbols_per_call=int(ov.get("max_symbols_per_call", pol["per_call"])),
        subscribe_pace_s=float(ov.get("subscribe_pace_s", pol["pace_s"])),
        book_limit=ov.get("book_limit", pol["book_limit"]),
        notes=pol.get("notes", ""),
        verified=bool(ov.get("verified", False)),
        runtime_has={k: has.get(k) for k in ("watchOrderBookForSymbols", "watchOrderBook",
                                              "watchTradesForSymbols", "watchTrades", "fetchTickers")},
    )
    req = ov.get("requires", pol.get("requires"))
    if req and not module_available(req):
        # Without it ccxt's message handler raises inside its receive callback, the socket is never
        # read again and the stream only shows up as a ping-pong keepalive timeout.
        caps.missing_requirement = req
    return caps
