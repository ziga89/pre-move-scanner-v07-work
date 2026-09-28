"""Checks against the REAL installed ccxt (skipped when ccxt is absent; runs in CI).
No network calls: only class instantiation and capability flags."""
import unittest

from tests.helpers import cfg

try:
    import ccxt  # noqa: F401
    import ccxt.pro  # noqa: F401
    HAVE = True
except Exception:  # pragma: no cover
    HAVE = False


@unittest.skipUnless(HAVE, "ccxt not installed")
class RealCcxtTest(unittest.TestCase):
    def test_configured_exchange_ids_exist_and_resolve(self):
        from server.feeds.capabilities import resolve_caps
        from server.feeds.ccxt_adapter import CcxtAdapter
        modes = {}
        for ex in cfg()["discovery"]["exchanges"]:
            has = CcxtAdapter(ex).has()
            self.assertIn("watchOrderBook", has, ex)
            caps = resolve_caps(ex, has)
            modes[ex] = caps.book_mode
        self.assertEqual(modes["binance"], "multi")
        self.assertTrue(sum(1 for m in modes.values() if m != "none") >= len(modes) - 2, modes)

    def test_stream_client_construction_without_network(self):
        from server.feeds.ccxt_adapter import CcxtAdapter
        cl = CcxtAdapter("kraken").new_stream_client()
        self.assertTrue(hasattr(cl.ex, "watch_order_book"))


if __name__ == "__main__":
    unittest.main()
