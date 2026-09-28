"""End-to-end scenarios: SIM venues → engine → scoring → status.

These encode the reference cases from the v0.7 brief:
  A  100 % buys on ~$70 / 2 trades              -> low score, LOW CONFIDENCE
  B  70 % buys, heavy tape, asks −45 %, 4 venues, price flat -> EMERGING, then CONFIRMED/STRONG
  C  same flow but price already +9 % in 30 min -> LATE, capped
"""
import copy
import unittest

from tests.helpers import cfg
from tests.scenario import Scenario

_WARM = None


def warmed() -> Scenario:
    global _WARM
    if _WARM is None:
        _WARM = Scenario(seed=11)
        _WARM.run(50 * 60)
    return copy.deepcopy(_WARM)


def case_b_control(start):
    def ctrl(ts, s):
        prog = min(1.0, (ts - start) / 300.0)
        for m in s.venues:
            m.p.deep_ask_mult = 1 - 0.55 * prog
            m.p.ask_refill = 0.35 - 0.3 * prog
            m.p.buy_prob = 0.5 + 0.2 * prog
            m.p.rate_mult = 1 + 2.5 * prog
    return ctrl


def first_time(results, pred, start):
    for r in results:
        if pred(r):
            return r["ts"] - start
    return None


class ReferenceCases(unittest.TestCase):
    def test_normal_market_stays_quiet(self):
        sc = warmed()
        n0 = len(sc.results)
        sc.run(40 * 60)
        rs = sc.results[n0:]
        self.assertLess(max(r["premove"] for r in rs), 40.0)
        self.assertFalse(any(r["status"] in ("EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE") for r in rs))

    def test_case_a_tiny_tape_is_low_confidence(self):
        sc = warmed()

        def ctrl(ts, s):
            for m in s.venues:
                m.p.trade_rate, m.p.rate_mult, m.p.trade_usd, m.p.buy_prob = 0.017, 1.0, 35.0, 1.0
        n0 = len(sc.results)
        sc.run(5 * 60, ctrl)
        rs = sc.results[n0 + 60:]
        self.assertLess(max(r["premove"] for r in rs), 15.0)
        self.assertTrue(all(r["status"] == "LOW CONFIDENCE" for r in rs[-60:]))
        last = sc.results[-1]
        self.assertLess(last["agg"]["vol_60"], 1000.0)
        self.assertIn("Low confidence", last["reason"])

    def test_case_b_structure_with_flat_price(self):
        sc = warmed()
        start = sc.t
        n0 = len(sc.results)
        sc.run(10 * 60, case_b_control(start))
        rs = sc.results[n0:]
        t_emerging = first_time(rs, lambda r: r["status"] == "EMERGING", start)
        t_confirmed = first_time(rs, lambda r: r["status"] in ("CONFIRMED PRE-MOVE", "STRONG PRE-MOVE"), start)
        self.assertIsNotNone(t_emerging)
        self.assertIsNotNone(t_confirmed)
        self.assertLess(t_emerging, t_confirmed)             # two-speed: early warning first
        self.assertLessEqual(t_emerging, 6 * 60)
        best = max(r["premove"] for r in rs)
        self.assertGreaterEqual(best, 65.0)
        self.assertTrue(all(abs(r["returns"].get(15) or 0) < 1.0 for r in rs))  # price stayed flat
        top = max(rs, key=lambda r: r["premove"])
        self.assertGreaterEqual(top["confirmed"], 3)
        self.assertIn("Ask depth", top["reason"])
        self.assertGreaterEqual(top["n_families"], 4)

    def test_emerging_does_not_wait_for_full_median(self):
        sc = warmed()
        start = sc.t
        n0 = len(sc.results)
        sc.run(8 * 60, case_b_control(start))
        rs = sc.results[n0:]
        t_inst = first_time(rs, lambda r: (r["instant"] or 0) >= 55 and r["n_families"] >= 2 and r["confirmed"] >= 2, start)
        t_em = first_time(rs, lambda r: r["status"] in ("EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE"), start)
        self.assertIsNotNone(t_inst)
        self.assertLessEqual(t_em - t_inst, 60.0)  # within 30-60 s, not the 2-3 min confirmation

    def test_single_venue_anomaly_is_capped(self):
        sc = warmed()
        start = sc.t
        ctrl_all = case_b_control(start)

        def ctrl(ts, s):
            ctrl_all(ts, s)
            for m in s.venues[1:]:  # undo on all but the biggest venue
                m.p.deep_ask_mult, m.p.ask_refill, m.p.buy_prob, m.p.rate_mult = 1.0, 0.35, 0.5, 1.0
        n0 = len(sc.results)
        sc.run(8 * 60, ctrl)
        rs = sc.results[n0:]
        self.assertLessEqual(max(r["premove"] for r in rs), 59.0)
        self.assertFalse(any(r["status"] in ("CONFIRMED PRE-MOVE", "STRONG PRE-MOVE") for r in rs))

    def test_stale_venues_cannot_score(self):
        sc = warmed()
        start = sc.t
        ctrl_b = case_b_control(start)

        def ctrl(ts, s):
            ctrl_b(ts, s)
            if ts - start > 120:
                for m in s.venues[1:]:
                    m.p.frozen = True
        n0 = len(sc.results)
        sc.run(8 * 60, ctrl)
        tail = sc.results[-120:]
        self.assertTrue(all(r["coverage"] == 1 for r in tail))
        self.assertLessEqual(max(r["premove"] for r in tail), 42.0)
        # ... and with every venue frozen the asset goes STALE with no score.
        sc.each(lambda m: setattr(m.p, "frozen", True))
        sc.run(60)
        self.assertEqual(sc.results[-1]["status"], "STALE")
        self.assertIsNone(sc.results[-1]["premove"])

    def test_single_large_print_does_not_rank(self):
        sc = warmed()
        state = {"done": False}

        def ctrl(ts, s):
            if not state["done"]:
                s.venues[0].p.size_mult = 400.0  # one ~$160k print ...
                state["done"] = True
            else:
                s.venues[0].p.size_mult = 1.0    # ... then back to normal
        n0 = len(sc.results)
        sc.run(4 * 60, ctrl)
        rs = sc.results[n0:]
        self.assertLess(max(r["premove"] for r in rs), 40.0)
        self.assertFalse(any(r["status"] in ("EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE") for r in rs))


class LateAndWarmupCases(unittest.TestCase):
    def test_case_c_pump_is_late(self):
        sc = Scenario(seed=21)
        sc.run(50 * 60)

        def ctrl(ts, s):
            for m in s.venues:
                m.p.drift_pct_per_min, m.p.buy_prob, m.p.rate_mult = 0.30, 0.68, 2.0
        n0 = len(sc.results)
        sc.run(32 * 60, ctrl)
        rs = sc.results[n0:]
        late = [r for r in rs if (r["returns"].get(30) or 0) >= 8.0]
        self.assertTrue(late)
        self.assertTrue(all(r["status"] == "LATE" and r["premove"] <= 25.0 for r in late))
        self.assertFalse(any(r["status"] in ("CONFIRMED PRE-MOVE", "STRONG PRE-MOVE") for r in rs
                             if (r["returns"].get(15) or 0) >= 2.0))
        self.assertIn("Move already expanded", late[-1]["reason"])

    def test_vol_normalised_late_on_calm_asset(self):
        c = cfg(engine={"min_baseline_minutes": 20, "baseline_lag_minutes": 10},
                scoring={"late": {"min_vol_minutes": 120}})
        sc = Scenario(n_venues=2, seed=5, config=c)
        sc.run(150 * 60)

        def ctrl(ts, s):
            for m in s.venues:
                m.p.drift_pct_per_min = 0.15  # ~+2.3 % in 15 min: under the 5 % hard line
        n0 = len(sc.results)
        sc.run(16 * 60, ctrl)
        rs = sc.results[n0:]
        hit = [r for r in rs if r["status"] == "LATE"]
        self.assertTrue(hit, "vol-normalised displacement should flag the move as LATE")
        self.assertEqual(hit[0]["late"]["trigger"]["kind"], "vol")
        self.assertLess(hit[0]["returns"][15], 5.0)

    def test_warmup_suppresses_scores(self):
        sc = Scenario(seed=31)
        start = sc.t
        sc.run(25 * 60, case_b_control(start))
        rs = sc.results
        self.assertLessEqual(max(r["premove"] or 0 for r in rs), 44.0)
        self.assertFalse(any(r["status"] in ("EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE") for r in rs))
        self.assertEqual(rs[-1]["status"], "WARMING")


if __name__ == "__main__":
    unittest.main()
