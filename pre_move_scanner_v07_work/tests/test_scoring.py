import math
import random
import unittest

from tests.helpers import cfg
from server.engine.scoring import (FAMILIES, context_points, late_assessment, run_pipeline,
                                   sigma_estimates, subscores, venue_flags)

SC = cfg()["scoring"]
LC = SC["late"]


def agg(**kw):
    a = {
        "family": {k: 0.0 for k in FAMILIES + ("depth_both", "spread", "slip")},
        "n_live": 4, "n_warmed": 4, "n_confirmed": 4, "confirm_share": 1.0, "live_share": 1.0,
        "activity_conf": 1.0, "book_conf": 1.0, "confidence": 1.0, "propagation": 0.0,
        "buy_share": 0.7, "buy_share_base": 0.5, "refill": 0.5,
    }
    fam = kw.pop("family", {})
    a.update(kw)
    a["family"].update(fam)
    return a


FLAT = {"L": 0.0, "L_hard": 0.0, "L_vol": 0.0, "multiplier": 1.0, "cap": None, "state": "FLAT"}


def pipe(a, late=FLAT, comp=None, intel=None):
    return run_pipeline(a, subscores(a, late["state"] == "FLAT"), late, comp, intel, SC)


class PipelineOrderTest(unittest.TestCase):
    def test_bonus_and_context_cannot_bypass_family_cap(self):
        # Two strong families only -> independent-signal cap 55 applies AFTER bonus/context.
        a = agg(family={"thinning": 1.0, "no_replenish": 1.0}, n_confirmed=1, confirm_share=0.9)
        base = pipe(a)
        boosted = pipe(a, comp=0.2, intel={"whale": 100.0, "whale_direction": "ACCUMULATION",
                                           "cex_flow": 100.0, "cex_flow_direction": "OUTFLOW"})
        self.assertGreater(boosted["after_context"], base["after_context"])
        self.assertLessEqual(boosted["instant"], 55.0)
        self.assertLessEqual(base["instant"], 55.0)

    def test_confidence_before_caps_and_low_activity_cap(self):
        a = agg(family={k: 1.0 for k in FAMILIES}, activity_conf=0.1, confidence=0.3)
        p = pipe(a)
        self.assertLessEqual(p["instant"], 32.0)
        self.assertLess(p["after_confidence"], p["after_context"])

    def test_late_cap_applied_last(self):
        a = agg(family={k: 1.0 for k in FAMILIES})
        late = {"L": 1.2, "L_hard": 1.2, "L_vol": 0.0, "multiplier": 0.3, "cap": 25.0, "state": "LATE"}
        p = pipe(a, late=late, comp=0.2)
        self.assertLessEqual(p["instant"], 25.0)
        self.assertGreater(p["after_caps"], 25.0)  # everything before the late step was high
        self.assertEqual(p["caps"][-1]["name"], "late move")

    def test_venue_and_share_caps(self):
        a = agg(family={k: 1.0 for k in FAMILIES}, n_confirmed=1, confirm_share=0.2)
        p = pipe(a)
        self.assertLessEqual(p["instant"], 59.0)
        a = agg(family={k: 1.0 for k in FAMILIES}, n_live=1, n_warmed=1, n_confirmed=1)
        self.assertLessEqual(pipe(a)["instant"], 42.0)

    def test_warmup_and_partial_caps(self):
        a = agg(family={k: 1.0 for k in FAMILIES}, n_warmed=1)
        self.assertLessEqual(pipe(a)["instant"], 44.0)
        a = agg(family={k: 1.0 for k in FAMILIES}, live_share=0.4)
        self.assertLessEqual(pipe(a)["instant"], 60.0)

    def test_full_structure_scores_high(self):
        a = agg(family={"thinning": 0.8, "no_replenish": 0.7, "buy_flow": 0.8, "volume": 0.7,
                        "bid_support": 0.6, "slip": 0.3})
        p = pipe(a, comp=0.3)
        self.assertGreaterEqual(p["instant"], 70.0)
        self.assertIn("cross_venue", p["families"])


class NullSemanticsTest(unittest.TestCase):
    def test_missing_intel_is_zero_context_not_negative(self):
        self.assertEqual(context_points(None, 8.0)[0], 0.0)
        self.assertEqual(context_points({"cex_flow": None, "whale": None, "mm": None, "scarcity": None}, 8.0)[0], 0.0)

    def test_missing_intel_never_counts_as_family(self):
        a = agg(family={"thinning": 1.0})
        p = pipe(a, intel={"whale": None, "cex_flow": None})
        self.assertNotIn("onchain", p["families"])

    def test_inflow_is_negative_only_when_present(self):
        pts, _ = context_points({"cex_flow": 50.0, "cex_flow_direction": "INFLOW"}, 8.0)
        self.assertLess(pts, 0)

    def test_context_bounded(self):
        pts, _ = context_points({"cex_flow": 100.0, "cex_flow_direction": "OUTFLOW", "whale": 100.0,
                                 "whale_direction": "ACCUMULATION", "scarcity": 100.0,
                                 "scarcity_direction": "REAL_SUPPLY_DRAIN"}, 8.0)
        self.assertEqual(pts, 8.0)


class LateMoveTest(unittest.TestCase):
    SIG = {15: 0.4, 30: 0.6, 60: 0.9}

    def test_hard_thresholds(self):
        for h, r in ((15, 5.0), (30, 8.0), (60, 12.0)):
            la = late_assessment({h: r}, {}, False, LC)
            self.assertGreaterEqual(la["L"], 1.0, h)
            self.assertEqual(la["state"], "LATE")
            self.assertEqual(la["cap"], 25.0)
            self.assertEqual(la["trigger"]["kind"], "hard")

    def test_flat_no_penalty(self):
        la = late_assessment({15: 0.4, 30: 0.3, 60: 0.8}, {}, False, LC)
        self.assertEqual(la["multiplier"], 1.0)
        self.assertEqual(la["state"], "FLAT")

    def test_vol_normalised_detects_large_move_for_calm_asset(self):
        # +1.4 % in 15 m is below the 5 % hard threshold but 3.5σ for this asset.
        la = late_assessment({15: 1.4, 30: 1.5, 60: 1.6}, self.SIG, True, LC)
        self.assertEqual(la["state"], "LATE")
        self.assertEqual(la["trigger"]["kind"], "vol")
        hard_only = late_assessment({15: 1.4, 30: 1.5, 60: 1.6}, self.SIG, False, LC)
        self.assertEqual(hard_only["state"], "FLAT")

    def test_vol_requires_min_absolute_move(self):
        la = late_assessment({15: 0.9}, {15: 0.1, 30: 0.1, 60: 0.1}, True, LC)
        self.assertLess(la["L_vol"], 0.01)

    def test_down_moves_weighted(self):
        la = late_assessment({15: -5.0}, {}, False, LC)
        self.assertAlmostEqual(la["L"], 0.7)
        self.assertEqual(la["state"], "IN_PROGRESS")

    def test_sigma_estimates(self):
        rng = random.Random(1)
        px, closes = 100.0, []
        for _ in range(1000):
            px *= math.exp(rng.gauss(0, 0.001))  # 0.1 % per minute
            closes.append(px)
        s = sigma_estimates(closes)
        self.assertAlmostEqual(s[15], 0.1 * math.sqrt(15), delta=0.15)
        self.assertAlmostEqual(s[60], 0.1 * math.sqrt(60), delta=0.35)
        self.assertIsNone(sigma_estimates(closes[:50])[60])


class VenueFlagTest(unittest.TestCase):
    def base_f(self, **kw):
        f = {"state": "LIVE", "warmed": True, "activity_conf": 1.0, "confidence": 1.0,
             "ask1_ratio": 1.0, "ask1_z": 0.0, "net_flow_60": 1000.0}
        f.update(kw)
        return f

    def test_buy_share_relative_to_own_normal(self):
        biased = venue_flags(self.base_f(buy_share_excess=0.05), SC)   # venue normally 65 % buys
        real = venue_flags(self.base_f(buy_share_excess=0.2), SC)
        self.assertEqual(biased["buy_flow"], 0.0)
        self.assertGreater(real["buy_flow"], 0.7)

    def test_thinning_needs_ratio_and_z(self):
        noisy = venue_flags(self.base_f(ask1_ratio=0.55, ask1_z=-0.8), SC)  # normal for a jumpy book
        self.assertEqual(noisy["thinning"], 0.0)
        real = venue_flags(self.base_f(ask1_ratio=0.55, ask1_z=-4.0), SC)
        self.assertGreater(real["thinning"], 0.6)

    def test_warming_venue_contributes_nothing(self):
        fl = venue_flags(self.base_f(state="WARMING", ask1_ratio=0.3, ask1_z=-9), SC)
        self.assertEqual(fl["thinning"], 0.0)
        self.assertFalse(fl["confirmed"])

    def test_low_activity_blocks_flow_flags(self):
        fl = venue_flags(self.base_f(activity_conf=0.05, buy_share_excess=0.5, vol_ratio=8.0), SC)
        self.assertLess(fl["buy_flow"], 0.06)
        self.assertLess(fl["volume"], 0.06)


if __name__ == "__main__":
    unittest.main()
