import unittest

from tests.helpers import T0, cfg, fx_one
from server.engine.market_state import MarketState, cancel_proxy_confidence, proxy_label
from server.engine.rings import SecondRing
from server.sim import SimMarket


def make_state(**eng):
    c = cfg(engine=eng) if eng else cfg()
    return MarketState("QNT", "x", "QNT/USDT", "USDT", fx_one, c["engine"], c["feeds"], now=T0)


def book(mid=100.0, ask_q=1.0, bid_q=1.0, n=10, step=0.1):
    bids = [[mid - step * (i + 1), bid_q] for i in range(n)]
    asks = [[mid + step * (i + 1), ask_q] for i in range(n)]
    return bids, asks


class CancelProxyTest(unittest.TestCase):
    def test_confidence_never_reaches_one(self):
        c = cancel_proxy_confidence(50.0, True, False, 1e6, 0.0, 2.0, 2.0, False)
        self.assertLessEqual(c, 0.8)
        self.assertEqual(proxy_label(c), "moderate")

    def test_no_tape_means_very_low(self):
        c = cancel_proxy_confidence(10.0, False, False, 1e5, 0.0, 2.0, 2.0, False)
        self.assertLessEqual(c, 0.1)
        self.assertEqual(proxy_label(c), "very low")

    def test_resync_zero(self):
        self.assertEqual(cancel_proxy_confidence(10.0, True, True, 1e5, 0.0, 2.0, 2.0, False), 0.0)

    def test_fills_explaining_removal_lowers_confidence(self):
        hi = cancel_proxy_confidence(4.0, True, False, 1e5, 0.0, 2.0, 2.0, False)
        lo = cancel_proxy_confidence(4.0, True, False, 1e5, 0.95e5, 2.0, 2.0, False)
        self.assertLess(lo, hi)

    def test_feature_is_removed_minus_fills(self):
        st = make_state()
        b, a = book()
        st.on_book(b, a, T0)
        st.on_trades([(T0 + 1, 100.1, 0.5, "buy", "t1")], T0 + 1)
        b2, a2 = book(ask_q=0.2)
        st.resync_until = 0  # skip grace for the unit test
        st.on_book(b2, a2, T0 + 1.5)
        f = st.tick(T0 + 2)
        removed = f["ask_removed_60"]
        self.assertGreater(removed, 0)
        self.assertAlmostEqual(f["ask_cancel_proxy_60"], max(0.0, removed - f["buy_usd_60"]))
        self.assertIn(f["cancel_proxy_label"], ("moderate", "low", "very low"))


class TradeIngestTest(unittest.TestCase):
    def test_duplicates_dropped(self):
        st = make_state()
        st.on_book(*book(), T0)
        t = (T0, 100.0, 1.0, "buy", "id1")
        self.assertEqual(st.on_trades([t, t], T0), 1)
        self.assertEqual(st.on_trades([t], T0 + 1), 0)
        self.assertEqual(st.dropped_duplicate, 2)

    def test_historical_replay_dropped(self):
        st = make_state()
        st.on_book(*book(), T0)
        fresh = (T0 + 100 - 0.2, 100.0, 1.0, "buy", "a")
        old = (T0 + 100 - 45, 100.0, 1.0, "buy", "b")
        self.assertEqual(st.on_trades([fresh, old], T0 + 100), 1)
        self.assertEqual(st.dropped_historical, 1)

    def test_clock_skew_tolerated(self):
        st = make_state()
        st.on_book(*book(), T0)
        # Exchange clock 40 s ahead of the local clock: trades must still count.
        skewed = [(T0 + 140 + i * 0.1, 100.0, 1.0, "sell", f"s{i}") for i in range(3)]
        self.assertEqual(st.on_trades(skewed, T0 + 100), 3)

    def test_windows_use_receipt_time(self):
        st = make_state()
        st.on_book(*book(), T0)
        st.on_trades([(T0 - 500, 100.0, 2.0, "buy", "z")], T0 + 10)  # first batch seeds skew
        f = st.tick(T0 + 11)
        self.assertAlmostEqual(f["buy_usd_60"], 200.0)


class HealthTest(unittest.TestCase):
    def test_stale_after_threshold(self):
        st = make_state()
        st.on_book(*book(), T0)
        st.on_book(*book(), T0 + 1)
        self.assertNotEqual(st.state(T0 + 5)[0], "STALE")
        self.assertEqual(st.state(T0 + 60)[0], "STALE")

    def test_disconnect_resets_book_and_next_book_is_snapshot(self):
        st = make_state()
        st.on_book(*book(), T0)
        st.set_feed_status("DISCONNECTED", "socket closed")
        self.assertEqual(st.state(T0 + 1)[0], "DISCONNECTED")
        self.assertFalse(st.book.ready)
        st.set_feed_status("STREAMING")
        st.on_book(*book(ask_q=5.0), T0 + 2)  # snapshot: no phantom adds
        self.assertEqual(st.flow.sum("ask_add", int(T0 + 2), 60), 0.0)
        self.assertEqual(st.state(T0 + 3)[0], "RESYNCING")

    def test_resync_grace_blocks_flows(self):
        st = make_state()
        st.on_book(*book(), T0)
        st.on_book(*book(ask_q=0.1), T0 + 1)  # within the initial grace period
        self.assertEqual(st.flow.sum("ask_rem", int(T0 + 1), 60), 0.0)

    def test_memory_bounded(self):
        st = make_state()
        st.on_book(*book(), T0)
        for i in range(5000):
            st.on_trades([(T0 + i, 100.0, 1.0, "buy", f"k{i}")], T0 + i)
        self.assertEqual(len(st.flow.data["buy"]), 900)
        self.assertLessEqual(len(st._seen_ids), 4000)


class WarmupTest(unittest.TestCase):
    def _run(self, st, m, start, seconds):
        f = None
        for i in range(seconds * 2):
            ts = start + i * 0.5
            b, a, tr = m.step(ts, 0.5)
            if b:
                st.on_book(b, a, ts)
            st.on_trades(tr, ts)
            if i % 2 == 1:
                f = st.tick(ts)
        return f

    def test_warming_then_live(self):
        st = make_state(min_baseline_minutes=5, baseline_lag_minutes=2)
        m = SimMarket("x", "QNT/USDT", 100.0, seed=5)
        f = self._run(st, m, T0, 120)
        self.assertEqual(f["state"], "WARMING")
        f = self._run(st, m, T0 + 120, 9 * 60)
        self.assertEqual(f["state"], "LIVE")
        self.assertTrue(f["warmed"])
        self.assertIsNotNone(f["ask1_ratio"])
        minutes = st.drain_minutes()
        self.assertGreaterEqual(len(minutes), 9)
        self.assertIn("ask_net_pct", minutes[-1])

    def test_rehydrate_skips_warmup(self):
        src = make_state(min_baseline_minutes=5, baseline_lag_minutes=2)
        m = SimMarket("x", "QNT/USDT", 100.0, seed=6)
        self._run(src, m, T0, 12 * 60)
        recs = src.drain_minutes()
        fresh = make_state(min_baseline_minutes=5, baseline_lag_minutes=2)
        n = fresh.rehydrate(recs, T0 + 12 * 60)
        self.assertEqual(n, len(recs))
        self.assertTrue(fresh.base.warm)
        self.assertIsNotNone(fresh.slip_q_usd)


class RingTest(unittest.TestCase):
    def test_second_ring_window_and_zeroing(self):
        r = SecondRing(("v",), 10)
        r.add(100, "v", 1.0)
        r.add(105, "v", 2.0)
        r.advance(108)
        self.assertEqual(r.sum("v", 108, 10), 3.0)
        r.advance(112)
        self.assertEqual(r.sum("v", 112, 10), 2.0)   # second 100 fell out
        r.add(200, "v", 5.0)
        r.advance(200)
        self.assertEqual(r.sum("v", 200, 10), 5.0)   # stale slots zeroed
        r.add(150, "v", 9.0)                          # too old: ignored
        self.assertEqual(r.sum("v", 200, 10), 5.0)


if __name__ == "__main__":
    unittest.main()
