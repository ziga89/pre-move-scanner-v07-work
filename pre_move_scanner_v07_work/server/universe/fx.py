"""Quote-currency → USD conversion.

* USD: 1. Stablecoin quotes: CoinGecko price when known (depeg-aware), else 1.
* Crypto quotes (BTC, ETH, BNB, SOL, …): CoinGecko USD price, then updated
  live from the scanner's own composite prices for those assets.
* Fiat / other quotes (EUR, KRW, TRY, …): derived from the most liquid
  BTC/<quote> (or ETH/<quote>) pair across the loaded catalogs.

Note: KRW derived via BTC/KRW embeds the Korean premium; this slightly skews
Upbit/Bithumb USD volume (a few %) and is documented as a known limitation.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .catalog import Catalog

STABLE_QUOTES = {"USDT", "USDC", "FDUSD", "USD1", "TUSD", "DAI", "USDE", "PYUSD", "RLUSD", "USDS",
                 "BUSD", "USDP", "USDD", "USDG", "EURC"}
CRYPTO_QUOTES = {"BTC", "ETH", "BNB", "SOL", "TRX", "XRP", "DOGE"}
SYMBOL_TO_ID = {"USDT": "tether", "USDC": "usd-coin", "FDUSD": "first-digital-usd", "DAI": "dai",
                "TUSD": "true-usd", "USDE": "ethena-usde", "PYUSD": "paypal-usd", "USD1": "usd1-wlfi",
                "RLUSD": "ripple-usd", "USDS": "usds", "EURC": "euro-coin"}


class FxService:
    def __init__(self):
        self.rates: Dict[str, float] = {"USD": 1.0}
        self.sources: Dict[str, str] = {"USD": "fixed"}

    def rate(self, quote: str) -> Optional[float]:
        q = str(quote).upper()
        if q in self.rates:
            return self.rates[q]
        if q in STABLE_QUOTES:
            return 1.0
        return None

    __call__ = rate

    def update_from_markets(self, rows: Iterable[Dict[str, Any]]) -> None:
        by_sym = {}
        for r in rows:
            s = str(r.get("symbol", "")).upper()
            if s and s not in by_sym and r.get("current_price"):
                by_sym[s] = float(r["current_price"])
        for q in STABLE_QUOTES:
            if q in by_sym and 0.9 <= by_sym[q] <= 1.1:
                self.rates[q] = by_sym[q]
                self.sources[q] = "coingecko"
        for q in CRYPTO_QUOTES:
            if q in by_sym:
                self.rates[q] = by_sym[q]
                self.sources[q] = "coingecko"

    def update_live(self, asset: str, price_usd: Optional[float]) -> None:
        if price_usd and asset.upper() in CRYPTO_QUOTES:
            self.rates[asset.upper()] = float(price_usd)
            self.sources[asset.upper()] = "live composite"

    def update_from_catalogs(self, catalogs: Iterable[Catalog]) -> None:
        """Derive other quote currencies via the most liquid BTC/<Q> or ETH/<Q> pair."""
        best: Dict[str, tuple] = {}
        for cat in catalogs:
            for sym, m in cat.markets.items():
                base, quote = m.get("base"), m.get("quote")
                if base not in ("BTC", "ETH") or quote in STABLE_QUOTES or quote in ("USD",) or quote in CRYPTO_QUOTES:
                    continue
                t = cat.tickers.get(sym) or {}
                last, qv = t.get("last"), t.get("quoteVolume") or 0.0
                ref = self.rates.get(base)
                if not last or not ref:
                    continue
                if quote not in best or qv > best[quote][0]:
                    best[quote] = (qv, ref / float(last), f"{cat.exchange} {sym}")
        for q, (_, r, src) in best.items():
            self.rates[q] = r
            self.sources[q] = src

    def snapshot(self) -> List[Dict[str, Any]]:
        return [{"quote": q, "usd": round(r, 8), "source": self.sources.get(q, "")} for q, r in sorted(self.rates.items())]
