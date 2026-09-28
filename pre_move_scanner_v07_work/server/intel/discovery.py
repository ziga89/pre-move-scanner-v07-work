"""Automatic EVM token-contract discovery for the scanner universe.

For each universe coin, CoinGecko's coin detail says which platform the token
is *native* to (`asset_platform_id`) and its contract there (`platforms`,
`detail_platforms` with decimals). A contract is accepted only when it is safe:

* the token is native to a supported EVM chain (a bridged copy of a token that
  lives on another chain is NOT tracked: its transfers are not the asset's supply);
* the contract is a well-formed 0x address and CoinGecko's symbol matches;
* it is later verified again on-chain: the monitor rejects the contract if the
  first transfer's token symbol differs.

Native coins of their own chains (BTC, XRP, SOL, XDC, ...) and native gas
coins of EVM chains (ETH, BNB, AVAX: not ERC-20 transfers) are UNSUPPORTED with
the reason stated. Configured tokens (`intel.tokens`) always take precedence.
Results are cached in SQLite (`token_contracts`) and re-checked after
`discovery_ttl_days`. Calls are paced to `discovery_calls_per_minute`.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .providers import COINGECKO_EVM_PLATFORMS, NATIVE_EVM_COINS

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SUPPORTED, UNSUPPORTED, NOT_FOUND = "supported", "unsupported", "not_found"


def decide(asset: str, coin_id: str, detail: Dict[str, Any], supports: Callable[[str], bool],
           now: float) -> Dict[str, Any]:
    """Turn one CoinGecko coin detail into a discovery result (never guesses)."""
    base = {"asset": asset.upper(), "coingecko_id": coin_id, "chain": None, "contract": None, "decimals": None,
            "source": "coingecko", "checked_ts": now}
    sym = str(detail.get("symbol") or "").upper()
    if sym and sym != asset.upper():
        return {**base, "state": NOT_FOUND, "reason": f"CoinGecko symbol {sym} does not match {asset.upper()}"}
    pid = detail.get("asset_platform_id")
    name = detail.get("name") or coin_id
    if not pid:
        if coin_id in NATIVE_EVM_COINS:
            return {**base, "state": UNSUPPORTED,
                    "reason": f"native gas coin of {NATIVE_EVM_COINS[coin_id]}; only ERC-20 token transfers are tracked"}
        return {**base, "state": UNSUPPORTED,
                "reason": f"native coin of its own chain ({name}); only EVM tokens are supported"}
    chain = COINGECKO_EVM_PLATFORMS.get(pid)
    if chain is None:
        return {**base, "state": UNSUPPORTED, "reason": f"token lives on {pid}; only EVM chains are supported"}
    if not supports(chain):
        return {**base, "state": UNSUPPORTED, "chain": chain,
                "reason": f"no wallet-data provider configured for {chain}"}
    platforms = detail.get("platforms") or {}
    dp = (detail.get("detail_platforms") or {}).get(pid) or {}
    contract = str(platforms.get(pid) or dp.get("contract_address") or "").strip()
    if not ADDRESS.match(contract):
        return {**base, "state": NOT_FOUND, "chain": chain, "reason": f"no valid contract address on {pid}"}
    decimals = dp.get("decimal_place")
    return {**base, "state": SUPPORTED, "chain": chain, "contract": contract.lower(),
            "decimals": int(decimals) if isinstance(decimals, (int, float)) else None,
            "reason": f"native {chain} token (CoinGecko), on-chain symbol check pending"}


class ContractDiscovery:
    def __init__(self, cg: Any, icfg: Dict[str, Any], supports: Callable[[str], bool],
                 cache: Optional[Dict[str, Dict[str, Any]]] = None, clock=time.time):
        self.cg = cg
        self.cfg = icfg
        self.supports = supports
        self.clock = clock
        self.results: Dict[str, Dict[str, Any]] = dict(cache or {})
        self.pending: List[str] = []
        self.errors = 0
        self.last_error = ""
        self.last_run = 0.0
        self.calls = 0

    def result(self, asset: str) -> Optional[Dict[str, Any]]:
        return self.results.get(asset.upper())

    def _fresh(self, r: Optional[Dict[str, Any]], coin_id: str, now: float) -> bool:
        if not r or r.get("coingecko_id") != coin_id:
            return False
        ttl = float(self.cfg.get("discovery_ttl_days", 30)) * 86400.0
        if r.get("state") == "error":
            ttl = 3600.0
        return now - float(r.get("checked_ts") or 0.0) < ttl

    def queue(self, assets: Iterable[Tuple[str, str]], configured: Iterable[str] = ()) -> List[Tuple[str, str]]:
        """(asset, coingecko_id) pairs that still need a lookup, in the given (priority) order."""
        now = self.clock()
        skip = {a.upper() for a in configured}
        out, seen = [], set()
        for asset, cid in assets:
            a = asset.upper()
            if not cid or a in skip or a in seen:
                continue
            seen.add(a)
            if not self._fresh(self.results.get(a), cid, now):
                out.append((a, cid))
        self.pending = [a for a, _ in out]
        return out

    async def run_once(self, assets: Iterable[Tuple[str, str]], configured: Iterable[str] = (),
                       max_calls: int = 2) -> List[Dict[str, Any]]:
        """Look up at most `max_calls` coins; returns the new results (to persist)."""
        todo = self.queue(assets, configured)[:max(0, max_calls)]
        new = []
        for asset, cid in todo:
            now = self.clock()
            try:
                self.calls += 1
                detail = await self.cg.coin_detail(cid)
                r = decide(asset, cid, detail, self.supports, now)
            except Exception as exc:          # network / rate limit: retry after an hour
                self.errors += 1
                self.last_error = f"{cid}: {type(exc).__name__}: {exc}"[:200]
                r = {"asset": asset, "coingecko_id": cid, "state": "error", "chain": None, "contract": None,
                     "decimals": None, "reason": f"lookup failed: {type(exc).__name__}", "source": "coingecko",
                     "checked_ts": now}
            self.results[asset] = r
            new.append(r)
        self.last_run = self.clock()
        return new

    def stats(self) -> Dict[str, Any]:
        states: Dict[str, int] = {}
        for r in self.results.values():
            states[r.get("state", "?")] = states.get(r.get("state", "?"), 0) + 1
        return {"known": len(self.results), "by_state": states, "pending": len(self.pending),
                "calls": self.calls, "errors": self.errors, "last_error": self.last_error, "last_run": self.last_run}
