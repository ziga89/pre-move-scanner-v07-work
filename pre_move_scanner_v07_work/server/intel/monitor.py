"""Address-centric wallet monitor (v0.7 amendment 2).

Primary mode: poll each *labelled, reliable* MM / CEX / custody / whale
address — one `tokentx` call per address covers every tracked token — with a
budget-aware schedule (addresses of top-ranked anomalies are polled more
often). Token-wide polling (`tokentx` by contract) is used only where it is
manageable: in `auto` mode it is switched off for a token as soon as its
observed transfer rate exceeds `token_wide_max_transfers_per_hour`.
Balances are snapshotted with `tokenbalance` on a slower cadence.

Coverage is tracked honestly: an address whose new transfers exceed what one
cycle can page through is marked `lagging`; scores computed from incomplete
coverage are reduced or reported as N/A.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .classify import classify_transfer
from .etherscan import CHAIN_IDS, EtherscanAuthError, EtherscanBudgetExceeded, EtherscanClient, EtherscanError
from .labels import LabelRegistry


def log_index_of(x: Dict[str, Any]) -> int:
    li = x.get("logIndex")
    try:
        return int(li)
    except (TypeError, ValueError):
        h = hashlib.sha1(f"{x.get('from')}|{x.get('to')}|{x.get('value')}|{x.get('contractAddress')}".encode()).hexdigest()
        return int(h[:8], 16)


class IntelMonitor:
    def __init__(self, icfg: Dict[str, Any], labels: LabelRegistry, client: EtherscanClient,
                 price_fn: Callable[[str], Optional[float]], store: Optional[Any] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None, clock=time.time):
        self.cfg = icfg
        self.labels = labels
        self.client = client
        self.price = price_fn
        self.store = store          # object with save_transfers(rows), save_balances(rows), cursors dict
        self.on_event = on_event
        self.clock = clock
        self.tokens: Dict[str, Dict[str, Any]] = {}          # asset -> {chain, contract(lower), token_wide, ...}
        for asset, spec in (icfg.get("tokens") or {}).items():
            if spec.get("contract"):
                self.tokens[asset.upper()] = {"chain": str(spec.get("chain", "ethereum")).lower(),
                                              "contract": str(spec["contract"]).lower(),
                                              "token_wide": str(spec.get("token_wide", "auto")).lower(),
                                              "status": "pending", "verified_symbol": None,
                                              "decimals": spec.get("decimals")}
        self.by_contract = {(t["chain"], t["contract"]): a for a, t in self.tokens.items()}
        self.cursors: Dict[str, int] = dict(getattr(store, "cursors", {}) or {})
        self.addr_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.transfers: List[Dict[str, Any]] = []            # recent classified transfers (memory window)
        self.balances: Dict[Tuple[str, str, str], List[Tuple[float, float]]] = {}  # (chain, token, addr) -> [(ts, bal)]
        self.seen: Set[Tuple[str, str, int]] = set()
        self.priority_assets: Set[str] = set()
        self.status = "idle"
        self.last_error = ""
        self.first_ok_ts: Dict[str, float] = {}               # chain -> first successful address poll
        for t in self.tokens.values():
            t["source"] = "config"

    def supports(self, chain: str) -> bool:
        fn = getattr(self.client, "supports", None)
        return bool(fn(chain)) if callable(fn) else chain in CHAIN_IDS

    def add_token(self, asset: str, chain: str, contract: str, decimals: Optional[int] = None,
                  source: str = "discovered", token_wide: str = "off") -> bool:
        """Track another token (e.g. a contract discovered from the universe). Configured tokens win."""
        asset = asset.upper()
        if asset in self.tokens:
            return False
        key = (chain.lower(), contract.lower())
        if key in self.by_contract:
            return False
        self.tokens[asset] = {"chain": chain.lower(), "contract": contract.lower(), "token_wide": token_wide,
                              "status": "pending", "verified_symbol": None, "decimals": decimals, "source": source}
        self.by_contract[key] = asset
        return True

    # ------------------------------------------------------------ helpers
    def asset_for(self, chain: str, contract: str) -> Optional[str]:
        return self.by_contract.get((chain, (contract or "").lower()))

    def coverage(self, asset: str) -> Dict[str, Any]:
        t = self.tokens.get(asset.upper())
        if not t:
            return {"covered": False, "reason": "token not configured for wallet intelligence"}
        if not self.supports(t["chain"]):
            return {"covered": False, "reason": f"chain {t['chain']} not supported", "unsupported": True}
        if t["status"].startswith("invalid"):
            return {"covered": False, "reason": t["status"]}
        addrs = self.labels.monitored(t["chain"])
        polled = [a for a in addrs if self.addr_state.get((t["chain"], a.address), {}).get("last_ok")]
        lag = [a.address for a in addrs if self.addr_state.get((t["chain"], a.address), {}).get("lagging")]
        if not addrs and t["token_wide"] == "off":
            return {"covered": False, "reason": "no reliable labelled addresses"}
        if not polled and t.get("token_wide_status") != "active":
            return {"covered": False, "reason": "no successful polls yet"}
        return {"covered": True, "addresses": len(addrs), "polled": len(polled), "lagging": lag,
                "token_wide": t.get("token_wide_status", "off")}

    def _classify_rows(self, rows: List[Dict[str, Any]], chain: str, source: str) -> List[Dict[str, Any]]:
        out = []
        for x in rows:
            asset = self.asset_for(chain, x.get("contractAddress"))
            if not asset:
                continue
            tok = self.tokens[asset]
            sym = str(x.get("tokenSymbol") or "").upper()
            if x.get("tokenDecimal") not in (None, ""):
                tok["decimals"] = int(x["tokenDecimal"])
            if tok["verified_symbol"] is None and sym:
                if sym != asset:
                    tok["status"] = f"invalid: contract symbol is {sym}, expected {asset}"
                    continue
                tok["verified_symbol"] = sym
                tok["status"] = "ok"
            key = (chain, str(x.get("hash")), log_index_of(x))
            if key in self.seen:
                continue
            self.seen.add(key)
            dec = int(x.get("tokenDecimal") or 18)
            amt = int(x.get("value") or 0) / (10 ** dec)
            frm, to = str(x.get("from", "")).lower(), str(x.get("to", "")).lower()
            fl, tl = self.labels.lookup(chain, frm), self.labels.lookup(chain, to)
            cls, conf, why = classify_transfer(fl, tl, frm, to)
            px = self.price(asset)
            row = {"chain": chain, "tx_hash": key[1], "log_index": key[2], "ts": float(x.get("timeStamp") or 0),
                   "block": int(x.get("blockNumber") or 0), "token": tok["contract"], "asset": asset,
                   "from_addr": frm, "to_addr": to, "amount": amt, "usd_value": (amt * px) if px else None,
                   "from_entity": fl.entity if fl else None, "from_type": fl.entity_type if fl else None,
                   "to_entity": tl.entity if tl else None, "to_type": tl.entity_type if tl else None,
                   "classification": cls, "class_confidence": conf, "explanation": why, "source": source}
            out.append(row)
            self._update_running_balance(row, fl, tl)
            if self.on_event and row["usd_value"] and row["usd_value"] >= float(self.cfg.get("min_event_usd", 250000)):
                self.on_event({"ts": row["ts"], "asset": asset, "category": "WALLET",
                               "event_type": "wallet_" + cls.lower().replace("-", "_"), "severity": 1,
                               "level": row["usd_value"],
                               "message": f"{cls}: {amt:,.0f} {asset} (${row['usd_value']:,.0f}) — {why}",
                               "evidence": {"tx": row["tx_hash"], "chain": chain, "from": frm, "to": to,
                                            "confidence": conf}})
        self.transfers.extend(out)
        horizon = self.clock() - 8 * 86400
        self.transfers = [t for t in self.transfers if t["ts"] >= horizon]
        if self.store is not None and out:
            self.store.save_transfers(out)
        return out

    def _update_running_balance(self, row, fl, tl) -> None:
        for lab, sign in ((fl, -1.0), (tl, 1.0)):
            if lab is None or not lab.reliable:
                continue
            k = (row["chain"], row["token"], lab.address)
            hist = self.balances.get(k)
            if hist:  # only extend series anchored by a snapshot
                hist.append((row["ts"], hist[-1][1] + sign * row["amount"]))

    # ------------------------------------------------------------ polling
    def schedule(self) -> List[Tuple[str, Any]]:
        """Pick the next poll jobs given the remaining daily budget."""
        now = self.clock()
        jobs: List[Tuple[str, Any]] = []
        chains = sorted({t["chain"] for t in self.tokens.values() if self.supports(t["chain"])})
        addrs = [(c, a) for c in chains for a in self.labels.monitored(c)]
        remaining = self.client.remaining_today()
        secs_left = max(3600.0, 86400.0 - (now % 86400.0))
        # 70 % of the budget for address polling, 20 % balances, 10 % token-wide.
        per_addr_calls = max(1.0, 0.7 * remaining / max(1, len(addrs)))
        base_iv = max(float(self.cfg.get("address_poll_min_seconds", 60)), secs_left / per_addr_calls)
        for c, lab in addrs:
            st = self.addr_state.setdefault((c, lab.address), {"next": 0.0})
            iv = base_iv
            if self.priority_assets and remaining > 0.5 * self.client.daily:
                # top-ranked anomalies present and budget healthy: poll twice as often
                iv = max(float(self.cfg.get("address_poll_min_seconds", 60)), base_iv / 2)
            if now >= st["next"]:
                jobs.append(("address", (c, lab)))
                st["next"] = now + iv
        bal_iv = float(self.cfg.get("balance_poll_minutes", 60)) * 60.0
        for asset, t in self.tokens.items():
            if t["status"].startswith("invalid"):
                continue
            for lab in self.labels.monitored(t["chain"]):
                k = (t["chain"], t["contract"], lab.address)
                st = self.addr_state.setdefault(("bal",) + k, {"next": 0.0})
                if now >= st["next"] and remaining > len(addrs) * 2:
                    jobs.append(("balance", (asset, t, lab)))
                    st["next"] = now + bal_iv
            if t["token_wide"] in ("on", "auto") and t.get("token_wide_status") != "disabled":
                st = self.addr_state.setdefault(("tw", asset), {"next": 0.0})
                if now >= st["next"]:
                    jobs.append(("token", (asset, t)))
                    st["next"] = now + max(60.0, base_iv)
        return jobs

    async def poll_address(self, chain: str, lab) -> int:
        st = self.addr_state.setdefault((chain, lab.address), {"next": 0.0})
        ck = f"addr:{chain}:{lab.address}"
        start = int(self.cursors.get(ck, 0))
        size = int(self.cfg.get("page_size", 1000))
        n = 0
        for _ in range(int(self.cfg.get("max_pages_per_poll", 3))):
            rows = await self.client.tokentx(chain, address=lab.address, startblock=start, offset=size)
            n += len(self._classify_rows(rows, chain, "address"))
            if rows:
                start = max(int(r.get("blockNumber") or 0) for r in rows)
            if len(rows) < size:
                st["lagging"] = False
                break
        else:
            st["lagging"] = True  # more pages pending: coverage incomplete this cycle
        self.cursors[ck] = start
        st["last_ok"] = self.clock()
        self.first_ok_ts.setdefault(chain, st["last_ok"])
        return n

    async def poll_balance(self, asset: str, t: Dict[str, Any], lab) -> Optional[float]:
        dec = t.get("decimals")
        if dec is None:
            return None  # decimals learned from the first tokentx row; never guess
        raw = await self.client.tokenbalance(t["chain"], t["contract"], lab.address)
        if raw is None:
            return None
        bal = raw / (10 ** int(dec))
        k = (t["chain"], t["contract"], lab.address)
        self.balances.setdefault(k, []).append((self.clock(), bal))
        self.balances[k] = self.balances[k][-2000:]
        if self.store is not None:
            self.store.save_balances([{"chain": t["chain"], "token": t["contract"], "address": lab.address,
                                       "ts": self.clock(), "asset": asset, "balance": bal, "source": "tokenbalance"}])
        return bal

    async def poll_token(self, asset: str, t: Dict[str, Any]) -> int:
        ck = f"token:{t['chain']}:{t['contract']}"
        start = int(self.cursors.get(ck, 0))
        size = int(self.cfg.get("page_size", 1000))
        rows = await self.client.tokentx(t["chain"], contract=t["contract"], startblock=start, offset=size)
        if rows and t["token_wide"] == "auto":
            ts = [float(r.get("timeStamp") or 0) for r in rows]
            span_h = max((max(ts) - min(ts)) / 3600.0, 1e-3)
            rate = len(rows) / span_h
            if len(rows) >= 20 and rate > float(self.cfg.get("token_wide_max_transfers_per_hour", 400)):
                t["token_wide_status"] = "disabled"
                t["token_wide_reason"] = f"too active for token-wide polling ({rate:,.0f} transfers/h); address-centric only"
                return 0
        t["token_wide_status"] = "active"
        self.first_ok_ts.setdefault(t["chain"], self.clock())
        n = len(self._classify_rows(rows, t["chain"], "token"))
        if rows:
            self.cursors[ck] = max(int(r.get("blockNumber") or 0) for r in rows)
        return n

    async def run_once(self) -> Dict[str, int]:
        done = {"address": 0, "balance": 0, "token": 0, "errors": 0}
        for kind, arg in self.schedule():
            try:
                if kind == "address":
                    await self.poll_address(*arg)
                elif kind == "balance":
                    await self.poll_balance(*arg)
                else:
                    await self.poll_token(*arg)
                done[kind] += 1
            except EtherscanAuthError as exc:
                self.status, self.last_error = "auth error", str(exc)
                raise
            except EtherscanBudgetExceeded as exc:
                self.status, self.last_error = "daily budget exhausted", str(exc)
                break
            except EtherscanError as exc:
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
            except EtherscanAuthError:
                await asyncio.sleep(600)
            except Exception as exc:  # never kill the scanner because of intel
                self.status, self.last_error = "error", repr(exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
