"""Where market states live.

LocalEngineHost (default, `feeds.workers: 1`): market states + feed manager
in this process. Ingestion callbacks are O(changed levels); everything else
runs on the 1 Hz tick.

ProcessEngineHost (`feeds.workers: N`, for Top 250/500): exchanges are
sharded across N worker processes; each worker runs its own feeds + market
states and sends only compact 1 Hz feature rows and 1-minute records back
through a bounded queue (drop-oldest with a counter). Scoring, storage and
the API stay in the main process and use the same interface.
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..feeds.manager import FeedManager
from .market_state import MarketState


@dataclass
class MarketSpec:
    asset: str
    exchange: str
    symbol: str
    quote: str
    volume_24h_usd: float = 0.0


class LocalEngineHost:
    def __init__(self, cfg: Dict[str, Any], adapters: Dict[str, Any], fx: Callable[[str], Optional[float]],
                 rehydrate: Optional[Callable[[MarketSpec], List[Dict[str, Any]]]] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.fx = fx
        self.clock = clock
        self.rehydrate_fn = rehydrate
        self.states: Dict[Tuple[str, str], MarketState] = {}
        self.feeds = FeedManager(adapters, self, cfg["feeds"], clock=clock)
        self.detail_keys: set = set()
        self._n = 0

    # ---- FeedSink
    def on_book(self, ex, sym, bids, asks, ts, resync):
        st = self.states.get((ex, sym))
        if st is not None:
            st.on_book(bids, asks, ts, resync)

    def on_trades(self, ex, sym, trades, ts):
        st = self.states.get((ex, sym))
        if st is not None:
            st.on_trades(trades, ts)

    def on_market_status(self, ex, sym, status, reason, ts):
        st = self.states.get((ex, sym))
        if st is not None:
            st.set_feed_status(status, reason, ts)

    # ---- control
    async def set_markets(self, specs: List[MarketSpec]) -> Dict[str, Any]:
        want = {(s.exchange, s.symbol): s for s in specs}
        removed = [k for k in self.states if k not in want]
        for k in removed:
            del self.states[k]
        added = []
        now = self.clock()
        for k, s in want.items():
            if k in self.states:
                self.states[k].discovery_volume_24h_usd = s.volume_24h_usd
                continue
            self._n += 1
            st = MarketState(s.asset, s.exchange, s.symbol, s.quote, self.fx, self.cfg["engine"], self.cfg["feeds"],
                             s.volume_24h_usd, now=now, stagger=(self._n * 7) % 60)
            if self.rehydrate_fn is not None:
                try:
                    recs = self.rehydrate_fn(s)
                    if recs:
                        st.rehydrate(recs, now)
                except Exception:
                    pass
            self.states[k] = st
            added.append(k)
        desired: Dict[str, List[str]] = {}
        for (ex, sym) in want:
            desired.setdefault(ex, []).append(sym)
        changes = await self.feeds.set_desired(desired)
        return {"added": len(added), "removed": len(removed), "feeds": changes}

    def tick(self, now: float) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for st in self.states.values():
            out.setdefault(st.asset, []).append(st.tick(now))
        return out

    def drain_minutes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for st in self.states.values():
            out.extend(st.drain_minutes())
        return out

    def book_summary(self, ex: str, sym: str) -> Optional[Dict[str, Any]]:
        st = self.states.get((ex, sym))
        return st.book_summary() if st else None

    def mid_series(self, ex: str, sym: str, window: int, now: float) -> List[Optional[float]]:
        st = self.states.get((ex, sym))
        return st.mid_ring.series(int(now), window) if st else []

    def set_detail_keys(self, keys) -> None:
        self.detail_keys = set(keys)

    def health(self) -> Dict[str, Any]:
        return {"mode": "in-process", "exchanges": self.feeds.health(), "markets": len(self.states)}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.feeds.stop()


# --------------------------------------------------------------------------
# Worker processes

def _worker_main(idx: int, cfg: Dict[str, Any], cmd_q, out_q) -> None:  # pragma: no cover - runs in child
    asyncio.run(_worker(idx, cfg, cmd_q, out_q))


async def _worker(idx: int, cfg: Dict[str, Any], cmd_q, out_q) -> None:
    from ..feeds.registry import build_adapters
    from ..storage.db import connect
    from ..storage.history import load_market_minutes
    from ..universe.fx import FxService
    fx = FxService()
    adapters, driver = build_adapters(cfg)
    dtask = asyncio.ensure_future(driver.run()) if driver else None
    db_path = cfg.get("_db_path")
    ro = None

    def rehydrate(spec: MarketSpec):
        nonlocal ro
        if not db_path:
            return []
        try:
            if ro is None:
                ro = connect(db_path, readonly=True)
            since = time.time() - float(cfg["engine"].get("baseline_long_minutes", 1440)) * 60
            return load_market_minutes(ro, spec.asset, spec.exchange, since)
        except Exception:
            return []
    host = LocalEngineHost(cfg, adapters, fx.rate, rehydrate)
    dropped = 0
    stop = False

    async def commands():
        nonlocal stop
        while not stop:
            try:
                cmd = await asyncio.to_thread(cmd_q.get, True, 0.5)
            except queue.Empty:
                continue
            kind = cmd[0]
            if kind == "stop":
                stop = True
            elif kind == "markets":
                await host.set_markets([MarketSpec(**d) for d in cmd[1]])
            elif kind == "fx":
                fx.rates.update(cmd[1])
            elif kind == "detail":
                host.set_detail_keys([tuple(k) for k in cmd[1]])
    ctask = asyncio.ensure_future(commands())
    tick_s = float(cfg["engine"].get("tick_seconds", 1.0))
    while not stop:
        await asyncio.sleep(tick_s)
        now = time.time()
        feats = host.tick(now)
        detail = {}
        for k in host.detail_keys:
            if k in host.states:
                detail["|".join(k)] = {"book": host.book_summary(*k), "mids": host.mid_series(k[0], k[1], 600, now)}
        msg = ("tick", idx, now, feats, host.drain_minutes(), host.health(), detail, dropped)
        try:
            out_q.put_nowait(msg)
        except queue.Full:
            dropped += 1
    ctask.cancel()
    await host.stop()
    if dtask:
        dtask.cancel()


class ProcessEngineHost:
    def __init__(self, cfg: Dict[str, Any], n_workers: int, exchanges: List[str], db_path: Optional[str] = None,
                 queue_size: int = 64):
        self.cfg = dict(cfg)
        self.cfg["_db_path"] = db_path
        self.n = max(1, n_workers)
        self.exchanges = exchanges
        self.ctx = mp.get_context("spawn")
        self.cmd_qs = [self.ctx.Queue() for _ in range(self.n)]
        self.out_q = self.ctx.Queue(maxsize=queue_size)
        self.procs: List[Any] = []
        self.latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.minutes: List[Dict[str, Any]] = []
        self.worker_health: Dict[int, Dict[str, Any]] = {}
        self.worker_seen: Dict[int, float] = {}
        self.worker_dropped: Dict[int, int] = {}
        self.detail: Dict[str, Any] = {}
        self.assign: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _shard(self, specs: List[MarketSpec]) -> Dict[int, List[MarketSpec]]:
        # Balance by market count, keeping each exchange in exactly one worker.
        counts: Dict[str, int] = {}
        for s in specs:
            counts[s.exchange] = counts.get(s.exchange, 0) + 1
        load = [0] * self.n
        for ex in sorted(counts, key=lambda e: -counts[e]):
            if ex not in self.assign:
                self.assign[ex] = min(range(self.n), key=lambda i: load[i])
            load[self.assign[ex]] += counts[ex]
        out: Dict[int, List[MarketSpec]] = {i: [] for i in range(self.n)}
        for s in specs:
            out[self.assign[s.exchange]].append(s)
        return out

    async def start(self) -> None:
        for i in range(self.n):
            p = self.ctx.Process(target=_worker_main, args=(i, self.cfg, self.cmd_qs[i], self.out_q), daemon=True,
                                 name=f"feed-worker-{i}")
            p.start()
            self.procs.append(p)
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="worker-reader")
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self.out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            _, idx, ts, feats, minutes, health, detail, dropped = msg
            with self._lock:
                for asset, rows in feats.items():
                    for f in rows:
                        self.latest[(f["exchange"], f["symbol"])] = f
                self.minutes.extend(minutes)
                self.worker_health[idx] = health
                self.worker_seen[idx] = ts
                self.worker_dropped[idx] = dropped
                self.detail.update(detail)

    async def set_markets(self, specs: List[MarketSpec]) -> Dict[str, Any]:
        shards = self._shard(specs)
        keep = {(s.exchange, s.symbol) for s in specs}
        with self._lock:
            for k in [k for k in self.latest if k not in keep]:
                del self.latest[k]
        for i, lst in shards.items():
            self.cmd_qs[i].put(("markets", [asdict(s) for s in lst]))
        return {"workers": self.n, "per_worker": {i: len(v) for i, v in shards.items()}}

    def send_fx(self, rates: Dict[str, float]) -> None:
        for q in self.cmd_qs:
            q.put(("fx", dict(rates)))

    def tick(self, now: float) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        with self._lock:
            for f in self.latest.values():
                g = dict(f)
                age = now - float(g.get("ts") or 0)
                if age > 10:  # worker silent: never let old data look live
                    g["state"], g["state_reason"] = "STALE", f"feed worker silent for {age:.0f}s"
                out.setdefault(g["asset"], []).append(g)
        return out

    def drain_minutes(self) -> List[Dict[str, Any]]:
        with self._lock:
            out, self.minutes = self.minutes, []
        return out

    def book_summary(self, ex: str, sym: str) -> Optional[Dict[str, Any]]:
        d = self.detail.get(f"{ex}|{sym}")
        return d.get("book") if d else None

    def mid_series(self, ex: str, sym: str, window: int, now: float) -> List[Optional[float]]:
        d = self.detail.get(f"{ex}|{sym}")
        return (d.get("mids") or [])[-window:] if d else []

    def set_detail_keys(self, keys) -> None:
        keys = [list(k) for k in keys]
        for q in self.cmd_qs:
            q.put(("detail", keys))

    def health(self) -> Dict[str, Any]:
        with self._lock:
            ex = {}
            for h in self.worker_health.values():
                ex.update(h.get("exchanges", {}))
            return {"mode": f"{self.n} worker processes", "exchanges": ex, "markets": len(self.latest),
                    "workers": [{"idx": i, "alive": p.is_alive(), "last_seen": self.worker_seen.get(i),
                                 "dropped_frames": self.worker_dropped.get(i, 0)} for i, p in enumerate(self.procs)]}

    async def stop(self) -> None:
        for q in self.cmd_qs:
            try:
                q.put(("stop",))
            except Exception:
                pass
        for p in self.procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
        self._stop.set()
