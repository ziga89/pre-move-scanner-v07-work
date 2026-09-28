"""The REAL ccxt.pro KuCoin code against a local fake KuCoin server (no internet needed).

Covers the production path exactly: catalog markets shared into each stream client
(set_markets), negotiate() via bullet-public, websocket welcome/ack/ping, level2 deltas +
REST snapshot, match trades — driven through CcxtAdapter + FeedManager, repeatedly started,
stopped and disrupted. Skipped when ccxt / aiohttp are not installed (they are in CI).
"""
import asyncio
import random
import unittest

try:
    import aiohttp  # noqa: F401
    import ccxt.pro  # noqa: F401
    HAVE = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE = False

from server.feeds.ccxt_adapter import CcxtAdapter
from server.feeds.manager import FeedManager
from tests.test_feeds import RecSink, fast_cfg

SYMS = ["QNT/USDT", "XDC/USDT", "LINK/USDT"]


def kcfg(**kw):
    return fast_cfg(exchange_overrides={"kucoin": {"subscribe_pace_s": 0.001}}, **kw)


async def wait_streaming(fm, symbols, timeout=15.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if all(fm.market_status.get(("kucoin", s), {}).get("status") == "STREAMING" for s in symbols):
            return True
        await asyncio.sleep(0.02)
    return False


@unittest.skipUnless(HAVE, "ccxt / aiohttp not installed")
class RealCcxtKucoinTest(unittest.TestCase):
    def setUp(self):
        from tests.kucoin_fake import FakeKucoin, kucoin_markets, local_kucoin_class
        self.FakeKucoin, self.kucoin_markets, self.local_kucoin_class = FakeKucoin, kucoin_markets, local_kucoin_class

    def _adapter(self, srv, counters):
        base = self.local_kucoin_class(srv.base_url)

        class Counted(base):
            def __init__(self, config=None):
                super().__init__(config)
                counters["created"] += 1
                counters["instances"].append(self)

            async def close(self, *a, **kw):
                counters["closed"] += 1
                return await super().close(*a, **kw)

        class Mod:
            kucoin = Counted
        ad = CcxtAdapter("kucoin", module=Mod, pro_module=Mod)
        ad._markets = self.kucoin_markets()     # what load_catalog() shares after load_markets()
        counters.update(created=0, closed=0, instances=[])
        ad.has()                                # capability probe (never opened) is not counted
        counters.update(created=0, closed=0, instances=[])
        return ad

    def test_unfixed_path_reproduces_the_reported_error(self):
        """Documents the root cause with the real library: shared markets + no open()."""
        async def go():
            srv = await self.FakeKucoin().start()
            try:
                ex = self.local_kucoin_class(srv.base_url)({"enableRateLimit": True})
                ex.set_markets(self.kucoin_markets())
                if ex.asyncio_loop is not None:
                    self.skipTest("this ccxt version binds the loop at construction")
                with self.assertRaises(AttributeError) as cm:
                    await asyncio.wait_for(ex.watch_order_book_for_symbols(SYMS), 10)
                self.assertIn("create_task", str(cm.exception))
                await ex.close()
            finally:
                await srv.stop()
        asyncio.run(go())

    def test_feed_manager_streams_kucoin_books_and_trades(self):
        async def go():
            srv = await self.FakeKucoin().start()
            counters = {}
            try:
                ad = self._adapter(srv, counters)
                sink = RecSink()
                fm = FeedManager({"kucoin": ad}, sink, kcfg(), watchdog_interval=0.05)
                await fm.set_desired({"kucoin": SYMS})
                self.assertTrue(await wait_streaming(fm, SYMS), fm.health())
                await asyncio.sleep(1.0)
                for s in SYMS:
                    self.assertGreater(sink.books.get(("kucoin", s), 0), 5, s)
                    self.assertGreater(sink.trades.get(("kucoin", s), 0), 0, s)
                h = fm.health()["kucoin"]
                self.assertEqual((h["state"], h["errors"], h["partitions"][0]["rebuilds"]), ("LIVE", 0, 0), h)
                self.assertEqual(srv.bullet_calls, 1)          # one negotiate shared by book + trade watchers
                await fm.stop()
                self.assertEqual(counters["created"], counters["closed"])
            finally:
                await srv.stop()
        asyncio.run(go())

    def test_bad_symbol_does_not_stop_the_other_kucoin_markets(self):
        async def go():
            srv = await self.FakeKucoin().start()
            counters = {}
            try:
                ad = self._adapter(srv, counters)
                sink = RecSink()
                fm = FeedManager({"kucoin": ad}, sink, kcfg(), watchdog_interval=0.05)
                await fm.set_desired({"kucoin": SYMS + ["NOPE/USDT"]})
                self.assertTrue(await wait_streaming(fm, SYMS), fm.health())
                self.assertEqual(fm.market_status[("kucoin", "NOPE/USDT")]["status"], "UNAVAILABLE")
                await fm.stop()
            finally:
                await srv.stop()
        asyncio.run(go())

    def test_repeated_start_stop_and_server_faults(self):
        """Stress: start / stop / resubscribe / disconnect / token failures, many cycles."""
        async def go():
            srv = await self.FakeKucoin().start()
            counters = {}
            rng = random.Random(5)
            try:
                ad = self._adapter(srv, counters)
                sink = RecSink()
                fm = FeedManager({"kucoin": ad}, sink, kcfg(breaker_failures=50), watchdog_interval=0.05)
                ops = ["subset", "stop_all", "new_manager", "drop", "bullet_fail_drop", "expire"]
                done = {o: 0 for o in ops}
                for cycle in range(18):
                    op = ops[cycle % len(ops)]
                    done[op] += 1
                    want = rng.sample(SYMS, rng.randint(1, 3))
                    if op == "stop_all":
                        await fm.set_desired({})
                    elif op == "new_manager":
                        await fm.stop()
                        fm = FeedManager({"kucoin": ad}, sink, kcfg(breaker_failures=50), watchdog_interval=0.05)
                    elif op == "drop":
                        await srv.drop_all()
                    elif op == "bullet_fail_drop":
                        srv.fail_bullets = 2
                        await srv.expire_tokens()             # forces a renegotiation that fails twice
                    elif op == "expire":
                        await srv.expire_tokens()
                    await fm.set_desired({"kucoin": want})
                    self.assertTrue(await wait_streaming(fm, want), f"cycle {cycle} {op}: {fm.health()}")
                    errs = [p["last_error"] for p in fm.health()["kucoin"]["partitions"]]
                    self.assertFalse(any("create_task" in e for e in errs), errs)
                    self.assertLessEqual(counters["created"] - counters["closed"], 1, f"cycle {cycle}: open instances")
                await fm.stop()
                self.assertEqual(counters["created"], counters["closed"])
                self.assertGreaterEqual(srv.connections_total, 10)
                self.assertGreaterEqual(srv.bullet_calls, 6)
            finally:
                await srv.stop()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
