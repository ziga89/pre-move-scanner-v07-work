"""Persistent asset registry: chain / platform / contract metadata and the wallet provider of every
monitored asset (Top-100 and manual assets alike).

For each asset CoinGecko's coin detail is read once (paced, cached in SQLite `asset_registry`,
re-checked after `discovery_ttl_days`; failed lookups retried after an hour) and turned into a
decision that never guesses:

* a native coin of a known chain (BTC, ETH, BNB, SOL, XRP, TRX, XDC, HBAR, ADA, AVAX, ...) - curated
  map `chains.BY_NATIVE_COIN`; no contract;
* a token on the platform CoinGecko names as its *native* platform (`asset_platform_id`), with a
  contract that is well-formed for that chain family; bridged copies elsewhere are not tracked;
* a coin with token platforms but no native platform is ambiguous: NEEDS_VERIFICATION (set an
  override), nothing is tracked;
* anything on a chain without a provider: UNSUPPORTED with "provider not implemented".

Overrides (config `intel.tokens` / `assets.overrides`, or stored with the entry) always win and are
marked verified. A discovered token contract is verified on-chain by the monitor (the first transfer's
token symbol must match) before it can produce scores.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .addr import normalize_token
from .chains import BY_NATIVE_COIN, BY_PLATFORM, CHAINS, chain_name
from .model import TrackedAsset

PENDING, READY, UNSUPPORTED, NOT_FOUND, NEEDS_VERIFICATION, ERROR = (
    "PENDING", "READY", "UNSUPPORTED", "NOT_FOUND", "NEEDS_VERIFICATION", "ERROR")
REGISTRY_COLS = ["symbol", "name", "coingecko_id", "market_cap_rank", "manual", "native_chain", "token_platform",
                 "contract_address", "native_asset", "wallet_provider", "wallet_supported", "discovery_confidence",
                 "verified", "last_metadata_check", "created_at", "updated_at", "state", "reason", "decimals",
                 "platforms", "override", "source"]


def _provider_for(chain_id: Optional[str], providers: Any) -> Optional[str]:
    if not chain_id or providers is None:
        return None
    p = providers.assigned(chain_id) if hasattr(providers, "assigned") else providers.for_chain(chain_id)
    return p.name if p is not None else None


def decide(symbol: str, coin_id: str, detail: Dict[str, Any], providers: Any, now: float) -> Dict[str, Any]:
    """One CoinGecko coin detail -> registry decision (pure; never guesses)."""
    sym = symbol.upper()
    base = {"symbol": sym, "coingecko_id": coin_id, "name": detail.get("name") or None,
            "market_cap_rank": detail.get("market_cap_rank"), "native_chain": None, "token_platform": None,
            "contract_address": None, "native_asset": 0, "wallet_provider": None, "wallet_supported": 0,
            "discovery_confidence": "none", "verified": 0, "decimals": None, "last_metadata_check": now,
            "source": "coingecko",
            "platforms": json.dumps({k: v for k, v in (detail.get("platforms") or {}).items() if k and v}, sort_keys=True)}
    cg_sym = str(detail.get("symbol") or "").upper()
    if cg_sym and cg_sym != sym:
        return {**base, "state": NOT_FOUND, "reason": f"CoinGecko symbol {cg_sym} does not match {sym}"}
    name = detail.get("name") or coin_id
    c = BY_NATIVE_COIN.get(coin_id)
    if c is not None:
        prov = _provider_for(c.id, providers) if c.provider else None
        row = {**base, "native_chain": c.id, "native_asset": 1, "decimals": c.native_decimals,
               "discovery_confidence": "high", "verified": 1, "wallet_provider": prov}
        if prov is None:
            return {**row, "state": UNSUPPORTED, "reason": f"native coin of {c.name} · provider not implemented"}
        return {**row, "state": READY, "wallet_supported": 1, "reason": f"native coin of {c.name}"}
    pid = detail.get("asset_platform_id")
    if pid:
        c = BY_PLATFORM.get(pid)
        if c is None:
            return {**base, "token_platform": pid, "state": UNSUPPORTED,
                    "reason": f"token on '{pid}' · provider not implemented"}
        platforms = detail.get("platforms") or {}
        dp = (detail.get("detail_platforms") or {}).get(pid) or {}
        raw = str(platforms.get(pid) or dp.get("contract_address") or "").strip()
        contract = normalize_token(c.family, raw) if c.family != "other" else (raw or None)
        dec = dp.get("decimal_place")
        row = {**base, "native_chain": c.id, "token_platform": pid, "contract_address": contract,
               "decimals": int(dec) if isinstance(dec, (int, float)) else None}
        if c.provider is None:
            return {**row, "state": UNSUPPORTED, "reason": f"{c.name} token · provider not implemented"}
        if not contract:
            return {**row, "state": NOT_FOUND, "reason": f"no valid {c.name} contract on CoinGecko ({raw or 'empty'})"}
        return {**row, "state": READY, "discovery_confidence": "high", "wallet_provider": _provider_for(c.id, providers),
                "wallet_supported": 1,
                "reason": f"native {c.name} token (CoinGecko asset platform); on-chain symbol check pending"}
    listed = {k: v for k, v in (detail.get("platforms") or {}).items() if k and v}
    if not listed:
        return {**base, "state": UNSUPPORTED,
                "reason": f"native coin of its own chain ({name}) · provider not implemented"}
    names = ", ".join(sorted(chain_name(BY_PLATFORM[k].id) if k in BY_PLATFORM else k for k in listed))
    return {**base, "state": NEEDS_VERIFICATION, "discovery_confidence": "low",
            "reason": f"CoinGecko lists token platforms ({names}) but no native platform - "
                      "set a manual override (never guessed)"}


def override_decision(symbol: str, spec: Dict[str, Any], providers: Any, now: float,
                      prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A manual override (config or stored): {chain, contract|native, decimals} or {unsupported: reason}."""
    prev = prev or {}
    sym = symbol.upper()
    base = {k: prev.get(k) for k in REGISTRY_COLS}
    base.update(symbol=sym, source="override", discovery_confidence="override", verified=1,
                last_metadata_check=now, override=json.dumps(spec, sort_keys=True))
    if spec.get("unsupported"):
        return {**base, "state": UNSUPPORTED, "wallet_supported": 0, "wallet_provider": None,
                "reason": f"override: {spec['unsupported']}"}
    cid = str(spec.get("chain") or "ethereum").lower()
    c = CHAINS.get(cid)
    if c is None:
        return {**base, "state": UNSUPPORTED, "native_chain": cid, "wallet_supported": 0,
                "reason": f"override: unknown chain '{cid}'"}
    native = bool(spec.get("native"))
    contract = None
    if not native:
        contract = normalize_token(c.family, str(spec.get("contract") or "")) if c.family != "other" else spec.get("contract")
        if not contract:
            return {**base, "state": NOT_FOUND, "native_chain": cid, "wallet_supported": 0,
                    "reason": f"override: '{spec.get('contract')}' is not a valid {c.name} token identifier"}
    prov = _provider_for(cid, providers) if c.provider else None
    row = {**base, "native_chain": cid, "native_asset": 1 if native else 0, "contract_address": contract,
           "token_platform": None if native else c.cg_platform,
           "decimals": spec.get("decimals") if spec.get("decimals") is not None else (c.native_decimals if native else None),
           "wallet_provider": prov}
    if prov is None:
        return {**row, "state": UNSUPPORTED, "wallet_supported": 0, "reason": f"override: {c.name} · provider not implemented"}
    return {**row, "state": READY, "wallet_supported": 1,
            "reason": f"override: {'native coin' if native else 'token'} on {c.name}"}


class AssetRegistry:
    def __init__(self, cg: Any, cfg: Dict[str, Any], providers: Any, cache: Optional[Dict[str, Dict[str, Any]]] = None,
                 overrides: Optional[Dict[str, Dict[str, Any]]] = None, clock: Callable[[], float] = time.time):
        self.cg = cg
        self.cfg = cfg
        self.providers = providers
        self.clock = clock
        self.entries: Dict[str, Dict[str, Any]] = {k.upper(): dict(v) for k, v in (cache or {}).items()}
        self.overrides = {k.upper(): dict(v) for k, v in (overrides or {}).items()}
        self.pending: List[str] = []
        self.calls = 0
        self.errors = 0
        self.last_error = ""
        self.last_run = 0.0
        self._dirty: Dict[str, Dict[str, Any]] = {}
        now = clock()
        for sym, spec in self.overrides.items():
            self._put(override_decision(sym, spec, providers, now, self.entries.get(sym)))

    # ---------------------------------------------------------------- entries
    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.entries.get(symbol.upper())

    def _put(self, row: Dict[str, Any]) -> Dict[str, Any]:
        now = self.clock()
        sym = row["symbol"].upper()
        old = self.entries.get(sym) or {}
        merged = {**{k: None for k in REGISTRY_COLS}, **old, **row}
        merged["created_at"] = old.get("created_at") or now
        merged["updated_at"] = now
        for k in ("manual",):
            if row.get(k) is None and old.get(k) is not None:
                merged[k] = old[k]
        self.entries[sym] = merged
        self._dirty[sym] = merged
        return merged

    def note_member(self, symbol: str, coin_id: str, name: Optional[str], rank: Optional[int], manual: bool) -> None:
        """Keep universe metadata (name, rank, manual flag) current; new assets start PENDING."""
        sym = symbol.upper()
        e = self.entries.get(sym)
        if e is None:
            self._put({"symbol": sym, "coingecko_id": coin_id, "name": name, "market_cap_rank": rank,
                       "manual": 1 if manual else 0, "state": PENDING, "source": "coingecko",
                       "reason": "looking up chain / platform / contract (CoinGecko)"})
            return
        changes = {}
        if name and e.get("name") != name:
            changes["name"] = name
        if rank is not None and e.get("market_cap_rank") != rank:
            changes["market_cap_rank"] = rank
        if int(e.get("manual") or 0) != (1 if manual else 0):
            changes["manual"] = 1 if manual else 0
        if coin_id and e.get("coingecko_id") != coin_id and e.get("source") != "override":
            changes.update(coingecko_id=coin_id, state=PENDING, reason="CoinGecko id changed; re-checking")
        if changes:
            self._put({"symbol": sym, **changes})

    def set_override(self, symbol: str, spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        sym = symbol.upper()
        if not spec:
            self.overrides.pop(sym, None)
            return self._put({"symbol": sym, "state": PENDING, "source": "coingecko", "override": None,
                              "verified": 0, "reason": "override removed; re-checking"})
        self.overrides[sym] = dict(spec)
        return self._put(override_decision(sym, spec, self.providers, self.clock(), self.entries.get(sym)))

    def mark_verified(self, symbol: str, verified: bool, reason: str) -> None:
        e = self.entries.get(symbol.upper())
        if e is None or (int(e.get("verified") or 0) == (1 if verified else 0) and e.get("reason") == reason):
            return
        row = {"symbol": symbol.upper(), "verified": 1 if verified else 0, "reason": reason}
        if not verified:
            row.update(state=NOT_FOUND, wallet_supported=0)
        self._put(row)

    def pop_dirty(self) -> List[Dict[str, Any]]:
        rows = list(self._dirty.values())
        self._dirty.clear()
        return rows

    # ---------------------------------------------------------------- discovery
    def _fresh(self, e: Optional[Dict[str, Any]], coin_id: str, now: float) -> bool:
        if not e or e.get("state") in (None, PENDING):
            return False
        if e.get("source") == "override":
            return True
        if e.get("coingecko_id") != coin_id:
            return False
        ttl = float(self.cfg.get("discovery_ttl_days", 30)) * 86400.0
        if e.get("state") == ERROR:
            ttl = 3600.0
        return now - float(e.get("last_metadata_check") or 0.0) < ttl

    def queue(self, assets: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        now = self.clock()
        out, seen = [], set()
        for sym, cid in assets:
            s = sym.upper()
            if not cid or s in seen or s in self.overrides:
                continue
            seen.add(s)
            if not self._fresh(self.entries.get(s), cid, now):
                out.append((s, cid))
        self.pending = [s for s, _ in out]
        return out

    async def lookup(self, symbol: str, coin_id: str) -> Dict[str, Any]:
        now = self.clock()
        self.calls += 1
        try:
            detail = await self.cg.coin_detail(coin_id)
            row = decide(symbol, coin_id, detail, self.providers, now)
        except Exception as exc:              # network / rate limit: retried after an hour
            self.errors += 1
            self.last_error = f"{coin_id}: {type(exc).__name__}: {exc}"[:200]
            prev = self.entries.get(symbol.upper()) or {}
            if prev.get("state") == READY and prev.get("coingecko_id") == coin_id:
                # keep a good decision; just retry the refresh later
                return self._put({"symbol": symbol.upper(), "last_metadata_check": now - float(
                    self.cfg.get("discovery_ttl_days", 30)) * 86400.0 + 3600.0})
            row = {"symbol": symbol.upper(), "coingecko_id": coin_id, "state": ERROR,
                   "reason": f"metadata lookup failed ({type(exc).__name__}); retrying within an hour",
                   "last_metadata_check": now, "source": "coingecko"}
        return self._put(row)

    async def run_once(self, assets: Iterable[Tuple[str, str]], max_calls: int = 2) -> List[Dict[str, Any]]:
        todo = self.queue(assets)[:max(0, int(max_calls))]
        new = [await self.lookup(s, cid) for s, cid in todo]
        self.last_run = self.clock()
        return new

    # ---------------------------------------------------------------- views
    def tracked(self, config_tokens: Optional[Dict[str, Any]] = None) -> List[TrackedAsset]:
        out = []
        for sym, e in self.entries.items():
            if e.get("state") != READY or not e.get("native_chain"):
                continue
            src = e.get("source") or "discovered"
            spec = (config_tokens or {}).get(sym) or {}
            out.append(TrackedAsset(
                asset=sym, chain=e["native_chain"], native=bool(e.get("native_asset")),
                token=e.get("contract_address"), decimals=e.get("decimals"), provider=e.get("wallet_provider"),
                source="config" if src == "override" and sym in (config_tokens or {}) else
                ("override" if src == "override" else "discovered"),
                status="ok" if int(e.get("verified") or 0) else "pending",
                verified_symbol=sym if int(e.get("verified") or 0) else None,
                token_wide=str(spec.get("token_wide", self.cfg.get("discovered_token_wide", "off"))).lower()))
        return out

    def stats(self) -> Dict[str, Any]:
        by: Dict[str, int] = {}
        for e in self.entries.values():
            by[e.get("state") or "?"] = by.get(e.get("state") or "?", 0) + 1
        return {"known": len(self.entries), "by_state": by, "pending": len(self.pending), "calls": self.calls,
                "errors": self.errors, "last_error": self.last_error, "last_run": self.last_run,
                "overrides": sorted(self.overrides)}

    def public(self, symbol: str) -> Optional[Dict[str, Any]]:
        e = self.get(symbol)
        if e is None:
            return None
        d = dict(e)
        try:
            d["platforms"] = json.loads(d.get("platforms") or "{}")
        except ValueError:
            d["platforms"] = {}
        try:
            d["override"] = json.loads(d["override"]) if d.get("override") else None
        except ValueError:
            d["override"] = None
        for k in ("manual", "native_asset", "wallet_supported", "verified"):
            d[k] = bool(d.get(k))
        c = CHAINS.get(d.get("native_chain") or "")
        d["chain_name"] = c.name if c else d.get("native_chain")
        return d
