"""Feed manager: per-exchange partitions, supervision, health.

* Subscriptions are grouped by exchange and split into partitions of at most
  `max_symbols_per_connection` symbols. Each partition owns its own stream
  client (its own websocket connections), so a failure stays local.
* Each stream loop (book or trades; per symbol or per multi-symbol chunk)
  retries transient errors with exponential backoff + jitter. Permanent
  errors (bad symbol, not supported) mark that market UNAVAILABLE.
* A multi-symbol chunk that hits a symbol error falls back to per-symbol loops.
* Client errors (the client instance itself is unusable: library lifecycle bugs,
  closed instance, wrong event loop) rebuild the partition's client once per
  generation, with backoff; they never mark a market UNAVAILABLE.
* Every loop is bound to the client generation it was started with; a loop never
  touches a newer, closed or missing client.
* Circuit breaker: too many failures in a window while the partition delivers no
  data at all → partition pauses for a cooldown (state CIRCUIT_OPEN), then restarts
  with a fresh client. A partition that still delivers data for some markets never
  trips it, so one failing market cannot take down the healthy ones.
* Watchdog: a partition that receives nothing for too long is restarted
  (half-open sockets).
* One exchange disconnecting never affects the others.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set

from ..util import chunked
from .base import ExchangeAdapter, FeedSink, classify_error, trades_from_ccxt
from .capabilities import ExchangeCaps, resolve_caps


class Partition:
    def __init__(self, mgr: "FeedManager", ex: "ExchangeRuntime", idx: int, symbols: List[str]):
        self.mgr = mgr
        self.ex = ex
        self.idx = idx
        self.symbols: List[str] = list(symbols)
        self.state = "STARTING"
        self.task: Optional[asyncio.Task] = None
        self.loops: Dict[str, asyncio.Task] = {}
        self.client = None
        self.generation = 0
        self.book_msgs = 0
        self.trade_msgs = 0
        self.reconnects = 0
        self.rebuilds = 0
        self.breaker_trips = 0
        self.consecutive_rebuilds = 0
        self.errors = 0
        self.last_error = ""
        self.last_msg_ts = 0.0
        self.last_book_ts = 0.0
        self.last_data_ts = 0.0          # last real message (0 until data has flowed)
        self.started_ts = 0.0
        self.failures: Deque[float] = deque(maxlen=200)
        self.breaker_until = 0.0
        self.single_fallback: Set[str] = set()
        self.dirty = False
        self._rebuild_requested_gen = -1
        self._restart = asyncio.Event()

    # ---- bookkeeping
    def note_msg(self, kind: str) -> None:
        self.last_msg_ts = self.last_data_ts = self.mgr.clock()
        self.consecutive_rebuilds = 0
        if kind == "book":
            self.book_msgs += 1
            self.last_book_ts = self.last_msg_ts
        else:
            self.trade_msgs += 1
        if self.state in ("STARTING", "CONNECTING", "RECONNECTING"):
            self.state = "LIVE"

    def note_failure(self, exc: BaseException) -> None:
        now = self.mgr.clock()
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.failures.append(now)
        win = float(self.mgr.cfg.get("breaker_window_seconds", 300))
        n = sum(1 for t in self.failures if now - t <= win)
        # Isolation: while any market of this partition still delivers data, a failing market
        # keeps retrying on its own backoff and never trips the breaker for the healthy ones.
        healthy_s = float(self.mgr.cfg.get("breaker_healthy_seconds", 15))
        healthy = self.last_data_ts > 0 and now - self.last_data_ts <= healthy_s
        if n >= int(self.mgr.cfg.get("breaker_failures", 6)) and self.breaker_until <= now and not healthy:
            self.breaker_until = now + float(self.mgr.cfg.get("breaker_cooldown_seconds", 300))
            self.breaker_trips += 1
            self.failures.clear()
            self._restart.set()

    def request_rebuild(self, client, exc: BaseException) -> None:
        """A loop found its client instance unusable: rebuild it (once per generation)."""
        if client is not self.client or self._rebuild_requested_gen == self.generation:
            return  # a loop of an older generation, or already requested by a sibling loop
        self._rebuild_requested_gen = self.generation
        self.rebuilds += 1
        self.consecutive_rebuilds += 1
        self.note_failure(exc)
        reason = f"client rebuild after {type(exc).__name__}: {exc}"[:200]
        for s in self.symbols:
            self.mgr.sink.on_market_status(self.ex.name, s, "RECONNECTING", reason, self.mgr.clock())
        self._restart.set()

    def backoff(self, attempt: int) -> float:
        base = float(self.mgr.cfg.get("backoff_initial_seconds", 1.0))
        cap = float(self.mgr.cfg.get("backoff_max_seconds", 60.0))
        d = min(cap, base * (2 ** min(attempt, 10)))
        return d * (0.75 + 0.5 * self.mgr.rng.random())

    # ---- loops (each bound to the client of the generation that started it)
    def _failed(self, client, exc: BaseException, symbols: List[str], status: bool = True) -> str:
        """Common error handling. Returns the error class: 'permanent', 'client' or 'transient'."""
        kind = classify_error(exc)
        if kind == "client":
            self.request_rebuild(client, exc)
        elif kind == "transient":
            self.note_failure(exc)
            if status:
                for s in symbols:
                    self.mgr.sink.on_market_status(self.ex.name, s, "DISCONNECTED",
                                                   f"{type(exc).__name__}: {exc}"[:200], self.mgr.clock())
        return kind

    async def _book_single(self, client, symbol: str) -> None:
        caps, sink, ex = self.ex.caps, self.mgr.sink, self.ex.name
        resync, attempt, first = True, 0, True
        while client is self.client:
            try:
                ob = await client.watch_book(symbol, caps.book_limit)
                now = self.mgr.clock()
                sink.on_book(ex, symbol, ob.get("bids") or [], ob.get("asks") or [], now, resync)
                if first or resync:
                    sink.on_market_status(ex, symbol, "STREAMING", "", now)
                resync, attempt, first = False, 0, False
                self.note_msg("book")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if classify_error(exc) == "permanent":
                    sink.on_market_status(ex, symbol, "UNAVAILABLE", f"{type(exc).__name__}: {exc}"[:200], self.mgr.clock())
                    self.last_error = f"{symbol}: {type(exc).__name__}"
                    return
                if self._failed(client, exc, [symbol]) == "client":
                    return  # the supervisor rebuilds the client and restarts every loop
                resync = True
                await self.mgr.sleep(self.backoff(attempt))
                attempt += 1

    async def _books_multi(self, client, symbols: List[str]) -> None:
        caps, sink, ex = self.ex.caps, self.mgr.sink, self.ex.name
        resync_all, attempt = True, 0
        streaming: Set[str] = set()
        while client is self.client:
            try:
                ob = await client.watch_books(symbols, caps.book_limit)
                now = self.mgr.clock()
                sym = ob.get("symbol")
                if sym not in symbols:
                    continue
                resync = resync_all or sym not in streaming
                sink.on_book(ex, sym, ob.get("bids") or [], ob.get("asks") or [], now, resync)
                if sym not in streaming:
                    streaming.add(sym)
                    sink.on_market_status(ex, sym, "STREAMING", "", now)
                if resync_all and len(streaming) >= len(symbols):
                    resync_all = False
                attempt = 0
                self.note_msg("book")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if classify_error(exc) == "permanent":
                    # One bad symbol must not take the chunk down: fall back to per-symbol loops.
                    self.single_fallback.update(symbols)
                    self.last_error = f"multi-symbol book call failed ({type(exc).__name__}); per-symbol fallback"
                    for s in symbols:
                        self._spawn(f"book:{s}", self._book_single(client, s))
                    return
                if self._failed(client, exc, symbols) == "client":
                    return
                streaming.clear()
                resync_all = True
                await self.mgr.sleep(self.backoff(attempt))
                attempt += 1

    async def _trades_single(self, client, symbol: str) -> None:
        sink, ex = self.mgr.sink, self.ex.name
        attempt = 0
        while client is self.client:
            try:
                trades = await client.watch_trades(symbol)
                now = self.mgr.clock()
                grouped = trades_from_ccxt(trades)
                rows = grouped.get(symbol) or grouped.get(None) or [t for g in grouped.values() for t in g]
                if rows:
                    sink.on_trades(ex, symbol, rows, now)
                attempt = 0
                self.note_msg("trade")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if classify_error(exc) == "permanent":
                    self.last_error = f"{symbol} trades: {type(exc).__name__}"
                    return  # book may still work; trade tape just unavailable
                if self._failed(client, exc, [symbol], status=False) == "client":
                    return
                await self.mgr.sleep(self.backoff(attempt))
                attempt += 1

    async def _trades_multi(self, client, symbols: List[str]) -> None:
        sink, ex = self.mgr.sink, self.ex.name
        attempt = 0
        while client is self.client:
            try:
                trades = await client.watch_trades_multi(symbols)
                now = self.mgr.clock()
                for sym, rows in trades_from_ccxt(trades).items():
                    if sym in symbols and rows:
                        sink.on_trades(ex, sym, rows, now)
                attempt = 0
                self.note_msg("trade")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if classify_error(exc) == "permanent":
                    for s in symbols:
                        self._spawn(f"trades:{s}", self._trades_single(client, s))
                    return
                if self._failed(client, exc, symbols, status=False) == "client":
                    return
                await self.mgr.sleep(self.backoff(attempt))
                attempt += 1

    def _spawn(self, name: str, coro) -> None:
        old = self.loops.get(name)
        if old and not old.done():
            old.cancel()
        self.loops[name] = asyncio.ensure_future(coro)

    async def _start_loops(self, client) -> None:
        caps = self.ex.caps
        syms = list(self.symbols)

        async def pace_or_abort() -> bool:
            await self.mgr.sleep(caps.subscribe_pace_s)
            return self._restart.is_set()
        if caps.book_mode == "multi":
            for chunk in chunked(syms, caps.max_symbols_per_call):
                self._spawn("books:" + ",".join(chunk), self._books_multi(client, chunk))
                if await pace_or_abort():
                    return
        elif caps.book_mode == "single":
            for s in syms:
                self._spawn(f"book:{s}", self._book_single(client, s))
                if await pace_or_abort():
                    return
        else:
            for s in syms:
                self.mgr.sink.on_market_status(self.ex.name, s, "UNAVAILABLE",
                                               "exchange has no websocket order book in the installed ccxt", self.mgr.clock())
        if caps.trade_mode == "multi":
            for chunk in chunked(syms, caps.max_symbols_per_call):
                self._spawn("trades:" + ",".join(chunk), self._trades_multi(client, chunk))
                if await pace_or_abort():
                    return
        elif caps.trade_mode == "single":
            for s in syms:
                self._spawn(f"trades:{s}", self._trades_single(client, s))
                if await pace_or_abort():
                    return

    async def _stop_loops(self) -> None:
        """Cancel and await every loop, then close the client exactly once (never reused)."""
        loops = list(self.loops.values())
        for t in loops:
            t.cancel()
        for t in loops:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.loops.clear()
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def run(self) -> None:
        """Supervisor: (re)create client, run loops, watchdog, circuit breaker."""
        stale_s = max(float(self.mgr.cfg.get("watchdog_min_seconds", 60.0)),
                      3.0 * float(self.mgr.cfg.get("stale_book_seconds", 20)))
        first = True
        try:
            while True:
                now = self.mgr.clock()
                if self.breaker_until > now:
                    self.state = "CIRCUIT_OPEN"
                    for s in self.symbols:
                        self.mgr.sink.on_market_status(self.ex.name, s, "CIRCUIT_OPEN",
                                                       f"circuit breaker open: {self.last_error}", now)
                    await self.mgr.sleep(self.breaker_until - now)
                    continue
                if not first:
                    self.reconnects += 1
                    if self.consecutive_rebuilds:
                        # a client that keeps failing before any data flows is rebuilt with backoff
                        self.state = "RECONNECTING"
                        await self.mgr.sleep(self.backoff(min(self.consecutive_rebuilds - 1, 6)))
                self.state = "CONNECTING" if first else "RECONNECTING"
                first = False
                self._restart.clear()
                self.generation += 1
                self.started_ts = self.mgr.clock()
                self.last_msg_ts = self.last_book_ts = self.started_ts
                try:
                    self.client = client = self.ex.adapter.new_stream_client()
                    await self._start_loops(client)
                    while not self._restart.is_set():
                        await self.mgr.sleep(self.mgr.watchdog_interval)
                        if self._restart.is_set():
                            break
                        live_loops = [t for t in self.loops.values() if not t.done()]
                        if self.symbols and not live_loops:
                            # every loop ended on permanent errors: nothing to stream until the set changes
                            self.state = "UNAVAILABLE"
                            await self.mgr.sleep(self.mgr.watchdog_interval * 12)
                            break
                        # A hung order-book stream is caught even while trades still arrive.
                        ref = self.last_book_ts if self.ex.caps.book_mode != "none" else self.last_msg_ts
                        if self.mgr.clock() - ref > stale_s and self.symbols:
                            self.note_failure(TimeoutError(f"no order-book messages for {stale_s:.1f}s (watchdog)"))
                            for s in self.symbols:
                                self.mgr.sink.on_market_status(self.ex.name, s, "DISCONNECTED", "watchdog: silent connection",
                                                               self.mgr.clock())
                            break
                        if self.state == "LIVE" and any(t.done() for t in self.loops.values()):
                            self.state = "DEGRADED"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # client construction failed etc.
                    self.note_failure(exc)
                    await self.mgr.sleep(self.backoff(min(self.reconnects, 6)))
                await self._stop_loops()
        finally:
            # cancellation (unsubscribe / shutdown) or any unexpected exit: never leak loops or sockets
            await asyncio.shield(self._stop_loops())
            self.state = "STOPPED"

    def health(self) -> Dict[str, Any]:
        now = self.mgr.clock()
        age = (now - self.last_msg_ts) if self.last_msg_ts else None
        up = max(1.0, now - self.started_ts) if self.started_ts else 1.0
        return {"idx": self.idx, "state": self.state, "symbols": len(self.symbols),
                "book_msgs": self.book_msgs, "trade_msgs": self.trade_msgs,
                "msgs_per_s": round((self.book_msgs + self.trade_msgs) / up, 2),
                "last_msg_age_s": round(age, 1) if age is not None else None,
                "generation": self.generation, "reconnects": self.reconnects, "rebuilds": self.rebuilds,
                "breaker_trips": self.breaker_trips, "errors": self.errors, "last_error": self.last_error,
                "single_fallback": sorted(self.single_fallback),
                "breaker_open_for_s": round(max(0.0, self.breaker_until - now), 1)}


class ExchangeRuntime:
    def __init__(self, name: str, adapter: ExchangeAdapter, caps: ExchangeCaps):
        self.name = name
        self.adapter = adapter
        self.caps = caps
        self.partitions: List[Partition] = []


class FeedManager:
    def __init__(self, adapters: Dict[str, ExchangeAdapter], sink: FeedSink, fcfg: Dict[str, Any],
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep, seed: int = 1,
                 watchdog_interval: float = 5.0):
        self.adapters = adapters
        self.sink = sink
        self.cfg = fcfg
        self.clock = clock
        self.sleep = sleep
        self.rng = random.Random(seed)
        self.watchdog_interval = watchdog_interval
        self.exchanges: Dict[str, ExchangeRuntime] = {}
        self.market_status: Dict[tuple, Dict[str, Any]] = {}
        self._orig_sink_status = sink.on_market_status
        sink_self = self

        def _status(ex, sym, status, reason, ts):
            sink_self.market_status[(ex, sym)] = {"status": status, "reason": reason, "ts": ts}
            sink_self._orig_sink_status(ex, sym, status, reason, ts)
        self.sink = _StatusTap(sink, _status)

    def caps_for(self, exchange: str) -> ExchangeCaps:
        rt = self.exchanges.get(exchange)
        if rt:
            return rt.caps
        ad = self.adapters[exchange]
        try:
            has = ad.has()
        except Exception:
            has = {}
        return resolve_caps(exchange, has, self.cfg.get("exchange_overrides"))

    async def set_desired(self, desired: Dict[str, List[str]]) -> Dict[str, Any]:
        """Diff desired subscriptions against running partitions; restart only what changed."""
        changes = {"started": 0, "restarted": 0, "stopped": 0}
        for ex in list(self.exchanges):
            if ex not in desired or not desired[ex]:
                tasks = [p.task for p in self.exchanges[ex].partitions if p.task]
                for t in tasks:
                    t.cancel()
                for t in tasks:  # wait until loops are stopped and clients closed
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                changes["stopped"] += len(self.exchanges[ex].partitions)
                del self.exchanges[ex]
        for ex, symbols in desired.items():
            if not symbols or ex not in self.adapters:
                continue
            rt = self.exchanges.get(ex)
            if rt is None:
                rt = ExchangeRuntime(ex, self.adapters[ex], self.caps_for(ex))
                self.exchanges[ex] = rt
            want = list(dict.fromkeys(symbols))
            want_set = set(want)
            cap = max(1, rt.caps.max_symbols_per_connection)
            assigned: Set[str] = set()
            for p in rt.partitions:
                keep = [s for s in p.symbols if s in want_set]
                if keep != p.symbols:
                    p.symbols = keep
                    p.dirty = True
                assigned.update(keep)
            new = [s for s in want if s not in assigned]
            for p in rt.partitions:
                room = cap - len(p.symbols)
                if room > 0 and new:
                    p.symbols.extend(new[:room])
                    new = new[room:]
                    p.dirty = True
            for chunk in chunked(new, cap) if new else []:
                p = Partition(self, rt, len(rt.partitions), chunk)
                p.dirty = True
                rt.partitions.append(p)
            for p in list(rt.partitions):
                if not p.dirty:
                    continue
                p.dirty = False
                if p.task:
                    p.task.cancel()
                    try:
                        await p.task
                    except (asyncio.CancelledError, Exception):
                        pass
                    changes["restarted"] += 1
                else:
                    changes["started"] += 1
                if not p.symbols:
                    rt.partitions.remove(p)
                    continue
                for s in p.symbols:
                    self.sink.on_market_status(ex, s, "SUBSCRIBING", "", self.clock())
                p.task = asyncio.ensure_future(p.run())
        return changes

    async def stop(self) -> None:
        tasks = [p.task for rt in self.exchanges.values() for p in rt.partitions if p.task]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.exchanges.clear()

    def health(self) -> Dict[str, Any]:
        out = {}
        for ex, rt in self.exchanges.items():
            parts = [p.health() for p in rt.partitions]
            syms = [s for p in rt.partitions for s in p.symbols]
            st = [self.market_status.get((ex, s), {}).get("status") for s in syms]
            streaming = sum(1 for x in st if x == "STREAMING")
            unavailable = [{"symbol": s, "reason": self.market_status.get((ex, s), {}).get("reason", "")}
                           for s, x in zip(syms, st) if x == "UNAVAILABLE"]
            states = {p["state"] for p in parts}
            if parts and states == {"CIRCUIT_OPEN"}:
                state = "CIRCUIT_OPEN"
            elif streaming == 0:
                state = "DISCONNECTED" if syms else "IDLE"
            elif streaming < len(syms):
                state = "PARTIAL"
            else:
                state = "LIVE"
            out[ex] = {"exchange": ex, "state": state, "markets": len(syms), "streaming": streaming,
                       "unavailable": unavailable, "caps": rt.caps.to_dict(), "partitions": parts,
                       "msgs_per_s": round(sum(p["msgs_per_s"] for p in parts), 2),
                       "reconnects": sum(p["reconnects"] for p in parts), "errors": sum(p["errors"] for p in parts)}
        return out


class _StatusTap:
    """Wraps a sink so the manager can observe market status updates."""

    def __init__(self, sink: FeedSink, status_fn):
        self._sink = sink
        self.on_market_status = status_fn

    def on_book(self, *a, **kw):
        return self._sink.on_book(*a, **kw)

    def on_trades(self, *a, **kw):
        return self._sink.on_trades(*a, **kw)
