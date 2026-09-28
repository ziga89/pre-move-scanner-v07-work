"""Per-coin venue selection.

For EACH coin separately: find where *that coin* has the most real spot
volume, verify the exact pair exists on the exchange, and pick the best 3–5
usable markets. Global exchange size plays no role — XDC never gets Binance
unless Binance actually lists XDC.

Filters (each rejection keeps its reason for the UI):
  * spot + active market with a 24h ticker
  * quote convertible to USD (live FX)
  * identity: exchange price within ±5 % of CoinGecko's USD price (±12 % for
    KRW) — rejects a *different* token that shares the ticker symbol
  * spread ≤ max_spread_pct, ticker not stale, volume ≥ minimum
  * not marked unavailable by the feed layer (verification timeout, wash
    volume vs visible depth, subscription errors)
One best pair per exchange; replacement of an existing venue needs
`replace_ratio`× its volume on `replace_confirmations` consecutive refreshes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .catalog import Catalog
from .fx import FxService

# CoinGecko market identifiers -> our exchange ids (for the cross-check report)
COINGECKO_MARKET_IDS = {
    "binance": "binance", "okex": "okx", "okx": "okx", "bybit_spot": "bybit", "gdax": "coinbase",
    "coinbase_exchange": "coinbase", "kraken": "kraken", "kucoin": "kucoin", "gate": "gate", "mxc": "mexc",
    "mexc": "mexc", "bitget": "bitget", "huobi": "htx", "htx": "htx", "crypto_com": "cryptocom",
    "upbit": "upbit", "bitfinex": "bitfinex", "bitstamp": "bitstamp", "bitrue": "bitrue", "bithumb": "bithumb",
}


@dataclass
class AssetInfo:
    symbol: str
    coin_id: str = ""
    name: str = ""
    rank: Optional[int] = None
    price_usd: Optional[float] = None
    market_cap: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    pinned: bool = False

    @classmethod
    def from_row(cls, r: Dict[str, Any], pinned: bool = False) -> "AssetInfo":
        return cls(symbol=str(r.get("symbol", "")).upper(), coin_id=str(r.get("id", "")), name=str(r.get("name", "")),
                   rank=r.get("market_cap_rank"), price_usd=r.get("current_price"), market_cap=r.get("market_cap"),
                   volume_24h_usd=r.get("total_volume"), pinned=pinned)


@dataclass
class Selection:
    asset: str
    selected: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    ts: float = 0.0

    @property
    def usable(self) -> bool:
        return bool(self.selected)

    def total_volume(self) -> float:
        return sum(m["volume_24h_usd"] for m in self.selected)

    def to_dict(self) -> Dict[str, Any]:
        return {"asset": self.asset, "ts": self.ts, "selected": self.selected, "candidates": self.candidates[:12],
                "rejected": self.rejected[:40], "total_volume_24h_usd": self.total_volume()}


class VenueSelector:
    def __init__(self, dcfg: Dict[str, Any]):
        self.cfg = dcfg
        self.current: Dict[str, List[Tuple[str, str]]] = {}         # asset -> [(exchange, symbol)]
        self.challengers: Dict[str, Dict[Tuple[str, str], int]] = {}  # asset -> {(ex,sym): consecutive wins}
        self.unavailable: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (ex,sym) -> {reason, until}

    # ---- feed feedback
    def mark_unavailable(self, exchange: str, symbol: str, reason: str, ttl_s: float = 6 * 3600,
                         now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.unavailable[(exchange, symbol)] = {"reason": reason, "until": now + ttl_s}

    def _is_unavailable(self, key: Tuple[str, str], now: float) -> Optional[str]:
        u = self.unavailable.get(key)
        if not u:
            return None
        if u["until"] < now:
            del self.unavailable[key]
            return None
        return u["reason"]

    # ---- candidate ranking (pure)
    def candidates(self, asset: AssetInfo, catalogs: Iterable[Catalog], fx: FxService, now: float
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        c = self.cfg
        aliases = {k.upper(): v for k, v in (c.get("symbol_aliases") or {}).items()}
        excluded_ex = set(c.get("excluded_exchanges") or [])
        tol = float(c.get("price_tolerance_pct", 5.0))
        tol_krw = float(c.get("price_tolerance_krw_pct", 12.0))
        max_spread = float(c.get("max_spread_pct", 1.0))
        min_vol = float(c.get("min_market_volume_usd", 50000))
        stale_s = float(c.get("stale_ticker_minutes", 120)) * 60.0
        best: Dict[str, Dict[str, Any]] = {}
        rejected: List[Dict[str, Any]] = []
        for cat in catalogs:
            ex = cat.exchange
            if ex in excluded_ex or cat.error:
                continue
            base = (aliases.get(asset.symbol) or {}).get(ex, asset.symbol).upper()
            for sym in cat.by_base.get(base, []):
                m = cat.markets[sym]

                def rej(reason, **kw):
                    rejected.append({"exchange": ex, "symbol": sym, "reason": reason, **kw})
                if not m.get("active", True):
                    rej("market inactive")
                    continue
                t = cat.tickers.get(sym)
                if not t or not t.get("last"):
                    rej("no 24h ticker")
                    continue
                q = str(m.get("quote", "")).upper()
                rate = fx.rate(q)
                if not rate:
                    rej(f"quote {q} not convertible to USD")
                    continue
                last = float(t["last"])
                price_usd = last * rate
                qv = t.get("quoteVolume")
                vol_usd = float(qv) * rate if qv else float(t.get("baseVolume") or 0.0) * price_usd
                if asset.price_usd:
                    dev = abs(price_usd / float(asset.price_usd) - 1.0) * 100.0
                    if dev > (tol_krw if q == "KRW" else tol):
                        rej("price mismatch: likely a different token with the same ticker",
                            price_usd=round(price_usd, 8), reference_usd=asset.price_usd)
                        continue
                bid, ask = t.get("bid"), t.get("ask")
                spread_pct = None
                if bid and ask and ask > bid:
                    spread_pct = (ask - bid) / ((ask + bid) / 2.0) * 100.0
                    if spread_pct > max_spread:
                        rej(f"spread {spread_pct:.2f}% > {max_spread}%")
                        continue
                ts = t.get("timestamp")
                if ts and now - float(ts) > stale_s:
                    rej("stale ticker")
                    continue
                if vol_usd < min_vol:
                    rej(f"24h volume ${vol_usd:,.0f} below minimum", volume_24h_usd=round(vol_usd, 2))
                    continue
                why = self._is_unavailable((ex, sym), now)
                if why:
                    rej(f"realtime unavailable: {why}")
                    continue
                row = {"exchange": ex, "symbol": sym, "base": base, "quote": q, "volume_24h_usd": round(vol_usd, 2),
                       "price_usd": price_usd, "spread_pct": spread_pct, "fx": rate}
                if ex not in best or vol_usd > best[ex]["volume_24h_usd"]:
                    if ex in best:
                        rejected.append({"exchange": ex, "symbol": best[ex]["symbol"],
                                         "reason": "lower-volume pair on same exchange"})
                    best[ex] = row
                else:
                    rej("lower-volume pair on same exchange")
        ranked = sorted(best.values(), key=lambda r: -r["volume_24h_usd"])
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
        return ranked, rejected

    def preview(self, asset: AssetInfo, catalogs: Iterable[Catalog], fx: FxService, now: float) -> Selection:
        """Selection without touching hysteresis state (used for universe usability)."""
        ranked, rejected = self.candidates(asset, catalogs, fx, now)
        n = int(self.cfg.get("max_venues_per_asset", 5))
        return Selection(asset.symbol, ranked[:n], ranked, rejected, now)

    def select(self, asset: AssetInfo, catalogs: Iterable[Catalog], fx: FxService, now: float) -> Selection:
        ranked, rejected = self.candidates(asset, catalogs, fx, now)
        n = int(self.cfg.get("max_venues_per_asset", 5))
        ratio = float(self.cfg.get("replace_ratio", 1.5))
        need = int(self.cfg.get("replace_confirmations", 2))
        by_key = {(r["exchange"], r["symbol"]): r for r in ranked}
        cur = [k for k in self.current.get(asset.symbol, []) if k in by_key]
        chal = self.challengers.setdefault(asset.symbol, {})
        if not cur:
            chosen = [(r["exchange"], r["symbol"]) for r in ranked[:n]]
            chal.clear()
        else:
            chosen = list(cur)
            for r in ranked:  # fill free slots immediately
                k = (r["exchange"], r["symbol"])
                if len(chosen) >= n:
                    break
                if k not in chosen and r["exchange"] not in {c[0] for c in chosen}:
                    chosen.append(k)
            wins = {}
            for r in ranked:
                k = (r["exchange"], r["symbol"])
                if k in chosen or r["exchange"] in {c[0] for c in chosen}:
                    continue
                weakest = min(chosen, key=lambda c: by_key[c]["volume_24h_usd"]) if chosen else None
                if weakest and r["volume_24h_usd"] >= ratio * by_key[weakest]["volume_24h_usd"]:
                    wins[k] = chal.get(k, 0) + 1
                    if wins[k] >= need:
                        chosen.remove(weakest)
                        chosen.append(k)
                        wins.pop(k)
            self.challengers[asset.symbol] = wins
        chosen_rows = sorted((by_key[k] for k in chosen), key=lambda r: -r["volume_24h_usd"])[:n]
        self.current[asset.symbol] = [(r["exchange"], r["symbol"]) for r in chosen_rows]
        return Selection(asset.symbol, chosen_rows, ranked, rejected, now)

    def forget(self, asset: str) -> None:
        self.current.pop(asset, None)
        self.challengers.pop(asset, None)


def crosscheck_unsupported(tickers: List[Dict[str, Any]], supported: Set[str], top: int = 10) -> List[Dict[str, Any]]:
    """Top CoinGecko markets on exchanges without a realtime adapter (transparency only)."""
    out = []
    for t in tickers[:40]:
        ident = str((t.get("market") or {}).get("identifier") or "").lower()
        ex = COINGECKO_MARKET_IDS.get(ident)
        if ex and ex in supported:
            continue
        vol = float((t.get("converted_volume") or {}).get("usd") or 0.0)
        out.append({"market": (t.get("market") or {}).get("name"), "identifier": ident,
                    "pair": f"{t.get('base')}/{t.get('target')}", "volume_24h_usd": vol,
                    "reason": "exchange not supported by the realtime adapter" if not ex else "exchange disabled"})
        if len(out) >= top:
            break
    return out
