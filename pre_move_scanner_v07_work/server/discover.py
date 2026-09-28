from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx


CG = "https://api.coingecko.com/api/v3"

# CoinGecko market identifier/name -> display name used by the feed layer.
# The feed layer will still verify that the pair actually exists.
MARKET_ALIASES = {
    "binance": "Binance",
    "okex": "OKX",
    "okx": "OKX",
    "bybit_spot": "Bybit",
    "bybit": "Bybit",
    "gdax": "Coinbase",
    "coinbase_exchange": "Coinbase",
    "coinbase": "Coinbase",
    "gate": "Gate",
    "gateio": "Gate",
    "mexc": "MEXC",
    "mxc": "MEXC",
    "kucoin": "KuCoin",
    "kraken": "Kraken",
    "bitget": "Bitget",
    "crypto_com": "Crypto.com",
    "cryptocom": "Crypto.com",
    "bitfinex": "Bitfinex",
    "bitstamp": "Bitstamp",
    "upbit": "Upbit",
    "huobi": "HTX",
    "htx": "HTX",
    "gemini": "Gemini",
    "poloniex": "Poloniex",
    "phemex": "Phemex",
    "bithumb": "Bithumb",
}

NAME_ALIASES = {
    "BINANCE": "Binance",
    "OKX": "OKX",
    "BYBIT": "Bybit",
    "COINBASE EXCHANGE": "Coinbase",
    "COINBASE": "Coinbase",
    "GATE": "Gate",
    "GATE.IO": "Gate",
    "MEXC": "MEXC",
    "KUCOIN": "KuCoin",
    "KRAKEN": "Kraken",
    "BITGET": "Bitget",
    "CRYPTO.COM EXCHANGE": "Crypto.com",
    "CRYPTO.COM": "Crypto.com",
    "BITFINEX": "Bitfinex",
    "BITSTAMP": "Bitstamp",
    "UPBIT": "Upbit",
    "HTX": "HTX",
    "HUOBI": "HTX",
    "GEMINI": "Gemini",
    "POLONIEX": "Poloniex",
    "PHEMEX": "Phemex",
    "BITHUMB": "Bithumb",
}


class VenueDiscoverer:
    def __init__(self, root: Path, cfg: Dict[str, Any]):
        self.root = root
        self.cfg = cfg
        self.cache_path = root / "venue_discovery_cache.json"
        self.last_error = ""

    def _market_display(self, market: Dict[str, Any]) -> Optional[str]:
        ident = str(market.get("identifier") or "").lower()
        name = str(market.get("name") or "").upper()
        return MARKET_ALIASES.get(ident) or NAME_ALIASES.get(name)

    async def resolve_coin_id(self, client: httpx.AsyncClient, asset: str) -> Optional[str]:
        explicit = self.cfg.get("coingecko_ids", {}).get(asset.upper())
        if explicit:
            return explicit

        r = await client.get(f"{CG}/search", params={"query": asset})
        r.raise_for_status()
        coins = r.json().get("coins", [])
        exact = [x for x in coins if str(x.get("symbol","")).upper() == asset.upper()]
        if not exact:
            return None
        exact.sort(key=lambda x: x.get("market_cap_rank") or 10**9)
        return exact[0].get("id")

    async def discover_asset(self, client: httpx.AsyncClient, asset: str) -> Dict[str, Any]:
        asset = asset.upper()
        coin_id = await self.resolve_coin_id(client, asset)
        if not coin_id:
            return {"asset": asset, "coin_id": None, "markets": [], "raw_top": [], "error": "CoinGecko ID not found"}

        dcfg = self.cfg.get("venue_discovery", {})
        r = await client.get(
            f"{CG}/coins/{coin_id}/tickers",
            params={
                "order": "volume_desc",
                "page": 1,
                "include_exchange_logo": "false",
                "depth": "true",
            },
        )
        r.raise_for_status()
        tickers = r.json().get("tickers", [])

        # Keep one (highest-volume) pair per exchange.
        best_by_exchange = {}
        raw_top = []
        for t in tickers:
            if str(t.get("base","")).upper() != asset:
                continue
            if dcfg.get("exclude_stale", True) and t.get("is_stale"):
                continue
            if dcfg.get("exclude_anomaly", True) and t.get("is_anomaly"):
                continue

            vol_usd = float((t.get("converted_volume") or {}).get("usd") or 0)
            last = float(t.get("last") or 0)
            last_usd = float((t.get("converted_last") or {}).get("usd") or 0)
            if vol_usd <= 0 or last <= 0 or last_usd <= 0:
                continue

            market = t.get("market") or {}
            display = self._market_display(market)
            quote_to_usd = last_usd / last
            row = {
                "asset": asset,
                "coin_id": coin_id,
                "market_id": market.get("identifier"),
                "market_name": market.get("name"),
                "venue": display,
                "base": str(t.get("base")).upper(),
                "target": str(t.get("target")).upper(),
                "pair": f'{str(t.get("base")).upper()}-{str(t.get("target")).upper()}',
                "volume_24h_usd": vol_usd,
                "last_usd": last_usd,
                "quote_to_usd": quote_to_usd,
                "spread_pct": t.get("bid_ask_spread_percentage"),
                "depth_up_2_usd": t.get("cost_to_move_up_usd"),
                "depth_down_2_usd": t.get("cost_to_move_down_usd"),
                "trust_score": t.get("trust_score"),
                "last_traded_at": t.get("last_traded_at"),
            }
            raw_top.append(row)

            # Only exchanges with a known real-time adapter proceed to best_by_exchange.
            if not display:
                continue
            prev = best_by_exchange.get(display)
            if prev is None or vol_usd > prev["volume_24h_usd"]:
                best_by_exchange[display] = row

        supported = sorted(best_by_exchange.values(), key=lambda x: x["volume_24h_usd"], reverse=True)
        candidate_n = int(dcfg.get("candidate_markets", 20))
        return {
            "asset": asset,
            "coin_id": coin_id,
            "markets": supported[:candidate_n],
            "raw_top": sorted(raw_top, key=lambda x: x["volume_24h_usd"], reverse=True)[:candidate_n],
            "error": None,
        }

    async def discover(self, assets: List[str]) -> Dict[str, Any]:
        dcfg = self.cfg.get("venue_discovery", {})
        ttl = int(dcfg.get("cache_minutes", 30)) * 60

        if self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text())
                if time.time() - cached.get("ts", 0) < ttl:
                    return cached
            except Exception:
                pass

        out = {"ts": time.time(), "assets": {}, "source": "CoinGecko tickers sorted by converted USD volume"}
        try:
            async with httpx.AsyncClient(timeout=20.0, headers={"accept":"application/json"}) as client:
                for i, asset in enumerate(assets):
                    out["assets"][asset.upper()] = await self.discover_asset(client, asset)
                    # Public API rate-limit courtesy.
                    if i < len(assets)-1:
                        import asyncio
                        await asyncio.sleep(1.2)
            self.cache_path.write_text(json.dumps(out, indent=2))
            self.last_error = ""
            return out
        except Exception as exc:
            self.last_error = repr(exc)
            # Old cache is still preferable to a hard-coded wrong venue list.
            if self.cache_path.exists():
                try:
                    cached = json.loads(self.cache_path.read_text())
                    cached["stale_cache"] = True
                    cached["discovery_error"] = self.last_error
                    return cached
                except Exception:
                    pass
            return {"ts": time.time(), "assets": {}, "source": "discovery failed", "discovery_error": self.last_error}
