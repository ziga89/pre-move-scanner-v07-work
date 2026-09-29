"""Wallet-data provider interface, budget accounting and error types.

Every provider (Etherscan for EVM chains, Esplora for Bitcoin, rippled for XRPL, TronGrid, Solana
RPC, the Hedera mirror node, Koios for Cardano) implements the same small interface and returns
`RawTransfer`s. Attribution, classification and scoring are shared and live outside the providers.

Budgets are explicit per provider: calls today (persisted), a per-second pace, a daily cap, rate-limit
hits with back-off (honouring Retry-After), consecutive failures, last success and last error. A provider
never exceeds its configured daily cap: the call is refused with `ProviderBudgetExceeded` instead.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from ...env import env_secret
from ...universe.http import HttpError
from ..chains import chains_for_provider
from ..model import FetchResult, TrackedAsset


class ProviderError(Exception):
    """Temporary failure (network, 5xx, malformed answer): retried on the next cycle."""


class ProviderAuthError(ProviderError):
    """Credentials missing or rejected."""


class ProviderRateLimited(ProviderError):
    """HTTP 429 / provider rate-limit message: back off."""


class ProviderBudgetExceeded(ProviderError):
    """The configured daily call budget is used up (never silently exceeded)."""


class ProviderChainUnavailable(ProviderError):
    """The provider does not serve this chain for this account / plan (e.g. an Etherscan free-tier chain)."""


class ProviderBudget:
    def __init__(self, name: str, daily: int, per_second: float, store: Optional[Dict[str, int]] = None,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep, day_key: Optional[str] = None):
        self.name = name
        self.daily = max(0, int(daily))
        self.per_second = max(0.05, float(per_second))
        self.store = store if store is not None else {}
        self.clock = clock
        self.sleep = sleep
        self._day_key_fmt = day_key or f"budget:{name}:" + "{day}"
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.calls = 0                    # since start
        self.errors = 0
        self.rate_limit_hits = 0
        self.consecutive_failures = 0
        self.backoff_until = 0.0
        self.last_success: Optional[float] = None
        self.last_error: str = ""
        self.last_error_ts: Optional[float] = None
        self.remaining_hint: Optional[int] = None   # provider-reported remaining quota, if any

    def day_key(self) -> str:
        return self._day_key_fmt.format(day=time.strftime("%Y%m%d", time.gmtime(self.clock())))

    def used_today(self) -> int:
        return int(self.store.get(self.day_key(), 0))

    def remaining_today(self) -> int:
        return max(0, self.daily - self.used_today())

    async def acquire(self) -> None:
        """Pace one call; refuse it when the daily budget is used or a rate-limit back-off runs."""
        if self.remaining_today() <= 0:
            raise ProviderBudgetExceeded(f"{self.name}: daily call budget {self.daily} used")
        now = self.clock()
        if now < self.backoff_until:
            raise ProviderRateLimited(f"{self.name}: rate-limited, backing off {self.backoff_until - now:.0f}s")
        async with self._lock:
            now = self.clock()
            if now < self._next:
                await self.sleep(self._next - now)
            self._next = max(now, self._next) + 1.0 / self.per_second
            self.store[self.day_key()] = self.used_today() + 1
            self.calls += 1

    def ok(self) -> None:
        self.last_success = self.clock()
        self.consecutive_failures = 0

    def fail(self, msg: str) -> None:
        self.errors += 1
        self.consecutive_failures += 1
        self.last_error = str(msg)[:300]
        self.last_error_ts = self.clock()

    def rate_limited(self, retry_after: Optional[float], msg: str = "rate limited") -> None:
        self.rate_limit_hits += 1
        wait = float(retry_after) if retry_after else min(600.0, 15.0 * (2 ** min(5, self.consecutive_failures)))
        self.backoff_until = self.clock() + wait
        self.fail(f"{msg}; backing off {wait:.0f}s")

    def degraded_reason(self, max_failures: int = 3) -> Optional[str]:
        if self.remaining_today() <= 0:
            return f"daily call budget {self.daily} used"
        if self.clock() < self.backoff_until:
            return f"rate-limited ({self.last_error})"
        if self.consecutive_failures >= max_failures:
            return f"{self.consecutive_failures} consecutive failures: {self.last_error}"
        return None

    def stats(self) -> Dict[str, Any]:
        now = self.clock()
        return {"calls": self.calls, "used_today": self.used_today(), "daily_budget": self.daily,
                "remaining_today": self.remaining_today(), "remaining_reported": self.remaining_hint,
                "per_second": self.per_second, "errors": self.errors, "rate_limit_hits": self.rate_limit_hits,
                "rate_limited": now < self.backoff_until,
                "backoff_s": max(0.0, round(self.backoff_until - now, 1)),
                "consecutive_failures": self.consecutive_failures, "last_success": self.last_success,
                "last_error": self.last_error, "last_error_ts": self.last_error_ts}


class WalletProvider:
    """Base class. Subclasses set `name`, `label`, `family` and implement the fetch methods."""
    name = "base"
    label = "base"
    family = "other"
    requires_key = False
    default_daily = 10000
    default_per_second = 1.0

    def __init__(self, http: Any, pcfg: Optional[Dict[str, Any]] = None, budget_store: Optional[Dict[str, int]] = None,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep, day_key: Optional[str] = None):
        self.http = http
        self.cfg = dict(pcfg or {})
        self.clock = clock
        self.sleep = sleep
        self.enabled = bool(self.cfg.get("enabled", True))
        self.chains = frozenset(self.cfg.get("chains") or chains_for_provider(self.family))
        key_env = self.cfg.get("api_key_env")
        self.key_env = str(key_env).strip() if key_env else None
        self.key = env_secret(self.key_env, self.key_env) if self.key_env else ""
        self.budget = ProviderBudget(self.name, int(self.cfg.get("daily_call_budget", self.default_daily)),
                                     float(self.cfg.get("calls_per_second", self.default_per_second)),
                                     budget_store, clock=clock, sleep=sleep, day_key=day_key)
        self.chain_errors: Dict[str, str] = {}

    # ---------------------------------------------------------------- state
    @property
    def keyed(self) -> bool:
        return bool(self.key) or not self.requires_key

    @property
    def daily(self) -> int:
        return self.budget.daily

    def remaining_today(self) -> int:
        return self.budget.remaining_today()

    def supports(self, chain: str) -> bool:
        return self.enabled and chain in self.chains

    def state(self, chain: Optional[str] = None) -> Dict[str, Any]:
        """ok | off | no_key | degraded (with reason)."""
        if not self.enabled:
            return {"state": "off", "reason": f"provider {self.name} disabled (intel.providers.{self.family}.enabled)"}
        if not self.keyed:
            return {"state": "no_key", "reason": f"set the {self.key_env} environment variable"}
        if chain and chain in self.chain_errors:
            return {"state": "degraded", "reason": self.chain_errors[chain]}
        why = self.budget.degraded_reason()
        if why:
            return {"state": "degraded", "reason": f"{self.label}: {why}"}
        return {"state": "ok", "reason": "ok"}

    # ---------------------------------------------------------------- calls
    async def _call(self, fn, *args, **kw) -> Any:
        """One budgeted HTTP call with uniform error mapping."""
        await self.budget.acquire()
        try:
            res = await fn(*args, **kw)
        except HttpError as exc:
            if exc.status == 429:
                self.budget.rate_limited(exc.retry_after, f"HTTP 429 from {self.label}")
                raise ProviderRateLimited(str(exc)) from None
            if exc.status in (401, 403):
                self.budget.fail(str(exc))
                raise ProviderAuthError(f"{self.label}: {exc}") from None
            self.budget.fail(str(exc))
            raise ProviderError(f"{self.label}: {exc}") from None
        except (ProviderError, asyncio.CancelledError):
            raise
        except Exception as exc:                  # network errors, timeouts, bad JSON
            self.budget.fail(f"{type(exc).__name__}: {exc}")
            raise ProviderError(f"{self.label}: {type(exc).__name__}: {exc}") from None
        self.budget.ok()
        return res

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None):
        return await self._call(self.http.get_json, url, params=params, headers=headers)

    async def post(self, url: str, body: Any, headers: Optional[Dict[str, str]] = None):
        return await self._call(self.http.post_json, url, body, headers=headers)

    # ---------------------------------------------------------------- interface
    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        raise NotImplementedError

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        """New transfers of `assets` that touch `address` since `cursor`."""
        raise NotImplementedError

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        raise NotImplementedError

    async def fetch_token(self, chain: str, asset: TrackedAsset, cursor: Dict[str, Any]) -> FetchResult:
        """Token-wide transfers (all holders). Only where it is manageable (EVM)."""
        raise NotImplementedError(f"{self.name}: token-wide polling not supported")

    def supports_token_wide(self) -> bool:
        return False

    def assets_per_call(self) -> str:
        """'chain': one address call covers every tracked asset on the chain; 'asset': one call per asset."""
        return "asset"

    def stats(self) -> Dict[str, Any]:
        st = self.state()
        return {"name": self.name, "label": self.label, "family": self.family, "enabled": self.enabled,
                "requires_key": self.requires_key, "keyed": self.keyed, "key_env": self.key_env,
                "chains": sorted(self.chains), "state": st["state"], "reason": st["reason"],
                "chain_errors": dict(self.chain_errors), **self.budget.stats()}


def first(items: Iterable[Any], default: Any = None) -> Any:
    for x in items:
        return x
    return default
