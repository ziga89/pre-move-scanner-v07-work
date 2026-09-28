import copy
import unittest

from server.engine.alerts import HighConvictionAlerts
from tests.helpers import cfg


def good(ts=1000.0, premove=94.0, intel=None):
    return {
        "asset": "QNT", "ts": ts, "premove": premove, "price": 100.0,
        "status": "STRONG PRE-MOVE", "confidence": 0.82,
        "confirmed": 4, "coverage": 4, "coverage_total": 4, "live_share": 1.0,
        "confirmed_venues": ["binance", "coinbase", "kraken", "kucoin"],
        "families": ["thinning", "no_replenish", "buy_flow", "volume", "cross_venue"],
        "subscores": {"orderbook": 82.0, "buy_pressure": 78.0, "cross_venue": 84.0, "liquidity": 60.0},
        "agg": {
            "family": {"thinning": 0.82, "no_replenish": 0.76, "bid_support": 0.50, "spread": 0.20},
            "family_venue_counts": {"thinning": 3, "no_replenish": 3, "buy_flow": 3, "volume": 3},
            "vol_ratio": 2.1, "top_trade_share": 0.12,
        },
        "late": {"state": "FLAT"},
        "intel": intel,
    }


class HighConvictionAlertTests(unittest.TestCase):
    def engine(self, **overrides):
        c = cfg(alerts=overrides if overrides else {})
        return HighConvictionAlerts(c["alerts"])

    def test_requires_sustained_confirmation_before_fire(self):
        e = self.engine(persistence_seconds=120)
        r = good()
        self.assertIsNone(e.update(r, 1000)[0])
        self.assertIsNone(e.update(r, 1119)[0])
        active, events, fired = e.update(r, 1120)
        self.assertTrue(fired)
        self.assertEqual(active["state"], "HIGH_CONVICTION")
        self.assertTrue(any(x["event_type"] == "high_conviction_buy_setup" for x in events))

    def test_single_venue_or_missing_feed_cannot_fire(self):
        e = self.engine(persistence_seconds=1)
        r = good()
        r.update(confirmed=1, coverage=4)
        self.assertFalse(e.assess(r)["strict"])
        r = good()
        r.update(coverage=3, coverage_total=4)
        self.assertFalse(e.assess(r)["strict"])

    def test_single_large_trade_cannot_fire(self):
        e = self.engine(persistence_seconds=1)
        r = good()
        r["agg"]["top_trade_share"] = 0.72
        self.assertFalse(e.assess(r)["strict"])

    def test_late_move_cannot_fire(self):
        e = self.engine(persistence_seconds=1)
        r = good()
        r["late"] = {"state": "IN_PROGRESS"}
        self.assertFalse(e.assess(r)["strict"])

    def test_wallet_support_can_lower_market_threshold_but_reshuffle_cannot(self):
        e = self.engine(persistence_seconds=1)
        supportive = {
            "cex_flow": 75.0, "cex_flow_direction": "OUTFLOW",
            "whale": 70.0, "whale_direction": "ACCUMULATION",
            "scarcity": 45.0, "scarcity_direction": "REAL_SUPPLY_DRAIN",
            "mm": 10.0, "mm_direction": "NEUTRAL",
        }
        r = good(premove=83.0, intel=supportive)
        self.assertTrue(e.assess(r)["strict"])

        reshuffle = {
            "cex_flow": 0.0, "cex_flow_direction": "NEUTRAL",
            "whale": 0.0, "whale_direction": "NEUTRAL",
            "scarcity": 0.0, "scarcity_direction": "RESHUFFLING",
            "mm": 0.0, "mm_direction": "ROUTING",
        }
        r2 = good(premove=83.0, intel=reshuffle)
        a = e.assess(r2)
        self.assertEqual(a["wallet"]["status"], "neutral")
        self.assertFalse(a["strict"])

    def test_hostile_wallet_flow_invalidates_immediately(self):
        e = self.engine(persistence_seconds=1, clear_after_seconds=60)
        r = good()
        e.update(r, 1000)
        active, _, fired = e.update(r, 1001)
        self.assertTrue(fired)
        self.assertIsNotNone(active)
        bad = copy.deepcopy(r)
        bad["intel"] = {
            "cex_flow": 80.0, "cex_flow_direction": "INFLOW",
            "whale": 65.0, "whale_direction": "DISTRIBUTION",
            "scarcity": 0.0, "scarcity_direction": "NEUTRAL",
            "mm": 0.0, "mm_direction": "NEUTRAL",
        }
        active, events, fired = e.update(bad, 1002)
        self.assertFalse(fired)
        self.assertIsNone(active)
        self.assertTrue(any(x["event_type"] == "high_conviction_cleared" for x in events))

    def test_hysteresis_survives_brief_score_dip(self):
        e = self.engine(persistence_seconds=1, clear_after_seconds=60)
        r = good()
        e.update(r, 1000)
        self.assertIsNotNone(e.update(r, 1001)[0])
        dip = good(premove=70.0)
        self.assertIsNotNone(e.update(dip, 1010)[0])
        self.assertIsNotNone(e.update(r, 1030)[0])


if __name__ == "__main__":
    unittest.main()
