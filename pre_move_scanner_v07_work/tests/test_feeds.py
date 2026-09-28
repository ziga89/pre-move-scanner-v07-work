import asyncio
import unittest

from tests.helpers import cfg
from server.feeds.base import classify_error, trades_from_ccxt
from server.feeds.capabilities import resolve_caps
from server.feeds.ccxt_adapter import CcxtAdapter
from server.feeds.manager import FeedManager
from server.feeds.sim_adapter import BadSymbol, NetworkError, SimAdapter, SimDriver
from server.sim import SimWorld

MULTI = {"watchOrderBookForSymbols": True, "watchOrderBook": True, "watchTradesForSymbols": True, "watchTrades": True}
SINGLE = {"watchOrderBook": True, "watchTrades": True}


def fast_cfg(**kw):
    c = dict(cfg()["feeds"])
    c.update(backoff_initial_seconds=0.01, backoff_max_seconds=0.05, breaker_failures=6,
             breaker_window_seconds=5, breaker_cooldown_seconds=0.3, stale_book_seconds=0.05,
             watchdog_min_seconds=0.3,
             exchange_overrides={x: {"subscribe_pace_s": 0.001} for x in ("fakex", "badex", "goodex", "binance")})
    ov = kw.pop("exchange_overrides", {})
    c.update(kw)
    for x, v in ov.items():
        c["exchange_overrides"].setdefault(x, {}).update(v)
    return c


class RecSink:
    def __init__(self):
        self.books, self.trades, self.status, self.resyncs = {}, {}, {}, {}

    def on_book(self, ex, sym, bids, asks, ts, resync):
        self.books[(ex, sym)] = self.books.get((ex, sym), 0) + 1
        if resync:
            self.resyncs[(ex, sym)] = self.resyncs.get((ex, sym), 0) + 1

    def on_trades(self, ex, sym, trades, ts):
        self.trades[(ex, sym)] = self.trades.get((ex, sym), 0) + len(trades)

    def on_market_status(self, ex, sym, status, reason, ts):
        self.status.setdefault((ex, sym), []).append(status)


class FakeClient:
    def __init__(self, plan, has):
        self.plan = plan  # symbol -> behaviour
        self.has = has
        self.calls = {}

    async def _one(self, sym):
        b = self.plan.get(sym, "ok")
        n = self.calls[sym] = self.calls.get(sym, 0) + 1
        if b == "bad":
            raise BadSymbol(sym)
        if b == "down":
            raise NetworkError("down")
        if b == "flaky" and n in (3, 4):
            raise NetworkError("blip")
        if b == "hang":
            await asyncio.sleep(3600)
        await asyncio.sleep(0.005)
        return {"symbol": sym, "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}

    async def watch_book(self, symbol, limit):
        return await self._one(symbol)

    async def watch_books(self, symbols, limit):
        for s in symbols:
            if self.plan.get(s) == "bad":
                raise BadSymbol(s)
        s = symbols[sum(self.calls.values()) % len(symbols)]
        return await self._one(s)

    async def watch_trades(self, symbol):
        await asyncio.sleep(0.01)
        if self.plan.get(symbol) in ("bad", "down"):
            raise NetworkError("down") if self.plan.get(symbol) == "down" else BadSymbol(symbol)
        return [{"symbol": symbol, "timestamp": 1_000_000, "price": 100.0, "amount": 1.0, "side": "buy", "id": "x"}]

    async def watch_trades_multi(self, symbols):
        await asyncio.sleep(0.01)
        return [{"symbol": s, "timestamp": 1_000_000, "price": 100.0, "amount": 1.0, "side": "sell", "id": s}
                for s in symbols if self.plan.get(s) != "bad"]

    async def close(self):
        pass


class FakeAdapter:
    def __init__(self, name, has, plan=None):
        self.exchange = name
        self._has = has
        self.plan = plan or {}
        self.clients = 0

    def has(self):
        return self._has

    def new_stream_client(self):
        self.clients += 1
        return FakeClient(self.plan, self._has)

    async def load_catalog(self):
        return None

    async def close(self):
        pass


def run(coro):
    return asyncio.run(coro)


class CapabilityTest(unittest.TestCase):
    def test_multi_only_when_policy_and_runtime_agree(self):
        self.assertEqual(resolve_caps("binance", MULTI).book_mode, "multi")
        self.assertEqual(resolve_caps("binance", SINGLE).book_mode, "single")   # ccxt lacks multi
        self.assertEqual(resolve_caps("mexc", MULTI).book_mode, "single")       # policy disallows multi
        self.assertEqual(resolve_caps("bybit", MULTI).max_symbols_per_call, 10)
        self.assertEqual(resolve_caps("bitrue", {"watchOrderBook": True}).trade_mode, "none")
        unk = resolve_caps("someex", MULTI)
        self.assertEqual((unk.book_mode, unk.max_symbols_per_connection), ("single", 20))
        ov = resolve_caps("mexc", MULTI, {"mexc": {"max_symbols_per_connection": 5, "multi": True}})
        self.assertEqual((ov.book_mode, ov.max_symbols_per_connection), ("multi", 5))
        self.assertEqual(resolve_caps("okx", {"watchOrderBookForSymbols": "emulated", "watchOrderBook": True}).book_mode, "multi")

    def test_error_classification(self):
        self.assertEqual(classify_error(BadSymbol("x")), "permanent")
        self.assertEqual(classify_error(NetworkError("x")), "transient")
        self.assertEqual(classify_error(TimeoutError()), "transient")

    def test_trades_conversion(self):
        g = trades_from_ccxt([{"symbol": "A/USDT", "timestamp": 1500, "price": 2, "amount": 3, "side": "buy", "id": 7}])
        self.assertEqual(g["A/USDT"][0], (1.5, 2.0, 3.0, "buy", "7"))


class ManagerTest(unittest.TestCase):
    def test_partitioning_and_diffing(self):
        async def go():
            sink = RecSink()
            ad = FakeAdapter("fakex", SINGLE)
            fm = FeedManager({"fakex": ad}, sink, fast_cfg(exchange_overrides={"fakex": {"max_symbols_per_connection": 4}}),
                             watchdog_interval=0.02)
            syms = [f"C{i}/USDT" for i in range(10)]
            await fm.set_desired({"fakex": syms})
            parts = fm.exchanges["fakex"].partitions
            self.assertEqual([len(p.symbols) for p in parts], [4, 4, 2])
            await asyncio.sleep(0.3)
            tasks_before = [p.task for p in parts]
            ch = await fm.set_desired({"fakex": syms[:9] + ["NEW/USDT"]})  # drop C9 (part 2), add NEW (part 2)
            self.assertEqual(ch["restarted"], 1)
            self.assertIs(parts[0].task, tasks_before[0])
            self.assertIs(parts[1].task, tasks_before[1])
            await asyncio.sleep(0.3)
            self.assertGreater(sink.books.get(("fakex", "NEW/USDT"), 0), 0)
            await fm.stop()
        run(go())

    def test_transient_error_reconnects_with_resync_and_bad_symbol_isolated(self):
        async def go():
            sink = RecSink()
            ad = FakeAdapter("fakex", SINGLE, {"A/USDT": "flaky", "BAD/USDT": "bad"})
            fm = FeedManager({"fakex": ad}, sink, fast_cfg(), watchdog_interval=0.02)
            await fm.set_desired({"fakex": ["A/USDT", "B/USDT", "BAD/USDT"]})
            await asyncio.sleep(0.5)
            st = sink.status
            self.assertIn("DISCONNECTED", st[("fakex", "A/USDT")])
            self.assertEqual(st[("fakex", "A/USDT")][-1], "STREAMING")
            self.assertGreaterEqual(sink.resyncs[("fakex", "A/USDT")], 2)  # first book + after reconnect
            self.assertEqual(st[("fakex", "BAD/USDT")][-1], "UNAVAILABLE")
            self.assertGreater(sink.books[("fakex", "B/USDT")], 10)
            h = fm.health()["fakex"]
            self.assertEqual(h["state"], "PARTIAL")
            self.assertEqual(h["unavailable"][0]["symbol"], "BAD/USDT")
            await fm.stop()
        run(go())

    def test_multi_symbol_chunk_falls_back_to_single(self):
        async def go():
            sink = RecSink()
            ad = FakeAdapter("binance", MULTI, {"BAD/USDT": "bad"})
            fm = FeedManager({"binance": ad}, sink, fast_cfg(), watchdog_interval=0.02)
            await fm.set_desired({"binance": ["A/USDT", "B/USDT", "BAD/USDT"]})
            await asyncio.sleep(0.4)
            self.assertEqual(sink.status[("binance", "BAD/USDT")][-1], "UNAVAILABLE")
            self.assertGreater(sink.books.get(("binance", "A/USDT"), 0), 3)
            self.assertGreater(sink.books.get(("binance", "B/USDT"), 0), 3)
            p = fm.exchanges["binance"].partitions[0]
            self.assertEqual(sorted(p.single_fallback), ["A/USDT", "B/USDT", "BAD/USDT"])
            await fm.stop()
        run(go())

    def test_circuit_breaker_and_exchange_isolation(self):
        async def go():
            sink = RecSink()
            bad = FakeAdapter("badex", SINGLE, {"A/USDT": "down", "B/USDT": "down"})
            good = FakeAdapter("goodex", SINGLE)
            fm = FeedManager({"badex": bad, "goodex": good}, sink, fast_cfg(breaker_failures=4), watchdog_interval=0.02)
            await fm.set_desired({"badex": ["A/USDT", "B/USDT"], "goodex": ["A/USDT"]})
            await asyncio.sleep(0.25)
            h = fm.health()
            self.assertIn("CIRCUIT_OPEN", sink.status[("badex", "A/USDT")])
            self.assertIn(h["badex"]["state"], ("CIRCUIT_OPEN", "DISCONNECTED"))
            self.assertEqual(h["goodex"]["state"], "LIVE")
            self.assertGreater(sink.books[("goodex", "A/USDT")], 5)
            await asyncio.sleep(0.5)  # cooldown elapsed -> fresh client
            self.assertGreaterEqual(bad.clients, 2)
            await fm.stop()
        run(go())

    def test_watchdog_restarts_silent_connection(self):
        async def go():
            sink = RecSink()
            ad = FakeAdapter("fakex", SINGLE, {"A/USDT": "hang"})
            fm = FeedManager({"fakex": ad}, sink, fast_cfg(), watchdog_interval=0.02)
            await fm.set_desired({"fakex": ["A/USDT"]})
            await asyncio.sleep(0.8)
            p = fm.exchanges["fakex"].partitions[0]
            self.assertGreaterEqual(p.reconnects, 1)
            self.assertIn("watchdog", p.last_error)
            await fm.stop()
        run(go())


class SimIntegrationTest(unittest.TestCase):
    def test_sim_world_streams_through_manager(self):
        async def go():
            world = SimWorld(n_assets=4, venues_per_asset=3, scenarios=False)
            drv = SimDriver(world, step_s=0.02)
            dtask = asyncio.ensure_future(drv.run())
            adapters = {ex: SimAdapter(ex, drv) for ex in {k[0] for k in world.markets}}
            desired = {}
            for ex, sym in world.markets:
                desired.setdefault(ex, []).append(sym)
            sink = RecSink()
            fm = FeedManager(adapters, sink, fast_cfg(), watchdog_interval=0.05)
            await fm.set_desired(desired)
            await asyncio.sleep(1.0)
            for key in world.markets:
                self.assertGreater(sink.books.get(key, 0), 3, key)
            modes = {ex: rt.caps.book_mode for ex, rt in fm.exchanges.items()}
            self.assertEqual(modes.get("simex_a"), "multi")
            self.assertEqual(modes.get("simex_b"), "single")
            self.assertTrue(any(v > 0 for v in sink.trades.values()))
            cat = await adapters["simex_a"].load_catalog()
            self.assertTrue(cat.markets and cat.by_base)
            drv.fail["simex_b"] = 3
            await asyncio.sleep(0.5)
            b_keys = [k for k in world.markets if k[0] == "simex_b"]
            self.assertTrue(any("DISCONNECTED" in sink.status[k] for k in b_keys))
            self.assertTrue(all(sink.status[k][-1] == "STREAMING" for k in b_keys))
            await fm.stop()
            dtask.cancel()
        run(go())


class CcxtAdapterTest(unittest.TestCase):
    def test_adapter_against_fake_ccxt_module(self):
        class FakeEx:
            has = MULTI

            def __init__(self, opts):
                self.opts = opts
                self.markets_set = None

            async def load_markets(self):
                return {"QNT/USDT": {"spot": True, "type": "spot", "base": "QNT", "quote": "USDT", "active": True, "id": "QNTUSDT"},
                        "QNT/USDT:USDT": {"spot": False, "type": "swap", "base": "QNT", "quote": "USDT", "active": True}}

            async def fetch_tickers(self, symbols=None):
                return {"QNT/USDT": {"last": 100.0, "bid": 99.9, "ask": 100.1, "quoteVolume": 5e6, "timestamp": 1_700_000_000_000},
                        "QNT/USDT:USDT": {"last": 100.0, "quoteVolume": 9e9}}

            def set_markets(self, m, c=None):
                self.markets_set = m

            async def watch_order_book(self, symbol, limit=None):
                return {"symbol": symbol, "bids": [[99, 1]], "asks": [[101, 1]], "nonce": 1}

            async def close(self):
                pass

        class Mod:
            binance = FakeEx
        ad = CcxtAdapter("binance", module=Mod, pro_module=Mod)
        self.assertTrue(ad.has()["watchOrderBookForSymbols"])
        cat = asyncio.run(ad.load_catalog())
        self.assertEqual(list(cat.markets), ["QNT/USDT"])         # derivatives excluded
        self.assertEqual(cat.tickers["QNT/USDT"]["timestamp"], 1_700_000_000.0)
        cl = ad.new_stream_client()
        self.assertIsNotNone(cl.ex.markets_set)                   # markets shared, no reload
        ob = asyncio.run(cl.watch_book("QNT/USDT", None))
        self.assertEqual(ob["symbol"], "QNT/USDT")


if __name__ == "__main__":
    unittest.main()
