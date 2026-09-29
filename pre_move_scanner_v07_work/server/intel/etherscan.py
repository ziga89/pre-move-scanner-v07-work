"""Etherscan V2 (multichain) client with per-second rate limiting and a daily
call budget. Free-tier chain coverage has changed over time, so per-chain
errors (NOTOK) are reported instead of failing silently.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from ..universe.http import HttpClient, HttpError
from ..env import env_secret

BASE = "https://api.etherscan.io/v2/api"
CHAIN_IDS = {"ethereum": 1, "bsc": 56, "polygon": 137, "arbitrum": 42161, "optimism": 10, "base": 8453,
             "avalanche": 43114, "linea": 59144, "scroll": 534352, "mantle": 5000, "blast": 81457}


class EtherscanError(Exception):
    pass


class EtherscanAuthError(EtherscanError):
    pass


class EtherscanBudgetExceeded(EtherscanError):
    pass


class EtherscanClient:
    """Transfer-data provider for the EVM chains in CHAIN_IDS (see intel/providers.py)."""
    name = "etherscan"
    chains = frozenset(CHAIN_IDS)

    def __init__(self, http: HttpClient, icfg: Dict[str, Any], clock=time.time, sleep=asyncio.sleep,
                 budget_store: Optional[Dict[str, int]] = None):
        self.http = http
        self.key = env_secret(icfg.get("etherscan_api_key_env"), "ETHERSCAN_API_KEY")
        self.cps = max(0.2, float(icfg.get("calls_per_second", 4.0)))
        self.daily = int(icfg.get("daily_call_budget", 90000))
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.budget = budget_store if budget_store is not None else {}
        self.calls = 0
        self.errors = 0
        self.last_error = ""
        self.chain_errors: Dict[str, str] = {}

    @property
    def keyed(self) -> bool:
        return bool(self.key)

    def supports(self, chain: str) -> bool:
        return chain in CHAIN_IDS

    def day_key(self) -> str:
        return "budget:" + time.strftime("%Y%m%d", time.gmtime(self.clock()))

    def used_today(self) -> int:
        return int(self.budget.get(self.day_key(), 0))

    def remaining_today(self) -> int:
        return max(0, self.daily - self.used_today())

    async def _pace(self) -> None:
        async with self._lock:
            now = self.clock()
            if now < self._next:
                await self.sleep(self._next - now)
            self._next = max(now, self._next) + 1.0 / self.cps

    async def call(self, chain: str, params: Dict[str, Any]) -> Any:
        if not self.key:
            raise EtherscanAuthError("Etherscan API key missing")
        if chain not in CHAIN_IDS:
            raise EtherscanError(f"chain '{chain}' not supported by Etherscan V2 in this build")
        if self.remaining_today() <= 0:
            raise EtherscanBudgetExceeded(f"daily call budget {self.daily} used")
        await self._pace()
        self.budget[self.day_key()] = self.used_today() + 1
        self.calls += 1
        q = {"chainid": CHAIN_IDS[chain], "apikey": self.key, **params}
        try:
            data = await self.http.get_json(BASE, params=q)
        except HttpError as exc:
            self.errors += 1
            self.last_error = str(exc)
            raise EtherscanError(str(exc)) from None
        status, msg, result = str(data.get("status")), str(data.get("message", "")), data.get("result")
        if status == "1":
            self.chain_errors.pop(chain, None)
            return result
        if "no transactions found" in msg.lower() or (isinstance(result, list) and not result):
            return []
        text = f"{msg}: {result}" if isinstance(result, str) else msg
        self.errors += 1
        self.last_error = text[:200]
        low = text.lower()
        if "invalid api key" in low or "missing" in low and "key" in low:
            raise EtherscanAuthError(text)
        if "rate limit" in low:
            await self.sleep(2.0)
            raise EtherscanError(text)
        self.chain_errors[chain] = text[:200]
        raise EtherscanError(text)

    async def tokentx(self, chain: str, address: Optional[str] = None, contract: Optional[str] = None,
                      startblock: int = 0, page: int = 1, offset: int = 1000) -> List[Dict[str, Any]]:
        p: Dict[str, Any] = {"module": "account", "action": "tokentx", "startblock": startblock,
                             "endblock": 99999999, "page": page, "offset": offset, "sort": "asc"}
        if address:
            p["address"] = address
        if contract:
            p["contractaddress"] = contract
        res = await self.call(chain, p)
        return res if isinstance(res, list) else []

    async def tokenbalance(self, chain: str, contract: str, address: str) -> Optional[int]:
        res = await self.call(chain, {"module": "account", "action": "tokenbalance", "contractaddress": contract,
                                      "address": address, "tag": "latest"})
        try:
            return int(res)
        except (TypeError, ValueError):
            return None

    def stats(self) -> Dict[str, Any]:
        return {"keyed": bool(self.key), "calls": self.calls, "used_today": self.used_today(),
                "daily_budget": self.daily, "errors": self.errors, "last_error": self.last_error,
                "chain_errors": dict(self.chain_errors)}
