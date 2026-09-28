"""Exchange quirks found by the repeated live self-tests (no network needed).

* Coinbase serves BASE/USDC subscriptions under BASE-USD; ccxt resolves the USDC request with a
  book / trades labelled BASE/USD. Those updates must reach the USDC market, not be dropped.
* ccxt decodes MEXC's spot stream as protobuf but does not install it. Without the package the
  stream only dies as a keepalive timeout; the feed manager must say what is missing instead.
"""
import asyncio
import unittest
from unittest import mock

from server.feeds import capabilities
from server.feeds.capabilities import resolve_caps
from server.feeds.ccxt_adapter import CcxtAdapter
from server.feeds.manager import FeedManager
from tests.test_feeds import SINGLE, FakeAdapter, RecSink, fast_cfg

MULTI = {"watchOrderBookForSymbols": True, "watchOrderBook": True, "watchTradesForSymbols": True, "watchTrades": True}


class FakeCoinbase:
    """ccxt-like: a BASE/USDC request is answered with the BASE/USD-labelled book (try_resolve_usdc)."""
    id = "coinbase"
    has = MULTI
    served = 0

    def __init__(self, config=None):
        self.asyncio_loop = None

    def open(self):
        self.asyncio_loop = asyncio.get_running_loop()

    def set_markets(self, markets, currencies=None):
        pass

    @staticmethod
    def _label(sym):
        return sym[:-1] if sym.endswith("/USDC") else sym

    async def watch_order_book_for_symbols(self, symbols, limit=None):
        await asyncio.sleep(0.003)
        FakeCoinbase.served += 1
        sym = symbols[FakeCoinbase.served % len(symbols)]
        return {"symbol": self._label(sym), "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}

    async def watch_order_book(self, symbol, limit=None):
        return await self.watch_order_book_for_symbols([symbol], limit)

    async def watch_trades_for_symbols(self, symbols, since=None, limit=None):
        await asyncio.sleep(0.01)
        return [{"symbol": self._label(s), "timestamp": 1_700_000_000_000, "price": 100.0, "amount": 1.0,
                 "side": "buy", "id": s} for s in symbols]

    async def watch_trades(self, symbol, since=None, limit=None):
        return await self.watch_trades_for_symbols([symbol])

    async def close(self):
        pass


class CoinbaseAliasTest(unittest.TestCase):
    def test_usdc_market_receives_usd_labelled_updates(self):
        async def go():
            class Mod:
                coinbase = FakeCoinbase
            ad = CcxtAdapter("coinbase", module=Mod, pro_module=Mod)
            sink = RecSink()
            fm = FeedManager({"coinbase": ad}, sink, fast_cfg(exchange_overrides={"coinbase": {"subscribe_pace_s": 0.001}}),
                             watchdog_interval=0.02)
            want = ["LINK/USDC", "SOL/USDC", "ETH/USD"]
            await fm.set_desired({"coinbase": want})
            await asyncio.sleep(0.5)
            for s in want:
                self.assertEqual(fm.market_status[("coinbase", s)]["status"], "STREAMING", s)
                self.assertGreater(sink.books.get(("coinbase", s), 0), 5, s)
                self.assertGreater(sink.trades.get(("coinbase", s), 0), 0, s)
            self.assertNotIn(("coinbase", "LINK/USD"), sink.books)          # attributed to the subscribed market
            p = fm.exchanges["coinbase"].partitions[0]
            self.assertEqual(p.unmatched_msgs, 0)
            await fm.stop()
        asyncio.run(go())

    def test_foreign_symbols_are_counted_not_silently_dropped(self):
        async def go():
            class Stray(FakeCoinbase):
                async def watch_order_book_for_symbols(self, symbols, limit=None):
                    await asyncio.sleep(0.003)
                    return {"symbol": "FOO/EUR", "bids": [[1.0, 1.0]], "asks": [[2.0, 1.0]]}

            class Mod:
                coinbase = Stray
            ad = CcxtAdapter("coinbase", module=Mod, pro_module=Mod)
            fm = FeedManager({"coinbase": ad}, RecSink(), fast_cfg(exchange_overrides={"coinbase": {"subscribe_pace_s": 0.001}}),
                             watchdog_interval=0.02)
            await fm.set_desired({"coinbase": ["LINK/USD"]})
            await asyncio.sleep(0.2)
            part = fm.health()["coinbase"]["partitions"][0]
            self.assertGreater(part["unmatched_msgs"], 5)
            self.assertEqual(part["last_unmatched_symbol"], "FOO/EUR")
            await fm.stop()
        asyncio.run(go())


class MissingRequirementTest(unittest.TestCase):
    def test_caps_report_missing_protobuf_for_mexc(self):
        with mock.patch.object(capabilities, "module_available", return_value=False):
            self.assertEqual(resolve_caps("mexc", SINGLE).missing_requirement, "google.protobuf")
            self.assertEqual(resolve_caps("gate", SINGLE).missing_requirement, "")
        with mock.patch.object(capabilities, "module_available", return_value=True):
            self.assertEqual(resolve_caps("mexc", SINGLE).missing_requirement, "")

    def test_manager_says_what_is_missing_instead_of_connecting(self):
        async def go():
            ad = FakeAdapter("mexc", SINGLE)
            sink = RecSink()
            with mock.patch.object(capabilities, "module_available", return_value=False):
                fm = FeedManager({"mexc": ad}, sink, fast_cfg(), watchdog_interval=0.02)
                await fm.set_desired({"mexc": ["XDC/USDT", "BTC/USDT"]})
            await asyncio.sleep(0.2)
            for s in ("XDC/USDT", "BTC/USDT"):
                st = fm.market_status[("mexc", s)]
                self.assertEqual(st["status"], "UNAVAILABLE")
                self.assertIn("protobuf", st["reason"])
            self.assertEqual(ad.clients, 0)          # no pointless connection attempts
            await fm.stop()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
