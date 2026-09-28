import unittest

from tests.helpers import T0, cfg
from server.engine.events import EventDetector
from server.engine.leadlag import price_lead_lag


def res(ts, premove=10.0, confirmed=0, returns=None, transitions=None, venues=None, status="NORMAL"):
    return {"asset": "QNT", "ts": ts, "premove": premove, "confirmed": confirmed, "coverage": 4,
            "returns": returns or {}, "transitions": transitions or [], "venues": venues or [],
            "status": status, "reason": ""}


class EventTest(unittest.TestCase):
    def setUp(self):
        self.d = EventDetector(cfg()["events"])

    def types(self, evs):
        return [e["event_type"] for e in evs]

    def test_score_crossing_hysteresis(self):
        seq = [50, 56, 54, 56.5, 52, 49, 56]
        out = []
        for i, s in enumerate(seq):
            out += self.d.process(res(T0 + i, premove=s))
        ups = [e for e in out if e["event_type"] == "score_cross_up" and e["level"] == 55]
        downs = [e for e in out if e["event_type"] == "score_cross_down" and e["level"] == 55]
        self.assertEqual(len(ups), 2)   # 56 and the final 56 after dropping below 50
        self.assertEqual(len(downs), 1)

    def test_confirmation_count_debounced(self):
        out = []
        t = T0
        out += self.d.process(res(t, confirmed=1))
        for i in range(70):  # establish 1
            t += 1
            out += self.d.process(res(t, confirmed=1))
        for i in range(40):  # flicker 1/2 — never held 60 s
            t += 1
            out += self.d.process(res(t, confirmed=2 if i % 2 else 1))
        self.assertNotIn("venue_confirmations", self.types(out))
        for i in range(65):
            t += 1
            out += self.d.process(res(t, confirmed=3))
        conf = [e for e in out if e["event_type"] == "venue_confirmations"]
        self.assertEqual(len(conf), 1)
        self.assertIn("1 → 3", conf[0]["message"])

    def test_breakout_has_precursors(self):
        t = T0
        onset = {"kind": "onset", "venue": "Gate", "family": "thinning", "ts": t, "strength": 0.7,
                 "detail": {"ask1_ratio": 0.62}}
        self.d.process(res(t, transitions=[onset]))
        self.d.process(res(t + 300, premove=72))
        out = self.d.process(res(t + 1800, returns={15: 3.4, 60: 3.9}))
        bo = [e for e in out if e["event_type"] == "price_breakout"]
        self.assertEqual(len(bo), 1)
        pre = bo[0]["evidence"]["precursors"]
        self.assertEqual(pre[0]["event_type"], "thinning_onset")
        self.assertEqual(pre[0]["minutes_before"], 30)
        self.assertIn("Preceded by", bo[0]["message"])
        # not re-emitted while still up
        self.assertEqual(self.d.process(res(t + 1801, returns={15: 3.6}))[:0], [])
        self.assertNotIn("price_breakout", self.types(self.d.process(res(t + 1802, returns={15: 3.6}))))

    def test_cross_venue_count_event(self):
        v = [{"active_families": ["thinning"]}, {"active_families": ["thinning"]}, {"active_families": []}]
        out = self.d.process(res(T0, venues=v))
        self.assertIn("2/4 venues confirm ask thinning", [e["message"] for e in out])
        self.assertEqual(self.d.process(res(T0 + 1, venues=v)), [])  # no repeat

    def test_status_escalation_and_gap(self):
        tr = lambda a, b: [{"kind": "status", "from": a, "to": b, "ts": 0}]
        out = self.d.process(res(T0, transitions=tr("NORMAL", "EMERGING")))
        out += self.d.process(res(T0 + 5, transitions=tr("EMERGING", "NORMAL")))       # de-escalation too soon
        out += self.d.process(res(T0 + 10, transitions=tr("NORMAL", "CONFIRMED PRE-MOVE")))  # escalation
        msgs = [e["message"] for e in out if e["category"] == "STATUS"]
        self.assertEqual(len(msgs), 2)


class LeadLagTest(unittest.TestCase):
    def test_leader_detected(self):
        import random
        rng = random.Random(3)
        base = [100.0]
        for _ in range(599):
            base.append(base[-1] * (1 + rng.gauss(0, 0.0005)))
        lead = base[3:] + [base[-1]] * 3       # moves 3 s earlier
        out = price_lead_lag({"ref": base, "fast": lead}, "ref", max_lag=8)
        self.assertEqual(out["fast"]["lag_s"], 3)
        self.assertEqual(out["fast"]["note"], "leads")


if __name__ == "__main__":
    unittest.main()
