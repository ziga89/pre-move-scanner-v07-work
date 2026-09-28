"""Rate-limited CoinGecko client (free / Demo / Pro keys).

Budget: the universe needs ~2 calls/hour (top 500 by market cap), exclusion
categories ~8 calls/day and optional per-coin ticker cross-checks (1/day per
coin, spread out). A token bucket keeps calls under `calls_per_minute`, and
HTTP 429 triggers exponential backoff honouring Retry-After.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from .http import HttpClient, HttpError

PUBLIC = "https://api.coingecko.com/api/v3"
PRO = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoClient:
    def __init__(self, http: HttpClient, ucfg: Dict[str, Any], clock=time.monotonic, sleep=asyncio.sleep):
        self.http = http
        key = os.getenv(str(ucfg.get("coingecko_api_key_env", "COINGECKO_API_KEY")), "")
        self.key_type = str(ucfg.get("coingecko_api_key_type", "demo")).lower()
        self.base = PRO if (key and self.key_type == "pro") else PUBLIC
        self.headers: Dict[str, str] = {}
        if key:
            self.headers["x-cg-pro-api-key" if self.key_type == "pro" else "x-cg-demo-api-key"] = key
        self.per_min = max(1.0, float(ucfg.get("coingecko_calls_per_minute", 8)))
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.per_min
        self._last = clock()
        self._lock = asyncio.Lock()
        self.calls = 0
        self.rate_limited = 0
        self.errors = 0
        self.last_error = ""
        self.backoff_until = 0.0

    async def _acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if now < self.backoff_until:
                    await self._sleep(self.backoff_until - now)
                    continue
                self._tokens = min(self.per_min, self._tokens + (now - self._last) * self.per_min / 60.0)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) * 60.0 / self.per_min)

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 4) -> Any:
        delay = 15.0
        for attempt in range(retries + 1):
            await self._acquire()
            self.calls += 1
            try:
                return await self.http.get_json(self.base + path, params=params, headers=self.headers)
            except HttpError as exc:
                if exc.status == 429:
                    self.rate_limited += 1
                    wait = exc.retry_after or delay
                    self.backoff_until = self._clock() + wait
                    delay = min(delay * 2, 300.0)
                    self.last_error = f"429 rate limited; backing off {wait:.0f}s"
                    continue
                self.errors += 1
                self.last_error = str(exc)
                if exc.status >= 500 and attempt < retries:
                    await self._sleep(min(delay, 60.0))
                    delay *= 2
                    continue
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = repr(exc)
                if attempt < retries:
                    await self._sleep(min(delay, 60.0))
                    delay *= 2
                    continue
                raise
        raise RuntimeError(f"CoinGecko {path}: retries exhausted ({self.last_error})")

    async def markets(self, page: int = 1, per_page: int = 250, category: Optional[str] = None,
                      ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page,
                                  "page": page, "sparkline": "false", "price_change_percentage": "1h,24h"}
        if category:
            params["category"] = category
        if ids:
            params["ids"] = ",".join(ids)
        data = await self.get("/coins/markets", params)
        return data if isinstance(data, list) else []

    async def coin_tickers(self, coin_id: str) -> List[Dict[str, Any]]:
        data = await self.get(f"/coins/{coin_id}/tickers", {"order": "volume_desc", "page": 1,
                                                             "include_exchange_logo": "false", "depth": "true"})
        return (data or {}).get("tickers", []) if isinstance(data, dict) else []

    def stats(self) -> Dict[str, Any]:
        return {"base": self.base, "keyed": bool(self.headers), "calls": self.calls,
                "rate_limited": self.rate_limited, "errors": self.errors, "last_error": self.last_error}
