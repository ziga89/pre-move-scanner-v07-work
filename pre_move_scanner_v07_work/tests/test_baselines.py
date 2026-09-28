import unittest

from tests.helpers import cfg
from server.engine.baselines import MINUTE_FIELDS, Baselines, derive_minute
from server.engine.rings import MinuteRing
from server.util import robust_stats


def rec(ask=100000.0, valid=1.0, **kw):
    r = {"mid_close": 10.0, "ask_depth_1": ask, "bid_depth_1": 100000.0, "ask_depth_05": ask / 2,
         "spread_bps": 5.0, "buy_usd": 5000.0, "sell_usd": 5000.0, "trades": 20.0, "imbalance_1": 0.0,
         "slippage_bps": 8.0, "ask_added": 50000.0, "ask_removed": 50000.0, "ask_cancel_proxy": 45000.0,
         "valid_frac": valid}
    r.update(kw)
    return derive_minute(r)


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.e = cfg()["engine"]
        self.ring = MinuteRing(MINUTE_FIELDS, 1500)

    def test_lag_excludes_unfolding_anomaly(self):
        t = 0
        for i in range(120):
            self.ring.append(t, rec())
            t += 60
        for i in range(10):  # last 10 minutes: asks collapse
            self.ring.append(t, rec(ask=30000.0))
            t += 60
        b = Baselines(self.e)
        b.recompute(self.ring, t)
        self.assertAlmostEqual(b.med("ask_depth_1"), 100000.0)
        self.assertAlmostEqual(b.ratio("ask_depth_1", 30000.0), 0.3)

    def test_invalid_minutes_excluded(self):
        t = 0
        for i in range(60):
            self.ring.append(t, rec(ask=100000.0 if i % 2 else 1.0, valid=1.0 if i % 2 else 0.2))
            t += 60
        b = Baselines(self.e)
        b.recompute(self.ring, t + 600)
        self.assertAlmostEqual(b.med("ask_depth_1"), 100000.0)
        self.assertEqual(b.valid_minutes, 30)

    def test_warm_threshold(self):
        t = 0
        for i in range(25):
            self.ring.append(t, rec())
            t += 60
        b = Baselines(self.e)
        b.recompute(self.ring, t)
        self.assertEqual(b.valid_minutes, 15)  # 25 minus the 10 lagged minutes
        self.assertFalse(b.warm)
        for i in range(5):
            self.ring.append(t, rec())
            t += 60
        b.recompute(self.ring, t)
        self.assertTrue(b.warm)

    def test_derived_fields(self):
        r = rec(buy_usd=8000.0, sell_usd=2000.0, ask_added=30000.0, ask_removed=50000.0)
        self.assertAlmostEqual(r["buy_share"], 0.8)
        self.assertAlmostEqual(r["ask_repl"], 0.6)
        self.assertAlmostEqual(r["ask_net_pct"], -0.2)
        self.assertAlmostEqual(r["ask_refill"], 1 - 20000.0 / 8000.0)
        tiny = rec(buy_usd=50.0, sell_usd=20.0, trades=2.0)
        self.assertIsNone(tiny["buy_share"])  # $70 / 2 trades is not a buy-share observation

    def test_robust_floor(self):
        med, scale = robust_stats([5.0] * 50, 0.05, 0.0)
        self.assertAlmostEqual(med, 5.0)
        self.assertAlmostEqual(scale, 0.25)


if __name__ == "__main__":
    unittest.main()
