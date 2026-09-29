"""Signal Radar / HIGH-CONVICTION with the v0.8 wallet evidence domain: classified multi-chain events ->
scores -> explicit status gate -> radar. Wallet evidence is independent and never mandatory; exchange-
internal moves, custody shifts and market-maker routing never count as buying; manual and non-EVM assets
are treated exactly like the others. (The v0.7.3 radar behaviour - persistence, hysteresis, stale feeds,
late rejection, single-venue / single-print spikes, invalidation - stays covered in test_signal_radar.py.)"""
import copy
import unittest

from server.engine.alerts import LABELS, HighConvictionAlerts
from server.intel.labels import Label, LabelRegistry
from server.intel.monitor import WalletMonitor
from server.intel.providers import ProviderRegistry
from server.intel.scores import compute_scores
from server.intel.status import asset_status, gate_scores
from tests.helpers import cfg
from tests.test_intel import A, COVERED, registry, tx
from tests.test_signal_radar import good

T = 50_000.0
NOW = 1_760_000_000.0


def engine(**kw):
    return HighConvictionAlerts(cfg(alerts=kw)["alerts"])


def run(e, res, start, seconds, step=5.0):
    fired, t, out = False, start, None
    while t <= start + seconds:
        out = e.update(copy.deepcopy(res), t)
        fired = fired or out[2]
        t += step
    return out, fired


def wallet_from_events(rows, vol=20e6, thinning=0.5):
    """Events -> scores -> the dict the engine receives (what service._intel_ctx passes on)."""
    s = compute_scores("QNT", rows, COVERED, NOW, vol, thinning)
    return {k: s.get(k) for k in ("mm", "whale", "cex_flow", "scarcity", "mm_direction", "whale_direction",
                                  "cex_flow_direction", "scarcity_direction", "cex_outflow_attributed_share")}


class WalletDomainTests(unittest.TestCase):
    def setUp(self):
        self.r = registry()

    def test_supportive_neutral_unavailable_and_contradictory(self):
        supportive = wallet_from_events([tx("binance", "whale1", 1.5e6, self.r), tx("okx", "whale2", 1.4e6, self.r),
                                         tx("kraken", "whale3", 1.2e6, self.r)])
        neutral = wallet_from_events([])
        contradictory = wallet_from_events([tx("whale1", "binance", 3.0e6, self.r), tx("anon", "okx", 2.0e6, self.r)])
        e = engine()
        cases = {"supportive": supportive, "neutral": neutral, "unavailable": None, "hostile": contradictory}
        thresholds = {"supportive": 82.0, "neutral": 86.0, "unavailable": 90.0}
        for status, intel in cases.items():
            a = e.assess(good(intel=intel))
            self.assertEqual(a["wallet"]["status"], status, status)
            if status in thresholds:
                self.assertEqual(a["threshold"], thresholds[status])
        self.assertEqual(e.assess(good(intel=contradictory))["wallet"]["text"], "contradictory")
        self.assertIn("wallet data unavailable (not required)", e.assess(good())["highlights"])

    def test_wallet_is_not_mandatory(self):
        _, fired = run(engine(persistence_seconds=60), good(premove=94.0, intel=None), T, 90)
        self.assertTrue(fired)                                   # strong market structure alone can fire
        _, fired = run(engine(persistence_seconds=60), good(premove=84.0, intel=None), T, 300)
        self.assertFalse(fired)                                  # ...but needs the higher no-wallet threshold

    def test_supportive_wallet_lowers_the_bar_contradictory_vetoes(self):
        supportive = wallet_from_events([tx("binance", "whale1", 1.5e6, self.r), tx("okx", "whale2", 1.4e6, self.r),
                                         tx("kraken", "whale3", 1.2e6, self.r)])
        _, fired = run(engine(persistence_seconds=60), good(premove=84.0, intel=supportive), T, 90)
        self.assertTrue(fired)
        contradictory = wallet_from_events([tx("whale1", "binance", 3.0e6, self.r)])
        e = engine(persistence_seconds=60)
        _, fired = run(e, good(premove=97.0, intel=contradictory), T, 300)
        self.assertFalse(fired)
        self.assertEqual(e.radar(T + 300)["state"], "NONE")         # not even WATCH
        # an active setup is invalidated at once when wallet flow turns contradictory
        e = engine(persistence_seconds=10, clear_after_seconds=600)
        run(e, good(), T, 15)
        _, events, _ = e.update(good(intel=contradictory), T + 20)
        self.assertIn("hostile wallet", events[0]["message"])

    def test_internal_custody_and_mm_routing_never_count_as_buying(self):
        moves = [tx("binance", "binance_cold", 9e6, self.r), tx("coinbase_hot", "coinbase_prime", 8e6, self.r),
                 tx("binance", "custodian", 7e6, self.r), tx("binance", "wintermute", 6e6, self.r),
                 tx("wintermute", "okx", 5e6, self.r), tx("binance", "okx", 4e6, self.r), tx("bridge", "anon", 3e6, self.r)]
        intel = wallet_from_events(moves)
        a = engine().assess(good(premove=84.0, intel=intel))
        self.assertNotEqual(a["wallet"]["status"], "supportive")
        self.assertEqual(a["threshold"], 86.0)                    # neutral at best: no lowered bar
        _, fired = run(engine(persistence_seconds=60), good(premove=84.0, intel=intel), T, 300)
        self.assertFalse(fired)

    def test_unattributed_cex_outflow_is_not_buying(self):
        intel = wallet_from_events([tx("binance", "anon", 4e6, self.r), tx("okx", "anon2", 3e6, self.r)])
        a = engine().assess(good(premove=84.0, intel=intel))
        self.assertNotEqual(a["wallet"]["status"], "supportive")

    def test_degraded_or_unsupported_wallet_is_unavailable_not_zero(self):
        lab = LabelRegistry()
        lab.add(Label("ethereum", A["binance"], "Binance", "CEX_HOT", "HIGH", "t"))

        class Degraded:
            name, label, family, enabled, keyed, key_env = "fake", "Fake", "evm", True, True, None
            chains = frozenset({"ethereum"})

            def supports(self, c):
                return c in self.chains

            def state(self, chain=None):
                return {"state": "degraded", "reason": "rate-limited"}
        m = WalletMonitor(dict(cfg()["intel"], tokens={"QNT": {"chain": "ethereum", "contract": "0x" + "4a" * 20}}),
                          lab, ProviderRegistry([Degraded()]), lambda a: 1.0, clock=lambda: NOW)
        scores = {"mm": 0.0, "whale": 55.0, "cex_flow": 60.0, "scarcity": 0.0, "whale_direction": "ACCUMULATION",
                  "cex_flow_direction": "OUTFLOW"}
        st = asset_status("QNT", enabled=True, monitor=m, providers=m.providers, labels=lab, scores=scores, now=NOW)
        self.assertEqual(st["state"], "DEGRADED")
        gated = gate_scores(scores, st)
        self.assertTrue(all(gated[k] is None for k in ("mm", "whale", "cex_flow", "scarcity")))
        self.assertEqual(engine().assess(good(intel=gated))["wallet"]["status"], "unavailable")


class ParityTests(unittest.TestCase):
    def test_manual_and_non_evm_assets_are_treated_identically(self):
        base = engine().assess(good("QNT"))
        for variant in (good("RAIL", manual=True), good("BTC", chain="bitcoin"), good("XRP", manual=True, rank=5)):
            a = engine().assess(variant)
            for k in ("strict", "watch", "threshold", "evidence_score", "checks", "gate"):
                self.assertEqual(a[k], base[k], (variant["asset"], k))

    def test_radar_states_labels_and_evidence_display(self):
        self.assertEqual((LABELS["WATCH"], LABELS["CONFIRMING"]), ("WATCH", "CONFIRMING"))
        e = engine(persistence_seconds=120)
        e.update(good(premove=78.0), T)
        r = e.radar(T + 1)
        p = r["primary"]
        self.assertEqual((r["state"], r["label"]), ("WATCH", "WATCH"))
        self.assertEqual(p["checks_total"], 15)
        self.assertLess(p["checks_passed"], 15)
        run(e, good(), T + 10, 60)
        r = e.radar(T + 70)
        self.assertEqual((r["state"], r["label"]), ("CONFIRMING", "CONFIRMING"))
        self.assertEqual((r["primary"]["checks_passed"], r["primary"]["gates_passed"]), (15, 7))
        run(e, good(), T + 75, 60)
        r = e.radar(T + 135)
        self.assertEqual((r["state"], r["label"]), ("HIGH_CONVICTION", "HIGH-CONVICTION BUY SETUP"))
        hl = r["primary"]["highlights"]
        for text in ("ask supply draining", "sustained buy pressure", "price still flat"):
            self.assertIn(text, hl)
        self.assertTrue(any(h.startswith("volume accelerating") for h in hl))
        self.assertIn("not a probability", r["note"])
        for bad in ("guaranteed", "100% buy"):
            self.assertNotIn(bad, str(r).lower())


if __name__ == "__main__":
    unittest.main()
