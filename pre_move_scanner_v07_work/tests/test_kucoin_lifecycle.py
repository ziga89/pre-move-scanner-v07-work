"""KuCoin stream-client lifecycle: regression + stress tests (no ccxt needed).

`FakeKucoinPro` models the parts of ccxt.pro's KuCoin that caused the live failure:
  * an instance is bound to an event loop only by open() (or the `asyncio_loop` option);
  * REST calls, client() and watch_multiple() call open() — but KuCoin's watchers first call
    negotiate() -> spawn() -> self.asyncio_loop.create_task(...);
  * markets shared via set_markets() mean no REST call happens first;
  * the negotiated websocket URL is cached per instance as a future (shared by all watchers);
  * close() marks the instance closed.
Unfixed, this reproduces: AttributeError: 'NoneType' object has no attribute 'create_task'.
The real ccxt is exercised in tests/test_kucoin_ccxt_local.py (skipped without ccxt).
"""
import asyncio
import random
import unittest

from server.feeds.base import classify_error
from server.feeds.ccxt_adapter import CcxtAdapter, CcxtStreamClient, StreamClientClosed, StreamClientLoopError
from server.feeds.manager import FeedManager
from server.feeds.sim_adapter import BadSymbol, NetworkError
from tests.test_feeds import RecSink, fast_cfg

SYMS = ["QNT/USDT", "XDC/USDT", "LINK/USDT"]
MULTI = {"watchOrderBookForSymbols": True, "watchOrderBook": True, "watchTradesForSymbols": True, "watchTrades": True}


class ExchangeClosedByUser(Exception):
    pass


class Model:
    """Shared 'exchange' state and fault injection for all FakeKucoinPro instances of a test."""

    def __init__(self, honour_loop_option=True):
        self.honour_loop_option = honour_loop_option
        self.instances = []
        self.calls_after_close = 0
        self.bullets = 0
        self.fail_bullets = 0
        self.client_error_instances = set()   # instance numbers whose watchers raise a client error
        self.always_client_error = False
        self.bad_network = set()              # symbols whose watchers always raise NetworkError
        self.drop_generation = 0              # bump to make every current watcher fail once
        self.rng = random.Random(3)


def fake_kucoin_class(model: Model):
    class FakeKucoinPro:
        id = "kucoin"
        has = MULTI

        def __init__(self, config=None):
            config = config or {}
            self.asyncio_loop = config.get("asyncio_loop") if model.honour_loop_option else None
            self.markets = None
            self.closed_by_user = False
            self.options = {"urls": {}}
            self.closes = 0
            model.instances.append(self)
            self.n = len(model.instances)
            self._seen_drop = model.drop_generation

        def set_markets(self, markets, currencies=None):
            self.markets = dict(markets)

        def open(self):
            self.closed_by_user = False
            if self.asyncio_loop is None:
                self.asyncio_loop = asyncio.get_running_loop()

        def spawn(self, method, *args):          # as ccxt: uses the bound loop
            fut = asyncio.get_running_loop().create_future()
            task = self.asyncio_loop.create_task(method(*args))

            def done(t):
                if fut.done():
                    return
                if t.cancelled():
                    fut.cancel()
                elif t.exception() is not None:
                    fut.set_exception(t.exception())
                else:
                    fut.set_result(t.result())
            task.add_done_callback(done)
            return fut

        async def negotiate(self):
            fut = self.options["urls"].get("public")
            if fut is None:
                fut = self.options["urls"]["public"] = self.spawn(self._negotiate_helper)
            return await fut

        async def _negotiate_helper(self):
            model.bullets += 1
            await asyncio.sleep(0.001)
            if model.fail_bullets > 0:
                model.fail_bullets -= 1
                del self.options["urls"]["public"]
                raise NetworkError("bullet-public 500 (injected)")
            return "wss://fake/endpoint?token=t"

        async def load_markets(self):            # REST -> fetch() -> open(lazy)
            self.open()
            self.markets = {s: {} for s in SYMS}
            return self.markets

        async def _watch(self, symbols):
            if self.closed_by_user:
                model.calls_after_close += 1
                raise ExchangeClosedByUser("kucoin instance was closed by the user")
            if self.markets is None:
                await self.load_markets()
            for s in symbols:
                if s not in self.markets:
                    raise BadSymbol(s)
            await self.negotiate()
            self.open()                          # client() / watch_multiple()
            if model.always_client_error or self.n in model.client_error_instances:
                raise AttributeError("'NoneType' object has no attribute 'send' (injected)")
            await asyncio.sleep(0.003)
            if self._seen_drop != model.drop_generation:
                self._seen_drop = model.drop_generation
                raise NetworkError("connection closed by server (injected)")
            bad = [s for s in symbols if s in model.bad_network]
            if bad:
                raise NetworkError(f"{bad[0]}: connection reset (injected)")
            return model.rng.choice(symbols)

        async def watch_order_book_for_symbols(self, symbols, limit=None):
            s = await self._watch(symbols)
            return {"symbol": s, "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]], "nonce": 1}

        async def watch_order_book(self, symbol, limit=None):
            return await self.watch_order_book_for_symbols([symbol], limit)

        async def watch_trades_for_symbols(self, symbols, since=None, limit=None):
            s = await self._watch(symbols)
            return [{"symbol": s, "timestamp": 1_700_000_000_000, "price": 100.0, "amount": 1.0, "side": "buy", "id": "1"}]

        async def watch_trades(self, symbol, since=None, limit=None):
            return await self.watch_trades_for_symbols([symbol])

        async def close(self):
            self.closes += 1
            self.closed_by_user = True

    return FakeKucoinPro


def adapter(model: Model) -> CcxtAdapter:
    cls = fake_kucoin_class(model)

    class Mod:
        kucoin = cls
    ad = CcxtAdapter("kucoin", module=Mod, pro_module=Mod)
    ad._markets = {s: {"symbol": s} for s in SYMS}   # shared catalog markets, as in production
    ad.has()                  # capability probe instance (never opened, needs no close)...
    model.instances.clear()   # ...so only stream-client instances are tracked below
    return ad


def kcfg(**kw):
    ov = {"kucoin": {"subscribe_pace_s": 0.001}}
    return fast_cfg(exchange_overrides=ov, **kw)


async def wait_streaming(fm: FeedManager, ex: str, symbols, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if all(fm.market_status.get((ex, s), {}).get("status") == "STREAMING" for s in symbols):
            return True
        await asyncio.sleep(0.01)
    return False


class ReproductionTest(unittest.TestCase):
    def test_model_reproduces_the_reported_error_without_open(self):
        async def go():
            m = Model(honour_loop_option=False)
            ex = fake_kucoin_class(m)()
            ex.set_markets({s: {} for s in SYMS})
            with self.assertRaises(AttributeError) as cm:
                await ex.watch_order_book_for_symbols(SYMS)
            self.assertIn("'NoneType' object has no attribute 'create_task'", str(cm.exception))
        asyncio.run(go())

    def test_stream_client_opens_before_first_watch(self):
        async def go():
            m = Model(honour_loop_option=False)       # only the explicit open() can save it
            cl = adapter(m).new_stream_client()
            ob = await cl.watch_books(SYMS, None)
            self.assertIn(ob["symbol"], SYMS)
            self.assertIs(m.instances[0].asyncio_loop, asyncio.get_running_loop())
            await cl.close()
            await cl.close()                          # idempotent
            self.assertEqual(m.instances[0].closes, 1)
            with self.assertRaises(StreamClientClosed):
                await cl.watch_books(SYMS, None)      # never reused after close
            self.assertEqual(m.calls_after_close, 0)
        asyncio.run(go())

    def test_loop_option_is_passed_when_created_inside_a_loop(self):
        async def go():
            m = Model(honour_loop_option=True)
            adapter(m).new_stream_client()
            self.assertIs(m.instances[0].asyncio_loop, asyncio.get_running_loop())
        asyncio.run(go())

    def test_client_bound_to_one_event_loop(self):
        m = Model()
        cl = adapter(m).new_stream_client()           # created outside any loop
        asyncio.run(cl.watch_books(SYMS, None))
        with self.assertRaises(StreamClientLoopError):
            asyncio.run(cl.watch_books(SYMS, None))   # a second, different loop

    def test_error_classes(self):
        self.assertEqual(classify_error(AttributeError("'NoneType' object has no attribute 'create_task'")), "client")
        self.assertEqual(classify_error(ExchangeClosedByUser("x")), "client")
        self.assertEqual(classify_error(StreamClientClosed("x")), "client")
        self.assertEqual(classify_error(RuntimeError("Event loop is closed")), "client")
        self.assertEqual(classify_error(BadSymbol("x")), "permanent")
        self.assertEqual(classify_error(NetworkError("x")), "transient")


class ManagerLifecycleTest(unittest.TestCase):
    def test_shared_markets_stream_all_kucoin_markets(self):
        async def go():
            m = Model(honour_loop_option=False)
            fm = FeedManager({"kucoin": adapter(m)}, RecSink(), kcfg(), watchdog_interval=0.02)
            await fm.set_desired({"kucoin": SYMS})
            self.assertTrue(await wait_streaming(fm, "kucoin", SYMS))
            h = fm.health()["kucoin"]
            self.assertEqual((h["state"], h["errors"]), ("LIVE", 0))
            await fm.stop()
            self.assertEqual([i.closes for i in m.instances], [1])
        asyncio.run(go())

    def test_client_error_rebuilds_once_and_recovers(self):
        async def go():
            m = Model()
            m.client_error_instances = {1}            # first instance broken, the rebuilt one works
            sink = RecSink()
            fm = FeedManager({"kucoin": adapter(m)}, sink, kcfg(), watchdog_interval=0.02)
            await fm.set_desired({"kucoin": SYMS})
            self.assertTrue(await wait_streaming(fm, "kucoin", SYMS))
            p = fm.exchanges["kucoin"].partitions[0]
            self.assertEqual(p.rebuilds, 1)           # book + trade loops failed together: one rebuild
            self.assertEqual(p.breaker_trips, 0)
            self.assertIn("AttributeError", p.last_error)   # reported, not hidden
            for s in SYMS:
                st = sink.status[("kucoin", s)]
                self.assertIn("RECONNECTING", st)
                self.assertNotIn("UNAVAILABLE", st)   # a client bug is not the market's fault
                self.assertEqual(st[-1], "STREAMING")
            self.assertEqual(m.instances[0].closes, 1)  # broken instance closed, never reused
            await fm.stop()
            self.assertEqual(m.calls_after_close, 0)
        asyncio.run(go())

    def test_persistent_client_error_backs_off_and_is_reported(self):
        async def go():
            m = Model()
            m.always_client_error = True
            sink = RecSink()
            fm = FeedManager({"kucoin": adapter(m)}, sink, kcfg(breaker_failures=4, breaker_cooldown_seconds=5),
                             watchdog_interval=0.02)
            await fm.set_desired({"kucoin": SYMS})
            await asyncio.sleep(0.6)
            p = fm.exchanges["kucoin"].partitions[0]
            self.assertLessEqual(len(m.instances), 8)   # backoff, not a hot rebuild loop
            self.assertEqual(p.breaker_trips, 1)        # partition fully dead -> breaker opens
            self.assertEqual(fm.health()["kucoin"]["state"], "CIRCUIT_OPEN")
            self.assertTrue(all("UNAVAILABLE" not in sink.status[("kucoin", s)] for s in SYMS))
            await fm.stop()
            self.assertTrue(all(i.closes == 1 for i in m.instances))
        asyncio.run(go())

    def test_one_failing_market_cannot_take_down_the_others(self):
        async def go():
            m = Model()
            m.bad_network = {"XDC/USDT"}
            sink = RecSink()
            cfg = kcfg(breaker_failures=3, breaker_cooldown_seconds=30)
            cfg["exchange_overrides"]["kucoin"].update(multi=False)   # per-symbol loops
            fm = FeedManager({"kucoin": adapter(m)}, sink, cfg, watchdog_interval=0.02)
            await fm.set_desired({"kucoin": SYMS})
            self.assertTrue(await wait_streaming(fm, "kucoin", ["QNT/USDT", "LINK/USDT"]))
            before = {s: sink.books.get(("kucoin", s), 0) for s in ("QNT/USDT", "LINK/USDT")}
            await asyncio.sleep(0.8)                   # XDC fails > breaker_failures times meanwhile
            p = fm.exchanges["kucoin"].partitions[0]
            self.assertGreaterEqual(p.errors, 3)
            self.assertEqual(p.breaker_trips, 0)       # healthy markets keep the partition open
            for s in ("QNT/USDT", "LINK/USDT"):
                self.assertGreater(sink.books[("kucoin", s)], before[s] + 20)
                self.assertEqual(sink.status[("kucoin", s)], ["SUBSCRIBING", "STREAMING"])
            self.assertEqual(sink.status[("kucoin", "XDC/USDT")][-1], "DISCONNECTED")
            await fm.stop()
        asyncio.run(go())


class StartStopStressTest(unittest.TestCase):
    """Repeatedly start / stop / change KuCoin subscriptions under injected faults."""

    CYCLES = 60

    def test_repeated_start_stop_with_faults(self):
        async def go():
            m = Model(honour_loop_option=False)
            ad = adapter(m)
            sink = RecSink()
            fm = FeedManager({"kucoin": ad}, sink, kcfg(breaker_failures=50), watchdog_interval=0.02)
            rng = random.Random(11)
            baseline_tasks = set(asyncio.all_tasks())
            ops = {"subset": 0, "stop_all": 0, "new_manager": 0, "drop": 0, "bullet_fail": 0, "client_error": 0}
            for cycle in range(self.CYCLES):
                op = rng.choice(list(ops))
                ops[op] += 1
                want = rng.sample(SYMS, rng.randint(1, 3))
                if op == "stop_all":
                    await fm.set_desired({})
                    self.assertEqual(fm.exchanges, {})
                elif op == "new_manager":
                    await fm.stop()
                    fm = FeedManager({"kucoin": ad}, sink, kcfg(breaker_failures=50), watchdog_interval=0.02)
                elif op == "drop":
                    m.drop_generation += 1
                elif op == "bullet_fail":
                    m.fail_bullets = 2
                    m.drop_generation += 1
                elif op == "client_error":
                    m.client_error_instances.add(len(m.instances) + 1)   # the next instance is broken
                    if fm.exchanges:
                        fm.exchanges["kucoin"].partitions[0]._restart.set()
                await fm.set_desired({"kucoin": want})
                ok = await wait_streaming(fm, "kucoin", want, timeout=5.0)
                self.assertTrue(ok, f"cycle {cycle} ({op}): {[fm.market_status.get(('kucoin', s)) for s in want]}")
                live = [i for i in m.instances if i.closes == 0]
                self.assertLessEqual(len(live), 1, f"cycle {cycle}: more than one open instance")
            await fm.stop()
            self.assertTrue(all(i.closes == 1 for i in m.instances), [i.closes for i in m.instances])
            self.assertEqual(m.calls_after_close, 0)       # no loop ever used a closed client
            await asyncio.sleep(0.05)
            leaked = [t for t in asyncio.all_tasks() - baseline_tasks if t is not asyncio.current_task() and not t.done()]
            self.assertEqual(leaked, [])
            self.assertGreaterEqual(min(ops.values()), 3, ops)
            self.assertGreater(len(m.instances), self.CYCLES // 3)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
