"""Multi-chain, address-centric wallet monitor (v0.8).

Every *reliable* labelled exchange / market-maker / custody / whale / treasury address is polled on
its chain through that chain's provider. One address poll covers every tracked asset on the chain.
The provider returns `RawTransfer`s; this module does everything chain-independent:

* on-chain symbol verification of discovered token contracts (first transfer's symbol must match),
* de-duplication by (chain, tx hash, index),
* attribution of both sides with the label registry and classification into the normalised
  event types (`classify.py`) -> `WalletEvent` rows,
* running balances, large-transfer timeline events, persistence.

Scheduling is budget-aware per provider (spec: "never silently exceed provider limits"). Each asset
gets a priority tier from the service - 1 current anomaly, 2 radar WATCH / CONFIRMING, 3 manual asset,
4 higher-ranked, 5 quiet - and an address is polled at the interval of its most urgent asset:
tiers 1-2 at the base interval, 3 at 2x, 4 at 4x, 5 at 8x. The base interval is derived from the
provider's remaining daily budget (70 % for address polls, 20 % balances, 10 % token-wide).

Coverage is honest: an address whose new data exceeds what one poll can page through is `lagging`
(scores are damped and marked partial); a provider that keeps failing, is rate-limited or out of
budget makes its assets DEGRADED.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .addr import normalize_token
from .chains import CHAINS
from .classify import classify
from .labels import LabelRegistry
from .model import NATIVE, RawTransfer, TrackedAsset, WalletEvent
from .providers.base import (ProviderAuthError, ProviderBudgetExceeded, ProviderChainUnavailable, ProviderError,
                             ProviderRateLimited)

TIER_MULT = {1: 1.0, 2: 1.0, 3: 2.0, 4: 4.0, 5: 8.0}
KEEP_SECONDS = 8 * 86400


def cursor_key(provider: str, chain: str, address: str) -> str:
    return f"cur:{provider}:{chain}:{address}"


class WalletMonitor:
    def __init__(self, icfg: Dict[str, Any], labels: LabelRegistry, providers: Any,
                 price_fn: Callable[[str], Optional[float]], store: Optional[Any] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None, clock: Callable[[], float] = time.time,
                 on_verify: Optional[Callable[[str, bool, str], None]] = None):
        self.cfg = icfg
        self.labels = labels
        self.providers = providers
        self.price = price_fn
        self.store = store
        self.on_event = on_event
        self.on_verify = on_verify
        self.clock = clock
        self.assets: Dict[str, TrackedAsset] = {}
        self.cursors: Dict[str, Any] = dict(getattr(store, "cursors", {}) or {})
        self.addr_state: Dict[Tuple, Dict[str, Any]] = {}
        self.transfers: List[Dict[str, Any]] = []
        self.balances: Dict[Tuple[str, str, str], List[Tuple[float, float]]] = {}
        self.seen: Set[Tuple[str, str, int]] = set()
        self.priority: Dict[str, int] = {}
        self.priority_assets: Set[str] = set()          # v0.7 name: assets with a current anomaly
        self.status = "idle"
        self.last_error = ""
        self.first_ok_ts: Dict[str, float] = {}
        self.notes: Dict[str, str] = {}                  # chain -> provider coverage note
        for asset, spec in (icfg.get("tokens") or {}).items():
            ta = config_token(asset, spec)
            if ta is not None:
                self.track(ta)

    # ---------------------------------------------------------------- tracked assets
    def track(self, ta: TrackedAsset) -> bool:
        """Track an asset. Configured / overridden assets win over discovered ones."""
        sym = ta.asset.upper()
        ta.asset = sym
        old = self.assets.get(sym)
        if old is not None:
            if old.source in ("config", "override") and ta.source == "discovered":
                return False
            if (old.chain, old.native, old.token) == (ta.chain, ta.native, ta.token) and old.source == ta.source:
                return False
        if not ta.native and ta.token and self.asset_for(ta.chain, ta.token) not in (None, sym):
            return False                                  # another asset already owns this contract
        if old is not None and (old.chain, old.token) == (ta.chain, ta.token):
            ta.status = old.status if old.status != "pending" else ta.status
            ta.verified_symbol = ta.verified_symbol or old.verified_symbol
            ta.decimals = ta.decimals if ta.decimals is not None else old.decimals
        self.assets[sym] = ta
        return True

    def untrack(self, asset: str) -> None:
        self.assets.pop(asset.upper(), None)

    def sync(self, tracked: List[TrackedAsset], keep: Optional[Set[str]] = None) -> None:
        """Apply the registry's tracked set for the current universe (config tokens always stay)."""
        want = {t.asset.upper() for t in tracked}
        for t in tracked:
            self.track(t)
        for sym in list(self.assets):
            ta = self.assets[sym]
            if ta.source == "config":
                continue
            if sym not in want or (keep is not None and sym not in keep):
                del self.assets[sym]

    def add_token(self, asset: str, chain: str, contract: str, decimals: Optional[int] = None,
                  source: str = "discovered", token_wide: str = "off") -> bool:
        """v0.7 call: track an EVM token contract."""
        return self.track(TrackedAsset(asset=asset.upper(), chain=chain.lower(), native=False,
                                       token=str(contract).lower(), decimals=decimals, source=source,
                                       token_wide=token_wide))

    @property
    def tokens(self) -> Dict[str, Dict[str, Any]]:
        """v0.7-shaped view (asset -> dict with chain / contract / status / source)."""
        return {a: t.to_dict() for a, t in self.assets.items()}

    def asset_for(self, chain: str, token: str) -> Optional[str]:
        key = str(token or "").lower()
        for a, t in self.assets.items():
            if t.chain == chain and (t.key.lower() == key):
                return a
        return None

    def supports(self, chain: str) -> bool:
        return bool(self.providers is not None and self.providers.supports(chain))

    def chains(self) -> List[str]:
        return sorted({t.chain for t in self.assets.values() if self.supports(t.chain)})

    # ---------------------------------------------------------------- coverage
    def coverage(self, asset: str) -> Dict[str, Any]:
        t = self.assets.get(asset.upper())
        if not t:
            return {"covered": False, "reason": "asset not tracked by wallet intelligence"}
        if not self.supports(t.chain):
            return {"covered": False, "reason": f"chain {t.chain} not supported", "unsupported": True}
        if t.status.startswith("invalid"):
            return {"covered": False, "reason": t.status}
        addrs = self.labels.monitored(t.chain)
        polled = [a for a in addrs if self.addr_state.get((t.chain, a.address), {}).get("last_ok")]
        lag = [a.address for a in addrs if self.addr_state.get((t.chain, a.address), {}).get("lagging")]
        if not addrs and t.token_wide == "off":
            return {"covered": False, "reason": "no reliable labelled addresses"}
        if not polled and t.token_wide_status != "active":
            return {"covered": False, "reason": "no successful polls yet"}
        p = self.providers.for_chain(t.chain)
        return {"covered": True, "addresses": len(addrs), "polled": len(polled), "lagging": lag,
                "token_wide": t.token_wide_status or "off", "provider": getattr(p, "name", None),
                "note": self.notes.get(t.chain, "")}

    # ---------------------------------------------------------------- ingest
    def _verify(self, t: TrackedAsset, raw: RawTransfer) -> bool:
        if raw.decimals is not None and not t.native:
            t.decimals = raw.decimals
        if t.status.startswith("invalid"):
            return False
        if t.status == "pending" and raw.symbol:
            if raw.symbol.upper() != t.asset:
                t.status = f"invalid: contract symbol is {raw.symbol.upper()}, expected {t.asset}"
                if self.on_verify:
                    self.on_verify(t.asset, False, t.status)
                return False
            t.status, t.verified_symbol = "ok", raw.symbol.upper()
            if self.on_verify and not t.native:
                self.on_verify(t.asset, True, f"on-chain token symbol {t.verified_symbol} verified")
        return True

    def event_for(self, raw: RawTransfer, source: str = "address") -> Dict[str, Any]:
        fl, tl = self.labels.lookup(raw.chain, raw.from_address), self.labels.lookup(raw.chain, raw.to_address)
        c = classify(fl, tl, raw.from_address, raw.to_address)
        px = self.price(raw.asset)
        why = c.explanation + (f" · {raw.note}" if raw.note else "")
        ev = WalletEvent(asset=raw.asset, chain=raw.chain, timestamp=raw.ts, tx_hash=raw.tx_hash, event_type=c.event_type,
                         amount_native=raw.amount, amount_usd=(raw.amount * px) if px else None,
                         from_address=raw.from_address, to_address=raw.to_address,
                         from_entity=fl.entity if fl else None, to_entity=tl.entity if tl else None,
                         entity_type=c.entity_type, attribution_confidence=c.attribution,
                         classification_confidence=c.confidence, index=raw.index, block=raw.block, token=raw.token,
                         from_type=fl.entity_type if (fl and fl.reliable) else None,
                         to_type=tl.entity_type if (tl and tl.reliable) else None,
                         direction=c.direction, explanation=why, provider=raw.provider, source=source)
        return ev.row()

    def ingest(self, raws: List[RawTransfer], source: str = "address") -> List[Dict[str, Any]]:
        out = []
        for raw in raws:
            t = self.assets.get(raw.asset.upper())
            if t is None or not self._verify(t, raw):
                continue
            key = (raw.chain, str(raw.tx_hash), int(raw.index))
            if key in self.seen:
                continue
            self.seen.add(key)
            row = self.event_for(raw, source)
            out.append(row)
            self._update_running_balance(row)
            usd = row["usd_value"]
            if self.on_event and usd and usd >= float(self.cfg.get("min_event_usd", 250000)):
                self.on_event({"ts": row["ts"], "asset": row["asset"], "category": "WALLET",
                               "event_type": "wallet_" + row["event_type"].lower(), "severity": 1, "level": usd,
                               "message": f"{row['event_type']}: {row['amount']:,.0f} {row['asset']} (${usd:,.0f}) — "
                                          f"{row['explanation']}",
                               "evidence": {"tx": row["tx_hash"], "chain": row["chain"], "from": row["from_addr"],
                                            "to": row["to_addr"], "confidence": row["class_confidence"],
                                            "attribution": row["attribution_confidence"], "provider": row["provider"]}})
        self.transfers.extend(out)
        horizon = self.clock() - KEEP_SECONDS
        self.transfers = [t for t in self.transfers if t["ts"] >= horizon]
        if self.store is not None and out:
            self.store.save_transfers(out)
        return out

    def load_history(self, rows: List[Dict[str, Any]]) -> None:
        """Rows persisted by an earlier run (v0.7 or v0.8), re-attributed with the current labels."""
        for r in rows:
            chain = r.get("chain") or "ethereum"
            raw = RawTransfer(chain=chain, asset=str(r.get("asset") or ""), token=str(r.get("token") or NATIVE),
                              tx_hash=str(r.get("tx_hash")), index=int(r.get("log_index") or 0), ts=float(r.get("ts") or 0),
                              from_address=str(r.get("from_addr") or ""), to_address=str(r.get("to_addr") or ""),
                              amount=float(r.get("amount") or 0), block=r.get("block"), provider=str(r.get("provider") or ""))
            row = self.event_for(raw, str(r.get("source") or "address"))
            if r.get("usd_value") is not None:
                row["usd_value"] = r["usd_value"]
            self.seen.add((chain, raw.tx_hash, raw.index))
            self.transfers.append(row)

    def _update_running_balance(self, row: Dict[str, Any]) -> None:
        for addr, sign in ((row["from_addr"], -1.0), (row["to_addr"], 1.0)):
            lab = self.labels.lookup(row["chain"], addr)
            if lab is None or not lab.reliable:
                continue
            k = (row["chain"], row["token"], lab.address)
            hist = self.balances.get(k)
            if hist:  # only extend series anchored by a snapshot
                hist.append((row["ts"], hist[-1][1] + sign * row["amount"]))

    # ---------------------------------------------------------------- scheduling
    def tier(self, assets: List[str]) -> int:
        return min((int(self.priority.get(a, 5)) for a in assets), default=5)

    def schedule(self) -> List[Tuple[str, Any]]:
        now = self.clock()
        jobs: List[Tuple[str, Any]] = []
        min_iv = float(self.cfg.get("address_poll_min_seconds", 60))
        secs_left = max(3600.0, 86400.0 - (now % 86400.0))
        by_provider: Dict[str, List[Tuple[str, Any, int, int]]] = {}
        for chain in self.chains():
            p = self.providers.for_chain(chain)
            on_chain = [a for a, t in self.assets.items() if t.chain == chain and not t.status.startswith("invalid")]
            if p is None or not on_chain:
                continue
            tier = self.tier(on_chain)
            calls = max(1, len({t.native for a, t in self.assets.items() if a in on_chain})) \
                if p.family == "evm" else len(on_chain)
            for lab in self.labels.monitored(chain):
                by_provider.setdefault(p.name, []).append((chain, lab, tier, calls))
        for pname, items in by_provider.items():
            p = self.providers.by_name(pname) if hasattr(self.providers, "by_name") else self.providers.for_chain(items[0][0])
            remaining = max(1, p.remaining_today())
            demand = sum(c / TIER_MULT[t] for _, _, t, c in items)
            base_iv = max(min_iv, secs_left * demand / (0.7 * remaining))
            for chain, lab, tier, _ in items:
                # the tier multiplies the budget-derived base (never below the minimum interval), so quiet
                # assets are polled up to 8x less often even when the budget is ample
                iv = max(min_iv, base_iv) * TIER_MULT[tier]
                if tier == 1 and p.remaining_today() > 0.5 * p.daily:
                    iv = max(min_iv, iv / 2)         # anomaly and a healthy budget: poll twice as often
                st = self.addr_state.setdefault((chain, lab.address), {"next": 0.0})
                st["interval"] = iv
                st["tier"] = tier
                if now >= st["next"]:
                    jobs.append(("address", (chain, lab)))
                    st["next"] = now + iv
        bal_iv = float(self.cfg.get("balance_poll_minutes", 60)) * 60.0
        for asset, t in self.assets.items():
            p = self.providers.for_chain(t.chain) if self.providers is not None else None
            if p is None or t.status.startswith("invalid"):
                continue
            labs = self.labels.monitored(t.chain)
            for lab in labs:
                k = ("bal", t.chain, t.key, lab.address)
                st = self.addr_state.setdefault(k, {"next": 0.0})
                if now >= st["next"] and p.remaining_today() > len(labs) * 2:
                    jobs.append(("balance", (asset, lab)))
                    st["next"] = now + bal_iv * TIER_MULT[int(self.priority.get(asset, 5))] / 2
            if t.token_wide in ("on", "auto") and t.token_wide_status != "disabled" and p.supports_token_wide():
                st = self.addr_state.setdefault(("tw", asset), {"next": 0.0})
                if now >= st["next"]:
                    jobs.append(("token", asset))
                    st["next"] = now + max(60.0, min_iv * 2)
        return jobs

    # ---------------------------------------------------------------- polling
    async def poll_address(self, chain: str, lab) -> int:
        p = self.providers.for_chain(chain)
        st = self.addr_state.setdefault((chain, lab.address), {"next": 0.0})
        assets = [t for t in self.assets.values() if t.chain == chain and not t.status.startswith("invalid")]
        if p is None or not assets:
            return 0
        ck = cursor_key(p.name, chain, lab.address)
        cur = self.cursors.get(ck)
        if cur is None and p.family == "evm":              # v0.7 cursor: last token block
            legacy = self.cursors.get(f"addr:{chain}:{lab.address}")
            cur = {"tok": int(legacy)} if legacy is not None else {}
        try:
            res = await p.fetch_address(chain, lab.address, assets, dict(cur or {}))
        except Exception as exc:
            st["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            raise
        n = len(self.ingest(res.transfers, "address"))
        self.cursors[ck] = res.cursor
        st["lagging"] = not res.complete
        st["last_ok"] = self.clock()
        st["last_error"] = ""
        st["last_new"] = n
        if res.note:
            self.notes[chain] = res.note
        self.first_ok_ts.setdefault(chain, st["last_ok"])
        return n

    async def poll_balance(self, asset: str, lab) -> Optional[float]:
        t = self.assets.get(asset)
        p = self.providers.for_chain(t.chain) if t else None
        if t is None or p is None:
            return None
        bal = await p.fetch_balance(t.chain, lab.address, t)
        if bal is None:
            return None
        k = (t.chain, t.key, lab.address)
        self.balances.setdefault(k, []).append((self.clock(), bal))
        self.balances[k] = self.balances[k][-2000:]
        if self.store is not None:
            self.store.save_balances([{"chain": t.chain, "token": t.key, "address": lab.address, "ts": self.clock(),
                                       "asset": asset, "balance": bal, "source": p.name}])
        return bal

    async def poll_token(self, asset: str) -> int:
        t = self.assets[asset]
        p = self.providers.for_chain(t.chain)
        ck = f"cur:tw:{t.chain}:{t.token}"
        cur = self.cursors.get(ck)
        if cur is None and self.cursors.get(f"token:{t.chain}:{t.token}") is not None:
            cur = {"block": int(self.cursors[f"token:{t.chain}:{t.token}"])}
        res = await p.fetch_token(t.chain, t, dict(cur or {}))
        rows = res.transfers
        if rows and t.token_wide == "auto":
            ts = [r.ts for r in rows]
            span_h = max((max(ts) - min(ts)) / 3600.0, 1e-3)
            rate = len(rows) / span_h
            if len(rows) >= 20 and rate > float(self.cfg.get("token_wide_max_transfers_per_hour", 400)):
                t.token_wide_status = "disabled"
                t.token_wide_reason = f"too active for token-wide polling ({rate:,.0f} transfers/h); address-centric only"
                return 0
        t.token_wide_status = "active"
        self.first_ok_ts.setdefault(t.chain, self.clock())
        self.cursors[ck] = res.cursor
        return len(self.ingest(rows, "token"))

    async def run_once(self) -> Dict[str, int]:
        done = {"address": 0, "balance": 0, "token": 0, "errors": 0}
        blocked: Set[str] = set()                        # providers skipped for the rest of this cycle
        for kind, arg in self.schedule():
            chain = arg[0] if kind == "address" else self.assets[arg[0] if kind == "balance" else arg].chain
            p = self.providers.for_chain(chain)
            if p is None or p.name in blocked or chain in blocked:
                continue
            try:
                if kind == "address":
                    await self.poll_address(*arg)
                elif kind == "balance":
                    await self.poll_balance(*arg)
                else:
                    await self.poll_token(arg)
                done[kind] += 1
            except ProviderAuthError as exc:
                self.last_error = str(exc)
                blocked.add(p.name)
                done["errors"] += 1
            except (ProviderBudgetExceeded, ProviderRateLimited) as exc:
                self.last_error = str(exc)
                blocked.add(p.name)
                done["errors"] += 1
            except ProviderChainUnavailable as exc:
                self.last_error = str(exc)
                blocked.add(chain)
                done["errors"] += 1
            except (ProviderError, LookupError) as exc:
                done["errors"] += 1
                self.last_error = str(exc)
        if self.store is not None:
            self.store.save_cursors(self.cursors)
        self.status = "ok" if not done["errors"] else "degraded"
        return done

    async def run(self, stop: asyncio.Event, interval: float = 15.0) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # never kill the scanner because of wallet intelligence
                self.status, self.last_error = "error", repr(exc)[:300]
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- views
    def provider_view(self) -> Dict[str, Dict[str, Any]]:
        """Per provider: assets covered, monitored / lagging addresses (for Health)."""
        out: Dict[str, Dict[str, Any]] = {}
        for a, t in self.assets.items():
            p = self.providers.assigned(t.chain) if self.providers is not None else None
            if p is None:
                continue
            v = out.setdefault(p.name, {"assets": [], "chains": set(), "addresses": 0, "lagging": 0, "polled": 0})
            v["assets"].append(a)
            v["chains"].add(t.chain)
        for name, v in out.items():
            for c in v["chains"]:
                for lab in self.labels.monitored(c):
                    st = self.addr_state.get((c, lab.address), {})
                    v["addresses"] += 1
                    v["lagging"] += 1 if st.get("lagging") else 0
                    v["polled"] += 1 if st.get("last_ok") else 0
            v["assets"] = sorted(v["assets"])
            v["chains"] = sorted(v["chains"])
        return out


def config_token(asset: str, spec: Dict[str, Any]) -> Optional[TrackedAsset]:
    """A configured asset (`intel.tokens`): {chain, contract | native, decimals, token_wide}."""
    if not isinstance(spec, dict):
        return None
    chain = str(spec.get("chain", "ethereum")).lower()
    native = bool(spec.get("native"))
    if not native and not spec.get("contract"):
        return None
    c = CHAINS.get(chain)
    token = None
    if not native:
        token = normalize_token(c.family, str(spec["contract"])) if c else str(spec["contract"]).lower()
        if token is None:
            token = str(spec["contract"]).lower()
    return TrackedAsset(asset=asset.upper(), chain=chain, native=native, token=token, decimals=spec.get("decimals"),
                        source="config", status="pending" if not native else "ok",
                        token_wide=str(spec.get("token_wide", "auto")).lower())


# v0.7 name
IntelMonitor = WalletMonitor
