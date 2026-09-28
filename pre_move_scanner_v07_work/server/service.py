"""ScannerService — orchestrates universe, discovery, feeds, engine, scoring,
events, wallet intelligence and storage. Framework-agnostic: the FastAPI app
and the stdlib dev server both call the same payload builders.

Loops (all asyncio, none blocking):
  tick        1 Hz  market features → asset scoring → events → persistence → broadcast
  universe    hourly (CoinGecko) with cached fallback
  discovery   every 30–60 min (exchange catalogs → per-coin venue selection)
  maintenance retention pruning, outcome filling, feed-health snapshots
  intel       wallet monitor (only when enabled and keyed)
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .config import resolve_path
from .engine.asset_state import PRE_MOVE_STATUSES, STATUS_ORDER, AssetState
from .engine.alerts import HighConvictionAlerts
from .engine.events import EventDetector
from .engine.host import LocalEngineHost, MarketSpec, ProcessEngineHost
from .engine.leadlag import price_lead_lag
from .feeds.registry import build_adapters
from .intel.discovery import ContractDiscovery
from .intel.etherscan import EtherscanClient
from .intel.labels import LabelRegistry
from .intel.monitor import IntelMonitor
from .intel.providers import ProviderRegistry
from .intel.scores import compute_scores, entity_balances
from .intel.status import asset_status, coverage_summary, gate_scores
from .intel.store import DbIntelStore
from .storage.db import Database, prune
from .storage.history import (alert_events_query, alert_row, alerts_query, close_open_alerts, load_token_contracts,
                              token_contract_row, asset_history, asset_row_1m, asset_row_5s, event_row, events_query, fill_outcomes,
                              load_asset_closes, load_market_minutes, market_row_10s, market_row_1m, outcome_summary,
                              venue_history)
from .storage.schema import (ALERT_COLS, ASSET_1M_COLS, ASSET_5S_COLS, EVENT_COLS, MARKET_10S_COLS, MARKET_1M_COLS,
                             TOKEN_CONTRACT_COLS)
from .universe.catalog import Catalog
from .universe.coingecko import CoinGeckoClient
from .universe.fx import FxService
from .universe.http import HttpClient
from .universe.universe import UniverseManager
from .universe.venues import AssetInfo, VenueSelector, crosscheck_unsupported
from .util import rnd

LATE_GROUP = ("MOVE IN PROGRESS", "LATE")
QUIET_GROUP = ("NO DATA", "STALE", "WARMING")


def _wallet_cells(ws: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact per-row wallet state for the table: explicit state per score, never a bare N/A."""
    if not ws:
        return {"state": "WARMING", "label": "WARMING", "reason": "not evaluated yet", "scores": {}}
    return {"state": ws.get("state"), "label": ws.get("label"), "reason": ws.get("reason"),
            "scores": {k: {"state": v.get("state"), "label": v.get("label"), "reason": v.get("reason")}
                       for k, v in (ws.get("scores") or {}).items()}}


def _json_safe(o: Any) -> Any:
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


class ScannerService:
    def __init__(self, cfg: Dict[str, Any], clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self.sim = cfg.get("mode") == "sim" or cfg["feeds"].get("backend") == "sim"
        if self.sim:
            cfg["universe"] = dict(cfg["universe"], pinned_assets=[])  # real tickers do not exist in SIM
        self.db = Database(resolve_path(cfg, cfg["storage"]["path"]), cfg["storage"])
        self.fx = FxService()
        self.adapters, self.sim_driver = build_adapters(cfg, clock=clock)
        self.selector = VenueSelector(cfg["discovery"])
        self.universe = UniverseManager(cfg["universe"])
        self.events = EventDetector(cfg["events"])
        self.alerts = HighConvictionAlerts(cfg.get("alerts", {}))
        self.http = HttpClient()
        self.cg = CoinGeckoClient(self.http, cfg["universe"])
        self.catalogs: Dict[str, Catalog] = {}
        self.assets: Dict[str, AssetState] = {}
        self.info: Dict[str, AssetInfo] = {}
        self.selections: Dict[str, Any] = {}
        self.results: Dict[str, Dict[str, Any]] = {}
        self.market_created: Dict[tuple, float] = {}
        self.unsupported_top: Dict[str, List[Dict[str, Any]]] = {}
        self.category_ids: Dict[str, set] = {}
        self.category_ts = 0.0
        self.universe_result: Dict[str, Any] = {}
        self.status: Dict[str, Any] = {"started": clock(), "universe": "pending", "discovery": "pending",
                                       "last_tick_ms": None, "avg_tick_ms": None, "ticks": 0,
                                       "warnings": list(cfg.get("_meta", {}).get("warnings", []))}
        self._reselect = set()
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._last_persist_a = 0.0
        self._last_persist_m = 0.0
        self._intel_cache: Dict[str, tuple] = {}
        self.listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self.wallet_status: Dict[str, Dict[str, Any]] = {}
        self.wallet_summary: Dict[str, Any] = {}
        self._wallet_summary_ts = 0.0
        self.radar: Dict[str, Any] = self.alerts.radar(clock())

        workers = int(cfg["feeds"].get("workers", 1))
        if workers > 1:
            self.host = ProcessEngineHost(cfg, workers, list(self.adapters), db_path=str(self.db.path))
        else:
            self.host = LocalEngineHost(cfg, self.adapters, self.fx.rate, rehydrate=self._rehydrate_market, clock=clock)

        # wallet intelligence (optional)
        self.labels = LabelRegistry()
        self.labels.load_csv(resolve_path(cfg, cfg["intel"].get("labels_file", "labels/wallet_labels.csv")))
        self.labels.load_dicts(cfg["intel"].get("legacy_labels", []))
        self.intel: Optional[IntelMonitor] = None
        self.intel_store: Optional[DbIntelStore] = None
        self.providers: Optional[ProviderRegistry] = None
        self.contract_discovery: Optional[ContractDiscovery] = None
        if cfg["intel"].get("enabled"):
            self.intel_store = DbIntelStore(self.db)
            # one registry of wallet-data providers; Etherscan V2 serves the EVM chains today
            self.providers = ProviderRegistry([EtherscanClient(self.http, cfg["intel"], budget_store=self.intel_store.budget)])
            self.intel = IntelMonitor(cfg["intel"], self.labels, self.providers, self._asset_price, store=self.intel_store,
                                      on_event=self._external_event)
            if cfg["intel"].get("auto_discover_contracts", True) and not self.sim:
                try:
                    cache = self.db.read_sync(load_token_contracts)
                except Exception:
                    cache = {}
                self.contract_discovery = ContractDiscovery(self.cg, cfg["intel"], self.providers.supports,
                                                            cache=cache, clock=clock)
                for r in cache.values():
                    self._track_discovered(r)
            try:
                self.intel.transfers = [t for t in self.intel_store.load_transfers(self.clock() - 8 * 86400)]
                for b in self.intel_store.load_balances(self.clock() - 8 * 86400):
                    self.intel.balances.setdefault((b["chain"], b["token"], b["address"]), []).append((b["ts"], b["balance"]))
            except Exception:
                pass

    # ------------------------------------------------------------------ helpers
    def _asset_price(self, asset: str) -> Optional[float]:
        r = self.results.get(asset)
        if r and r.get("price"):
            return r["price"]
        info = self.info.get(asset)
        return info.price_usd if info else None

    def _rehydrate_market(self, spec: MarketSpec) -> List[Dict[str, Any]]:
        since = self.clock() - float(self.cfg["engine"].get("rehydrate_max_age_hours", 12)) * 3600
        rows = self.db.read_sync(load_market_minutes, spec.asset, spec.exchange, since)
        long_since = self.clock() - float(self.cfg["engine"].get("baseline_long_minutes", 1440)) * 60
        return [r for r in rows if r["ts"] >= long_since]

    def _track_discovered(self, r: Dict[str, Any]) -> None:
        """Start tracking a safely discovered contract (configured tokens always win)."""
        if self.intel is None or r.get("state") != "supported" or not r.get("contract"):
            return
        self.intel.add_token(r["asset"], r["chain"], r["contract"], r.get("decimals"), source="discovered",
                             token_wide=str(self.cfg["intel"].get("discovered_token_wide", "off")))

    def _discovery_candidates(self) -> List[tuple]:
        """(asset, coingecko id) for the universe: pinned first, then current anomalies, then by rank."""
        def prio(a: str):
            info = self.info[a]
            return (0 if info.pinned else 1, -(self.results.get(a, {}).get("premove") or 0.0), info.rank or 9999)
        return [(a, self.info[a].coin_id) for a in sorted(self.info, key=prio) if self.info[a].coin_id]

    async def _contracts_loop(self) -> None:
        per_min = max(0.0, float(self.cfg["intel"].get("discovery_calls_per_minute", 2)))
        while True:
            await asyncio.sleep(60)
            if self.contract_discovery is None or per_min <= 0 or not (self.providers and self.providers.keyed()):
                continue
            try:
                new = await self.contract_discovery.run_once(self._discovery_candidates(),
                                                             configured=list(self.cfg["intel"].get("tokens") or {}),
                                                             max_calls=int(per_min))
                for r in new:
                    self._track_discovered(r)
                if new:
                    self.db.insert("token_contracts", TOKEN_CONTRACT_COLS, [token_contract_row(r) for r in new])
                    self._intel_cache.clear()
            except Exception as exc:
                self.status["contract_discovery_error"] = repr(exc)[:200]

    def _external_event(self, ev: Dict[str, Any]) -> None:
        e = self.events.add_external(ev)
        self.db.insert("events", EVENT_COLS, [event_row(e)], mode="", critical=True)

    def _emit(self, topic: str, payload: Dict[str, Any]) -> None:
        for fn in list(self.listeners):
            try:
                fn(topic, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.sim_driver is not None:
            self._tasks.append(asyncio.ensure_future(self.sim_driver.run()))
            await asyncio.sleep(0.05)
        try:
            n = await asyncio.to_thread(self.db.call, lambda c: close_open_alerts(
                c, self.clock(), "scanner restarted - setup not re-verified after restart"))
            if n:
                self.status["alerts_closed_at_start"] = n
        except Exception as exc:
            self.status["alerts_error"] = repr(exc)[:200]
        await self.host.start()
        await self.refresh_catalogs()
        await self.refresh_universe()
        await self.apply_selection()
        from .engine import tune_gc
        tune_gc()  # long-lived state out of GC scans (avoids tick-time spikes)
        self._tasks += [asyncio.ensure_future(self._tick_loop()),
                        asyncio.ensure_future(self._universe_loop()),
                        asyncio.ensure_future(self._discovery_loop()),
                        asyncio.ensure_future(self._maintenance_loop())]
        if self.intel is not None:
            self._tasks.append(asyncio.ensure_future(self.intel.run(self._stop)))
        if self.contract_discovery is not None:
            self._tasks.append(asyncio.ensure_future(self._contracts_loop()))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.host.stop()
        for a in self.adapters.values():
            try:
                await a.close()
            except Exception:
                pass
        await self.http.close()
        self.db.close()

    # ------------------------------------------------------------------ discovery
    async def refresh_catalogs(self) -> None:
        async def one(ex, ad):
            try:
                return ex, await ad.load_catalog()
            except Exception as exc:
                return ex, Catalog(ex, error=f"{type(exc).__name__}: {exc}"[:300], fetched_ts=self.clock()).index()
        results = await asyncio.gather(*(one(ex, ad) for ex, ad in self.adapters.items()))
        for ex, cat in results:
            if cat.error and ex in self.catalogs and not self.catalogs[ex].error:
                # keep the last good catalog; report the failed refresh
                self.status.setdefault("catalog_errors", {})[ex] = cat.error
                continue
            self.catalogs[ex] = cat
        self.fx.update_from_catalogs(self.catalogs.values())
        ok = sum(1 for c in self.catalogs.values() if not c.error)
        self.status["discovery"] = f"{ok}/{len(self.adapters)} exchange catalogs loaded"
        self.status["catalogs_ts"] = self.clock()

    def _sim_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for i, sym in enumerate(self.sim_driver.world.assets()):
            ms = self.sim_driver.world.markets_for(sym)
            px = ms[0].mid() if ms else 1.0
            rows.append({"id": sym.lower(), "symbol": sym.lower(), "name": sym.title(), "market_cap_rank": i + 1,
                         "current_price": px, "market_cap": 1e10 / (i + 1), "total_volume": 5e8 / (i + 1),
                         "price_change_percentage_24h": 0.0})
        return rows

    async def _fetch_universe_rows(self) -> List[Dict[str, Any]]:
        u = self.cfg["universe"]
        rows: List[Dict[str, Any]] = []
        for page in range(1, int(u.get("fetch_pages", 2)) + 1):
            rows.extend(await self.cg.markets(page=page, per_page=int(u.get("per_page", 250))))
        now = self.clock()
        if now - self.category_ts > 86400 or not self.category_ids:
            cats: Dict[str, set] = {}
            for cat in u.get("exclude_categories", []):
                try:
                    cats[cat] = {r["id"] for r in await self.cg.markets(category=cat, per_page=250)}
                except Exception:
                    continue
            if cats:
                self.category_ids, self.category_ts = cats, now
        return rows

    async def refresh_universe(self) -> None:
        now = self.clock()
        cache = resolve_path(self.cfg, "data/universe_cache.json")
        pinned_rows: List[Dict[str, Any]] = []
        try:
            if self.sim:
                rows = self._sim_rows()
            else:
                rows = await self._fetch_universe_rows()
                have = {str(r.get("symbol", "")).upper() for r in rows}
                ids = self.cfg["universe"].get("coingecko_ids", {})
                missing = [ids[s] for s in self.cfg["universe"].get("pinned_assets", []) if s not in have and s in ids]
                if missing:
                    pinned_rows = await self.cg.markets(ids=missing, per_page=len(missing))
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"ts": now, "rows": rows, "pinned": pinned_rows,
                                             "categories": {k: sorted(v) for k, v in self.category_ids.items()}}))
            src = "sim" if self.sim else "coingecko"
        except Exception as exc:
            if not cache.exists():
                self.status["universe"] = f"CoinGecko unavailable ({exc!r}); no cached universe"
                if not self.universe_result:
                    self._pinned_only()
                return
            data = json.loads(cache.read_text())
            rows, pinned_rows = data.get("rows", []), data.get("pinned", [])
            self.category_ids = {k: set(v) for k, v in data.get("categories", {}).items()} or self.category_ids
            src = f"cache from {time.strftime('%Y-%m-%d %H:%M', time.localtime(data.get('ts', 0)))} (CoinGecko error: {exc!r})"[:200]
        self.fx.update_from_markets(rows + pinned_rows)
        self.fx.update_from_catalogs(self.catalogs.values())
        u = self.cfg["universe"]

        def usable(r):
            sel = self.selector.preview(AssetInfo.from_row(r), self.catalogs.values(), self.fx, now)
            if not sel.selected:
                return False, "no usable realtime venue"
            if sel.total_volume() < float(u.get("min_usable_volume_usd", 250000)):
                return False, f"insufficient spot volume (${sel.total_volume():,.0f}/24h)"
            return True, ""
        res = self.universe.build(rows, self.category_ids, usable, pinned_rows, now)
        self.universe_result = res
        self.status["universe"] = f"{len(res['members'])} assets + {sum(1 for p in res['pinned'] if p.get('usable'))} pinned ({src})"
        self.status["universe_ts"] = now
        new_info: Dict[str, AssetInfo] = {}
        for r in res["members"]:
            new_info[str(r["symbol"]).upper()] = AssetInfo.from_row(r)
        for p in res["pinned"]:
            if p.get("usable"):
                new_info[str(p["symbol"]).upper()] = AssetInfo.from_row(p, pinned=True)
        for sym in list(self.assets):
            if sym not in new_info:
                del self.assets[sym]
                self.results.pop(sym, None)
                self.selector.forget(sym)
        for i, (sym, info) in enumerate(new_info.items()):
            if sym not in self.assets:
                st = AssetState(sym, self.cfg, stagger=i)
                closes = self.db.read_sync(load_asset_closes, sym, now - 86400)
                st.rehydrate_closes(closes)
                self.assets[sym] = st
        self.info = new_info
        snap = [(now, r.get("id"), str(r.get("symbol")).upper(), r.get("name"), r.get("market_cap_rank"),
                 r.get("market_cap"), 1, 1, 0, 1, "") for r in res["members"]]
        snap += [(now, p.get("id"), str(p.get("symbol")).upper(), p.get("name"), p.get("market_cap_rank"),
                  p.get("market_cap"), 1, 0, 1, 1 if p.get("usable") else 0, p.get("status", "")) for p in res["pinned"]]
        snap += [(now, e.get("id"), e.get("symbol"), e.get("name"), e.get("rank"), e.get("market_cap"), 0, 0, 0, 0,
                  e.get("reason")) for e in res["excluded"]]
        self.db.insert("universe_snapshots", ["ts", "coin_id", "symbol", "name", "rank", "market_cap", "eligible",
                                              "in_universe", "pinned", "usable", "reason"], snap, mode="")

    def _pinned_only(self) -> None:
        now = self.clock()
        for sym in self.cfg["universe"].get("pinned_assets", []):
            self.info[sym] = AssetInfo(symbol=sym, pinned=True)
            self.assets.setdefault(sym, AssetState(sym, self.cfg))
        self.universe_result = {"ts": now, "members": [], "pinned": [{"symbol": s, "status": "universe unavailable"}
                                                                     for s in self.info], "excluded": []}

    async def apply_selection(self, only: Optional[set] = None) -> None:
        now = self.clock()
        specs: List[MarketSpec] = []
        rows = []
        for sym, info in self.info.items():
            if only is None or sym in only:
                sel = self.selector.select(info, self.catalogs.values(), self.fx, now)
                self.selections[sym] = sel
                for m in sel.selected:
                    rows.append((now, sym, m["exchange"], m["symbol"], m["quote"], m["volume_24h_usd"], m["rank"], 1, ""))
                for r in sel.rejected[:20]:
                    rows.append((now, sym, r["exchange"], r["symbol"], None, r.get("volume_24h_usd"), None, 0, r["reason"]))
            sel = self.selections.get(sym)
            if sel is None:
                continue
            for m in sel.selected:
                specs.append(MarketSpec(sym, m["exchange"], m["symbol"], m["quote"], m["volume_24h_usd"]))
                self.market_created.setdefault((m["exchange"], m["symbol"]), now)
        self.db.insert("venue_selection", ["ts", "asset", "exchange", "symbol", "quote", "volume_24h_usd", "rank",
                                           "selected", "reason"], rows, mode="")
        keep = {(s.exchange, s.symbol) for s in specs}
        for k in [k for k in self.market_created if k not in keep]:
            del self.market_created[k]
        if isinstance(self.host, ProcessEngineHost):
            self.host.send_fx(self.fx.rates)
        await self.host.set_markets(specs)
        self.status["markets"] = len(specs)

    async def crosscheck(self, asset: str) -> List[Dict[str, Any]]:
        info = self.info.get(asset)
        if not info or not info.coin_id or self.sim:
            return []
        tick = await self.cg.coin_tickers(info.coin_id)
        out = crosscheck_unsupported(tick, set(self.adapters))
        self.unsupported_top[asset] = out
        return out

    # ------------------------------------------------------------------ loops
    async def _universe_loop(self) -> None:
        iv = float(self.cfg["universe"].get("refresh_minutes", 60)) * 60
        while True:
            await asyncio.sleep(iv)
            try:
                await self.refresh_universe()
                await self.apply_selection()
            except Exception as exc:
                self.status["universe"] = f"refresh error: {exc!r}"[:200]

    async def _discovery_loop(self) -> None:
        iv = float(self.cfg["discovery"].get("refresh_minutes", 45)) * 60
        last = self.clock()
        while True:
            await asyncio.sleep(30)
            try:
                if self.clock() - last >= iv:
                    last = self.clock()
                    await self.refresh_catalogs()
                    await self.apply_selection()
                elif self._reselect:
                    todo, self._reselect = set(self._reselect), set()
                    await self.apply_selection(only=todo)
            except Exception as exc:
                self.status["discovery"] = f"refresh error: {exc!r}"[:200]

    async def _maintenance_loop(self) -> None:
        every = float(self.cfg["storage"].get("retention_every_minutes", 10)) * 60
        while True:
            await asyncio.sleep(60)
            now = self.clock()
            try:
                h = self.host.health().get("exchanges", {})
                rows = []
                for ex, e in h.items():
                    for p in e.get("partitions", []):
                        rows.append((now, ex, p["idx"], p["state"], e["caps"]["book_mode"], p["symbols"], e["streaming"],
                                     0, p["msgs_per_s"], p["reconnects"], p["errors"], p["last_error"]))
                self.db.insert("feed_health_1m", ["ts", "exchange", "partition", "state", "mode", "markets", "live",
                                                  "stale", "msgs", "reconnects", "errors", "last_error"], rows, mode="")
                if int(now // 60) % max(1, int(every // 60)) == 0:
                    await asyncio.to_thread(self.db.call, lambda c: prune(c, self.cfg["storage"], now))
                    await asyncio.to_thread(self.db.call, lambda c: fill_outcomes(c, now))
            except Exception as exc:
                self.status["maintenance_error"] = repr(exc)[:200]

    async def _tick_loop(self) -> None:
        tick_s = float(self.cfg["engine"].get("tick_seconds", 1.0))
        nxt = self.clock()
        while True:
            nxt += tick_s
            await asyncio.sleep(max(0.0, nxt - self.clock()))
            t0 = time.perf_counter()
            try:
                self.tick_once(self.clock())
            except Exception as exc:
                self.status["tick_error"] = repr(exc)[:300]
            ms = (time.perf_counter() - t0) * 1000
            self.status["last_tick_ms"] = round(ms, 1)
            a = self.status.get("avg_tick_ms") or ms
            self.status["avg_tick_ms"] = round(a + 0.05 * (ms - a), 1)
            self.status["ticks"] += 1
            if self.clock() - nxt > 5 * tick_s:
                nxt = self.clock()  # fell behind: resync instead of bursting

    # ------------------------------------------------------------------ tick
    def _intel_ctx(self, asset: str, res_prev: Optional[Dict[str, Any]], now: float) -> Optional[Dict[str, Any]]:
        """Wallet scores for the engine plus an explicit state (OFF / NO KEY / WARMING / UNSUPPORTED / N/A / value).

        Only scores whose state is a real value reach the engine and the radar; everything
        else is None, never 0."""
        c = self._intel_cache.get(asset)
        if c and now - c[0] < 30:
            return c[1]
        icfg = self.cfg["intel"]
        scores = None
        if self.intel is not None:
            info = self.info.get(asset)
            sel = self.selections.get(asset)
            vol = (sel.total_volume() if sel else None) or (info.volume_24h_usd if info else None)
            thin = ((res_prev or {}).get("agg") or {}).get("family", {}).get("thinning", 0.0) if res_prev else 0.0
            scores = compute_scores(asset, self.intel.transfers, self.intel.coverage(asset), now, vol, thin,
                                    whale_candidate_usd=float(icfg.get("whale_candidate_usd", 250000)))
        status = asset_status(asset, enabled=bool(icfg.get("enabled")) and not self.sim,
                              keyed=bool(self.providers and self.providers.keyed()), monitor=self.intel,
                              discovery=self.contract_discovery, labels=self.labels, scores=scores, now=now,
                              warm_seconds=float(icfg.get("warmup_minutes", 60)) * 60.0)
        if scores is not None:
            status["whale_candidates"] = scores.get("whale_candidates") or []
            status["cex_outflow_attributed_share"] = scores.get("cex_outflow_attributed_share")
        self.wallet_status[asset] = status
        ctx = gate_scores(scores, status) if scores is not None else None
        self._intel_cache[asset] = (now, ctx)
        return ctx

    def tick_once(self, now: float) -> None:
        feats = self.host.tick(now)
        verify_to = float(self.cfg["discovery"].get("verify_timeout_seconds", 90))
        wash = float(self.cfg["discovery"].get("wash_volume_to_depth_ratio", 4000))
        new_events: List[Dict[str, Any]] = []
        outcomes = []
        for asset, st in self.assets.items():
            fl = feats.get(asset, [])
            for f in fl:
                k = (f["exchange"], f["symbol"])
                created = self.market_created.get(k, now)
                if f["state"] == "VERIFYING" and now - created > verify_to:
                    self.selector.mark_unavailable(k[0], k[1], f"no order book within {verify_to:.0f}s", now=now)
                    self._reselect.add(asset)
                elif f["state"] == "UNAVAILABLE":
                    self.selector.mark_unavailable(k[0], k[1], f.get("state_reason") or "feed unavailable", now=now)
                    self._reselect.add(asset)
                elif f["state"] == "LIVE" and now - created > 300:
                    depth2 = (f.get("bid_depth_2") or 0) + (f.get("ask_depth_2") or 0)
                    v24 = f.get("discovery_volume_24h_usd") or 0
                    if depth2 > 0 and v24 / depth2 > wash and (f.get("book_coverage_pct") or 0) >= 1.9:
                        self.selector.mark_unavailable(k[0], k[1], f"24h volume {v24 / depth2:,.0f}× visible ±2% depth "
                                                       "(implausible; possible wash volume)", now=now)
                        self._reselect.add(asset)
            sel = self.selections.get(asset)
            res = st.update(now, fl, len(sel.selected) if sel else len(fl), self._intel_ctx(asset, self.results.get(asset), now))
            info = self.info.get(asset)
            res["wallet_status"] = self.wallet_status.get(asset)
            res["name"] = info.name if info else asset
            res["rank"] = info.rank if info else None
            res["pinned"] = bool(info and info.pinned)
            if res.get("price"):
                self.fx.update_live(asset, res["price"])
            evs = self.events.process(res)
            new_events.extend(evs)
            active_alert, alert_events, alert_fired = self.alerts.update(res, now)
            if active_alert:
                res["high_conviction_alert"] = active_alert
            else:
                res["high_conviction_alert"] = None
            for ae in alert_events:
                new_events.append(self.events.add_external(ae))
            if alert_fired:
                outcomes.append((now, asset, "HIGH_CONVICTION", res.get("premove"), res.get("price")))
            for t in res.get("transitions", []):
                if t["kind"] == "status" and t["to"] in ("EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE"):
                    outcomes.append((now, asset, t["to"], res.get("premove"), res.get("price")))
            self.results[asset] = res
        for ae in self.alerts.sweep(now, set(self.assets)):
            new_events.append(self.events.add_external(ae))
        changed = self.alerts.pop_changes()
        if changed:
            self.db.insert("alerts", ALERT_COLS, [alert_row(a) for a in changed], critical=True)
        for gone in [a for a in self.wallet_status if a not in self.assets]:
            self.wallet_status.pop(gone, None)
        if now - self._wallet_summary_ts >= 10.0 or not self.wallet_summary:
            self._wallet_summary_ts = now
            icfg = self.cfg["intel"]
            self.wallet_summary = coverage_summary(
                self.wallet_status, enabled=bool(icfg.get("enabled")) and not self.sim,
                keyed=bool(self.providers and self.providers.keyed()), monitor=self.intel,
                discovery=self.contract_discovery, labels=self.labels,
                chains=self.providers.supported_chains() if self.providers else [])
        self.radar = self.alerts.radar(now, self.wallet_summary, self._feeds_summary())
        if self.intel is not None:
            top = sorted(self.results.values(), key=lambda r: -(r.get("premove") or 0))[:10]
            self.intel.priority_assets = {r["asset"] for r in top if (r.get("premove") or 0) >= 40}

        # persistence (all via the writer thread)
        pa = float(self.cfg["storage"].get("persist_asset_seconds", 5))
        pm = float(self.cfg["storage"].get("persist_market_seconds", 10))
        if now - self._last_persist_a >= pa:
            self._last_persist_a = now
            self.db.insert("asset_metrics_5s", ASSET_5S_COLS,
                           [asset_row_5s(r) for r in self.results.values() if r.get("premove") is not None])
        if now - self._last_persist_m >= pm:
            self._last_persist_m = now
            self.db.insert("market_metrics_10s", MARKET_10S_COLS,
                           [market_row_10s(v) for r in self.results.values() for v in r.get("venues", [])
                            if v.get("state") in ("LIVE", "WARMING", "RESYNCING")])
        mins = self.host.drain_minutes()
        if mins:
            self.db.insert("market_metrics_1m", MARKET_1M_COLS, [market_row_1m(m) for m in mins])
        amins = [m for st in self.assets.values() for m in st.drain_minutes()]
        if amins:
            self.db.insert("asset_metrics_1m", ASSET_1M_COLS, [asset_row_1m(m) for m in amins])
        if new_events:
            self.db.insert("events", EVENT_COLS, [event_row(e) for e in new_events], mode="", critical=True)
            self._emit("events", {"events": _json_safe(new_events)})
        if outcomes:
            self.db.insert("signal_outcomes", ["ts", "asset", "status", "score", "price"], outcomes, mode="")
        self._emit("tick", {})

    def _feeds_summary(self) -> Dict[str, Any]:
        live = total = 0
        for r in self.results.values():
            live += int(r.get("coverage") or 0)
            total += int(r.get("coverage_total") or 0)
        share = (live / total) if total else 0.0
        state = "NO_DATA" if live == 0 else ("DEGRADED" if share < 0.8 else "OK")
        return {"state": state, "live_markets": live, "selected_markets": total, "live_share": round(share, 3)}

    # ------------------------------------------------------------------ payloads
    def top_payload(self) -> Dict[str, Any]:
        rows = []
        for r in self.results.values():
            subs = r.get("subscores") or {}
            rows.append({
                "asset": r["asset"], "name": r.get("name"), "rank": r.get("rank"), "pinned": r.get("pinned"),
                "status": r["status"], "status_since": r.get("status_since"), "premove": r.get("premove"),
                "fast": r.get("fast"), "slow": r.get("slow"), "confidence": r.get("confidence"),
                "liquidity": subs.get("liquidity"), "orderbook": subs.get("orderbook"),
                "buy_pressure": subs.get("buy_pressure"), "cross_venue": subs.get("cross_venue"),
                "mm": subs.get("mm"), "whale": subs.get("whale"), "cex_flow": subs.get("cex_flow"),
                "scarcity": subs.get("scarcity"),
                "r15": (r.get("returns") or {}).get(15), "r60": (r.get("returns") or {}).get(60),
                "price": r.get("price"), "confirmed": r.get("confirmed"), "coverage": r.get("coverage"),
                "coverage_total": r.get("coverage_total"), "families": r.get("families"),
                "reason": r.get("reason"), "cap_reason": r.get("cap_reason"),
                "late_state": (r.get("late") or {}).get("state"),
                "wallet": _wallet_cells(r.get("wallet_status")),
            })

        def key(x):
            grp = 2 if x["status"] in QUIET_GROUP else 1 if x["status"] in LATE_GROUP else 0
            return (grp, -(x["premove"] or 0.0), x["rank"] or 9999)
        rows.sort(key=key)
        for i, x in enumerate(rows):
            x["position"] = i + 1
        counts: Dict[str, int] = {}
        for x in rows:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        max_alerts = int(self.cfg.get("alerts", {}).get("max_active_banners", 3))
        return _json_safe({"ts": self.clock(), "version": __version__, "mode": "sim" if self.sim else "live",
                           "rows": rows, "counts": counts, "alerts": self.alerts.active()[:max_alerts],
                           "radar": self.radar, "wallet_intel": self.wallet_summary,
                           "universe": self.status.get("universe"),
                           "discovery": self.status.get("discovery")})

    def coin_payload(self, asset: str) -> Optional[Dict[str, Any]]:
        asset = asset.upper()
        r = self.results.get(asset)
        if r is None:
            return None
        now = self.clock()
        sel = self.selections.get(asset)
        keys = [(v["exchange"], v["symbol"]) for v in r.get("venues", [])]
        self.host.set_detail_keys(keys)
        books = {f"{ex}|{sym}": self.host.book_summary(ex, sym) for ex, sym in keys}
        leadlag = {}
        live = [v for v in r.get("venues", []) if v.get("state") in ("LIVE", "WARMING")]
        if len(live) >= 2:
            ref = max(live, key=lambda v: v.get("base_depth_1") or v.get("ask_depth_1") or 0)
            series = {v["exchange"]: self.host.mid_series(v["exchange"], v["symbol"], 600, now) for v in live}
            leadlag = {"reference": ref["exchange"], "venues": price_lead_lag(series, ref["exchange"])}
        wallet = None
        if self.intel is not None:
            tok = self.intel.tokens.get(asset)
            wallet = {"coverage": self.intel.coverage(asset), "status": self.intel.status,
                      "token": tok, "balances": entity_balances(self.intel.balances, self.labels, tok["contract"], now) if tok else [],
                      "recent": [t for t in self.intel.transfers if t["asset"] == asset][-50:][::-1]}
        info = self.info.get(asset)
        out = dict(r)
        out.pop("transitions", None)
        out.update({"info": info.__dict__ if info else None,
                    "selection": sel.to_dict() if sel else None, "books": books, "leadlag": leadlag,
                    "onsets": self.assets[asset].onsets.active() if asset in self.assets else [],
                    "high_conviction_alert": self.alerts.get(asset),
                    "wallet_status": self.wallet_status.get(asset),
                    "radar_entry": next((e for e in (self.radar.get("entries") or []) if e.get("asset") == asset), None),
                    "events": self.events.recent(asset, now - 6 * 3600)[-80:],
                    "wallet": wallet, "unsupported_top": self.unsupported_top.get(asset, []),
                    "fx": {q: self.fx.rate(q) for q in {v.get("quote") for v in r.get("venues", [])} if q}})
        return _json_safe(out)

    def health_payload(self) -> Dict[str, Any]:
        h = self.host.health()
        cats = {ex: c.summary() for ex, c in self.catalogs.items()}
        venue_states: Dict[str, int] = {}
        for r in self.results.values():
            for v in r.get("venues", []):
                venue_states[v["state"]] = venue_states.get(v["state"], 0) + 1
        return _json_safe({
            "ts": self.clock(), "version": __version__, "mode": "sim" if self.sim else "live",
            "status": self.status, "engine": h, "catalogs": cats, "venue_states": venue_states,
            "coingecko": self.cg.stats(),
            "etherscan": (self.providers.stats().get("etherscan", {}) if self.providers else {"enabled": False}),
            "intel_status": (self.intel.status if self.intel else "disabled"),
            "storage": self.db.stats(), "fx": self.fx.snapshot(),
            "labels": {"count": len(self.labels.labels), "errors": self.labels.errors[:20]},
            "alerts": {"active": len(self.alerts.active()), "enabled": bool(self.cfg.get("alerts", {}).get("enabled", True)),
                       "radar_state": self.radar.get("state")},
            "wallet_intel": self.wallet_summary,
            "wallet_providers": self.providers.stats() if self.providers else {},
        })

    def universe_payload(self) -> Dict[str, Any]:
        u = dict(self.universe_result or {})
        u["members"] = [{"symbol": str(m.get("symbol")).upper(), "id": m.get("id"), "name": m.get("name"),
                         "rank": m.get("market_cap_rank"),
                         "venues": [x["exchange"] for x in (self.selections.get(str(m.get("symbol")).upper()).selected
                                                            if self.selections.get(str(m.get("symbol")).upper()) else [])]}
                        for m in u.get("members", [])]
        return _json_safe(u)

    async def history(self, asset: str, hours: float, max_points: int = 1500) -> Dict[str, Any]:
        asset = asset.upper()
        now = self.clock()
        h = await self.db.read(asset_history, asset, hours, now, max_points)
        h["venues"] = await self.db.read(venue_history, asset, hours, now, max(250, max_points // 2))
        h["events"] = await self.db.read(events_query, asset, now - hours * 3600, 500)
        h["alerts"] = await self.db.read(alerts_query, now - hours * 3600, asset, 200)
        return _json_safe(h)

    async def timeline(self, asset: str, hours: float = 24.0) -> Dict[str, Any]:
        asset = asset.upper()
        now = self.clock()
        evs = await self.db.read(events_query, asset, now - hours * 3600, 1000)
        return _json_safe({"asset": asset, "hours": hours, "events": evs})

    async def outcomes(self, days: float = 30.0) -> Dict[str, Any]:
        rows = await self.db.read(outcome_summary, self.clock() - days * 86400)
        return _json_safe({"days": days, "by_status": rows,
                           "note": "Forward returns after EMERGING / CONFIRMED / STRONG / HIGH_CONVICTION signals (calibration aid)."})

    def alerts_payload(self) -> Dict[str, Any]:
        return _json_safe({
            "ts": self.clock(),
            "active": self.alerts.active(),
            "radar": self.radar,
            "note": "HIGH_CONVICTION is a strict composite evidence alert, not a probability or proof of a purchase.",
        })

    def radar_payload(self) -> Dict[str, Any]:
        return _json_safe(self.radar)

    async def alert_history(self, days: float = 30.0, limit: int = 500) -> Dict[str, Any]:
        since = self.clock() - days * 86400.0
        rows = await self.db.read(alert_events_query, since, limit)
        alerts = await self.db.read(alerts_query, since, None, limit)
        return _json_safe({"days": days, "events": rows, "alerts": alerts})

    def state_compat(self) -> Dict[str, Any]:
        """v0.6-compatible /api/state shape (for old clients)."""
        assets = {}
        for a, r in self.results.items():
            agg = r.get("agg") or {}
            assets[a] = {"asset": a, "score": r.get("premove"), "status": r.get("status"), "price": r.get("price"),
                         "coverage": r.get("coverage"), "coverage_total": r.get("coverage_total"),
                         "confirmed_venues": r.get("confirmed"), "ask_depth_ratio_vs_baseline": agg.get("ask_ratio"),
                         "buy_ratio_60s": agg.get("buy_share"), "volume_60s": agg.get("vol_60"),
                         "volume_ratio_vs_baseline": agg.get("vol_ratio"), "spread_bps": agg.get("spread_bps"),
                         "venues": [{"venue": v["exchange"], "symbol": v["symbol"], "confirmed": v.get("confirmed"),
                                     "ask_depth_1": v.get("ask_depth_1"), "ask_depth_ratio": v.get("ask1_ratio"),
                                     "buy_ratio_60s": v.get("buy_share_60"), "volume_60s": v.get("vol_60")}
                                    for v in r.get("venues", [])]}
        return _json_safe({"mode": "v0.7 (v0.6-compatible view)", "server_ts": self.clock(), "assets": assets})
