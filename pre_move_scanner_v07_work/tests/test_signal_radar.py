"""v0.7.3 Signal Radar: states, persistence / hysteresis, stale feeds, late rejection,
single-venue / single-print spikes and CEX / MM reshuffling (never a BUY)."""
import copy
import unittest

from server.engine.alerts import LABELS, HighConvictionAlerts
from tests.helpers import cfg

T = 10_000.0


def good(asset="QNT", premove=94.0, price=100.0, intel=None, **kw):
    """A result every strict check accepts (4/4 venues, book + buy flow on 3 venues, flat price)."""
    r = {
        "asset": asset, "premove": premove, "price": price,
        "status": "STRONG PRE-MOVE", "confidence": 0.82,
        "confirmed": 4, "coverage": 4, "coverage_total": 4, "live_share": 1.0,
        "confirmed_venues": ["binance", "coinbase", "kraken", "kucoin"],
        "families": ["thinning", "no_replenish", "buy_flow", "volume", "cross_venue"],
        "subscores": {"orderbook": 82.0, "buy_pressure": 78.0, "cross_venue": 84.0, "liquidity": 60.0},
        "agg": {"family": {"thinning": 0.82, "no_replenish": 0.76, "bid_support": 0.5, "spread": 0.2},
                "family_venue_counts": {"thinning": 3, "no_replenish": 3, "buy_flow": 3, "volume": 3},
                "vol_ratio": 2.1, "top_trade_share": 0.12, "ask_ratio": 0.62},
        "late": {"state": "FLAT"},
        "intel": intel,
    }
    r.update(kw)
    return r


def single_venue_spike(asset="SPK"):
    """A huge pre-move reading carried by one venue: one book, one venue of buy flow."""
    r = good(asset, premove=99.0, confirmed=1, confirmed_venues=["gate"])
    r["agg"]["family_venue_counts"] = {"thinning": 1, "no_replenish": 1, "buy_flow": 1, "volume": 1}
    return r


def single_print_spike(asset="WHL"):
    """Every venue lights up, but one large trade is 72 % of the tape."""
    r = good(asset, premove=97.0)
    r["agg"]["top_trade_share"] = 0.72
    return r


RESHUFFLE = {  # exchange / MM reshuffling: large wallet moves that net out
    "cex_flow": 0.0, "cex_flow_direction": "NEUTRAL", "whale": 0.0, "whale_direction": "NEUTRAL",
    "scarcity": 0.0, "scarcity_direction": "RESHUFFLING", "mm": 0.0, "mm_direction": "ROUTING",
}
MM_OFF_EXCHANGE = {  # MM withdrawing inventory from exchanges: inventory management, not buying
    "cex_flow": 70.0, "cex_flow_direction": "OUTFLOW", "cex_outflow_attributed_share": 0.0,
    "whale": 0.0, "whale_direction": "NEUTRAL", "scarcity": 0.0, "scarcity_direction": "NEUTRAL",
    "mm": 90.0, "mm_direction": "OFF_EXCHANGE",
}
UNATTRIBUTED_OUTFLOW = {  # CEX outflow mostly to UNLABELLED addresses + a small labelled whale
    "cex_flow": 85.0, "cex_flow_direction": "OUTFLOW", "cex_outflow_attributed_share": 0.2,
    "whale": 40.0, "whale_direction": "ACCUMULATION", "scarcity": 0.0, "scarcity_direction": "NEUTRAL",
    "mm": 0.0, "mm_direction": "NEUTRAL",
}
SUPPORTIVE = {  # labelled holders accumulating from exchanges + real supply drain
    "cex_flow": 75.0, "cex_flow_direction": "OUTFLOW", "cex_outflow_attributed_share": 0.8,
    "whale": 70.0, "whale_direction": "ACCUMULATION", "scarcity": 45.0, "scarcity_direction": "REAL_SUPPLY_DRAIN",
    "mm": 10.0, "mm_direction": "NEUTRAL",
}


class RadarBase(unittest.TestCase):
    def engine(self, **kw):
        return HighConvictionAlerts(cfg(alerts=kw)["alerts"])

    def run_for(self, e, res, start, seconds, step=5.0):
        """Feed the same result every `step` seconds; return (last update tuple, all events, fired?)."""
        events, fired, out, t = [], False, None, start
        while t <= start + seconds:
            out = e.update(copy.deepcopy(res), t)
            events += out[1]
            fired = fired or out[2]
            t += step
        return out, events, fired


class RadarStateTests(RadarBase):
    def test_none_when_nothing_qualifies(self):
        e = self.engine()
        e.update(good(premove=30.0), T)
        r = e.radar(T)
        self.assertEqual(r["state"], "NONE")
        self.assertEqual(r["label"], "NO HIGH-CONVICTION SETUP")
        self.assertIsNone(r["primary"])
        self.assertEqual(HighConvictionAlerts(cfg()["alerts"]).radar(T)["state"], "NONE")   # empty engine

    def test_watch_then_confirming_then_high_conviction(self):
        e = self.engine(persistence_seconds=120)
        # gates hold, strict does not (pre-move 78 < 90 without wallet data) -> WATCH
        e.update(good(premove=78.0), T)
        r = e.radar(T + 1)
        self.assertEqual(r["state"], "WATCH")
        self.assertEqual(r["label"], "WATCH / CONFIRMING")
        self.assertTrue(any("pre-move 78" in m for m in r["primary"]["missing"]))
        # strict holds -> CONFIRMING with a persistence timer
        e.update(good(), T + 10)
        e.update(good(), T + 70)
        r = e.radar(T + 70)
        self.assertEqual(r["state"], "CONFIRMING")
        self.assertEqual(r["label"], "WATCH / CONFIRMING")
        self.assertAlmostEqual(r["primary"]["persistence_s"], 60.0)
        self.assertEqual(r["primary"]["persistence_required_s"], 120.0)
        # held for 120 s -> HIGH-CONVICTION BUY SETUP
        active, events, fired = e.update(good(), T + 130)
        self.assertTrue(fired)
        r = e.radar(T + 130)
        self.assertEqual(r["state"], "HIGH_CONVICTION")
        self.assertEqual(r["label"], "HIGH-CONVICTION BUY SETUP")
        p = r["primary"]
        # the bar shows asset, evidence, venues, persistence, reasons and wallet status
        self.assertEqual(p["asset"], "QNT")
        self.assertGreater(p["evidence_score"], 70)
        self.assertEqual(p["confirmed"], 4)
        self.assertEqual(p["confirmed_venues"], ["binance", "coinbase", "kraken", "kucoin"])
        self.assertAlmostEqual(p["persistence_s"], 120.0)
        self.assertTrue(p["reasons"])
        self.assertEqual(p["wallet"]["status"], "unavailable")
        self.assertIn("not a probability", r["note"])

    def test_invalidated_headline_then_listed_then_gone(self):
        e = self.engine(persistence_seconds=10, invalidated_headline_seconds=600, invalidated_display_seconds=900)
        self.run_for(e, good(), T, 15)
        self.assertEqual(e.radar(T + 15)["state"], "HIGH_CONVICTION")
        moving = good(price=103.0)
        moving["late"] = {"state": "IN_PROGRESS"}
        _, events, _ = e.update(moving, T + 20)
        self.assertEqual(events[0]["event_type"], "high_conviction_cleared")
        r = e.radar(T + 21)
        self.assertEqual(r["state"], "INVALIDATED")
        self.assertEqual(r["label"], "INVALIDATED")
        self.assertIn("price no longer flat", r["primary"]["end_reason"])
        self.assertAlmostEqual(r["primary"]["price_change_pct"], 3.0)
        # after the headline window it is listed but the headline returns to NONE
        r = e.radar(T + 20 + 700)
        self.assertEqual(r["state"], "NONE")
        self.assertTrue(any(x["state"] == "INVALIDATED" for x in r["entries"]))
        self.assertFalse(e.radar(T + 20 + 1000)["entries"])

    def test_headline_priority(self):
        e = self.engine(persistence_seconds=10)
        self.run_for(e, good("AAA"), T, 15)                      # fired
        e.update(good("BBB", premove=78.0), T + 15)              # watch
        self.run_for(e, good("CCC"), T + 10, 5)                  # confirming
        r = e.radar(T + 15)
        self.assertEqual(r["state"], "HIGH_CONVICTION")
        self.assertEqual(r["primary"]["asset"], "AAA")
        self.assertEqual([x["state"] for x in r["entries"]], ["HIGH_CONVICTION", "CONFIRMING", "WATCH"])

    def test_disabled_never_fires(self):
        e = self.engine(enabled=False, persistence_seconds=1)
        _, events, fired = self.run_for(e, good(), T, 300)
        self.assertFalse(fired)
        self.assertEqual(e.radar(T + 300)["state"], "NONE")


class PersistenceHysteresisTests(RadarBase):
    def test_candidate_resets_on_any_break(self):
        e = self.engine(persistence_seconds=120)
        self.run_for(e, good(), T, 100)
        e.update(good(premove=60.0), T + 105)                    # one failed check: timer restarts
        _, _, fired = self.run_for(e, good(), T + 110, 100)
        self.assertFalse(fired)                                  # 100 s since restart, 210 s in total
        _, _, fired = self.run_for(e, good(), T + 215, 20)
        self.assertTrue(fired)

    def test_flicker_never_fires(self):
        e = self.engine(persistence_seconds=120)
        fired = False
        for i in range(120):                                     # 10 minutes, every other tick fails
            r = good() if i % 2 == 0 else good(premove=80.0)
            fired = fired or e.update(r, T + 5 * i)[2]
        self.assertFalse(fired)

    def test_brief_dip_survives_long_dip_invalidates(self):
        e = self.engine(persistence_seconds=10, clear_after_seconds=60)
        self.run_for(e, good(), T, 10)
        weak = good(premove=70.0)
        (active, ev, _), _, _ = self.run_for(e, weak, T + 15, 40)
        self.assertIsNotNone(active)
        self.assertTrue(active["dipping"])
        self.assertEqual(e.radar(T + 55)["state"], "HIGH_CONVICTION")
        self.assertTrue(e.radar(T + 55)["primary"]["dipping"])
        e.update(good(), T + 60)                                 # recovered inside the window
        self.assertFalse(e.get("QNT")["dipping"])
        (active, _, _), events, _ = self.run_for(e, weak, T + 65, 70)
        self.assertIsNone(active)
        ended = [x for x in events if x["event_type"] == "high_conviction_cleared"]
        self.assertEqual(len(ended), 1)
        self.assertIn("composite confirmation faded", ended[0]["message"])
        self.assertEqual(e.radar(T + 140)["state"], "INVALIDATED")

    def test_watch_lingers_on_soft_dip_only(self):
        e = self.engine(watch_linger_seconds=20)
        e.update(good(premove=78.0), T)
        e.update(good(premove=65.0), T + 10)                      # soft dip below the WATCH score
        self.assertEqual(e.radar(T + 10)["state"], "WATCH")
        e.update(good(premove=65.0), T + 25)
        self.assertEqual(e.radar(T + 25)["state"], "NONE")       # linger expired
        for hard in ({"late": {"state": "MOVING"}}, {"coverage": 2}, {"intel": dict(SUPPORTIVE, cex_flow_direction="INFLOW")}):
            e = self.engine(watch_linger_seconds=20)
            e.update(good(premove=78.0), T)
            e.update(good(premove=78.0, **hard), T + 5)
            self.assertEqual(e.radar(T + 5)["state"], "NONE", hard)

    def test_invalidated_asset_does_not_linger_as_watch(self):
        e = self.engine(persistence_seconds=10, clear_after_seconds=5, watch_linger_seconds=20)
        self.run_for(e, good(), T, 10)
        (active, _, _), _, _ = self.run_for(e, good(premove=40.0), T + 15, 10)
        self.assertIsNone(active)
        r = e.radar(T + 26)
        self.assertEqual(r["state"], "INVALIDATED")
        self.assertEqual([x["state"] for x in r["entries"]], ["INVALIDATED"])

    def test_refire_needs_fresh_persistence(self):
        e = self.engine(persistence_seconds=60, clear_after_seconds=10)
        self.run_for(e, good(), T, 60)
        moving = good()
        moving["late"] = {"state": "MOVING"}
        e.update(moving, T + 65)
        _, _, fired = self.run_for(e, good(), T + 70, 55)
        self.assertFalse(fired)
        _, _, fired = self.run_for(e, good(), T + 130, 5)
        self.assertTrue(fired)


class StaleFeedTests(RadarBase):
    def test_incomplete_feeds_block_watch_and_fire(self):
        e = self.engine(persistence_seconds=10)
        r = good(coverage=3, coverage_total=4)
        a = e.assess(r)
        self.assertFalse(a["strict"])
        self.assertFalse(a["watch"])
        self.assertFalse(a["gate"]["feed_complete"])
        _, _, fired = self.run_for(e, r, T, 300)
        self.assertFalse(fired)
        self.assertEqual(e.radar(T + 300)["state"], "NONE")

    def test_no_data_never_qualifies(self):
        e = self.engine(persistence_seconds=1)
        r = good(premove=None)
        a = e.assess(r)
        self.assertFalse(a["watch"] or a["strict"])
        self.assertEqual(a["evidence_score"], 0.0)

    def test_active_alert_invalidated_when_feeds_stay_incomplete(self):
        e = self.engine(persistence_seconds=10, clear_after_seconds=60)
        self.run_for(e, good(), T, 10)
        (active, _, _), events, _ = self.run_for(e, good(coverage=2, coverage_total=4), T + 15, 70)
        self.assertIsNone(active)
        msg = [x for x in events if x["event_type"] == "high_conviction_cleared"][0]["message"]
        self.assertIn("feeds stale / incomplete (2/4 selected venues live)", msg)

    def test_sweep_invalidates_assets_that_stop_updating(self):
        e = self.engine(persistence_seconds=10, clear_after_seconds=60)
        self.run_for(e, good(), T, 10)
        self.assertEqual(e.sweep(T + 30), [])                   # still fresh
        ev = e.sweep(T + 80)
        self.assertEqual(len(ev), 1)
        self.assertIn("no fresh data for 70s", ev[0]["message"])
        self.assertIsNone(e.get("QNT"))
        self.assertEqual(e.radar(T + 81)["state"], "INVALIDATED")

    def test_sweep_asset_left_universe(self):
        e = self.engine(persistence_seconds=10)
        self.run_for(e, good(), T, 10)
        ev = e.sweep(T + 11, live_assets={"OTHER"})
        self.assertIn("asset left the scanner universe", ev[0]["message"])

    def test_stale_watch_and_confirming_drop_off_the_radar(self):
        e = self.engine(persistence_seconds=120, clear_after_seconds=60)
        e.update(good("AAA", premove=78.0), T)
        e.update(good("BBB"), T)
        self.assertEqual(len(e.radar(T + 5)["entries"]), 2)
        self.assertEqual(e.radar(T + 61)["entries"], [])
        self.assertEqual(e.radar(T + 61)["state"], "NONE")


class LateRejectionTests(RadarBase):
    def test_moving_states_never_watch_or_fire(self):
        for st in ("MOVING", "IN_PROGRESS", "LATE"):
            e = self.engine(persistence_seconds=1)
            r = good(premove=99.0)
            r["late"] = {"state": st}
            a = e.assess(r)
            self.assertFalse(a["strict"], st)
            self.assertFalse(a["watch"], st)
            self.assertIn("price no longer flat", a["missing"])
            _, _, fired = self.run_for(e, r, T, 300)
            self.assertFalse(fired, st)

    def test_unknown_late_state_is_not_flat(self):
        e = self.engine()
        r = good()
        r["late"] = None
        self.assertFalse(e.assess(r)["checks"]["price_still_flat"])

    def test_late_move_invalidates_immediately_despite_hysteresis(self):
        e = self.engine(persistence_seconds=10, clear_after_seconds=600)
        self.run_for(e, good(), T, 10)
        late = good(price=108.0)
        late["late"] = {"state": "LATE"}
        active, events, _ = e.update(late, T + 15)
        self.assertIsNone(active)
        self.assertIn("price no longer flat: late (+8.0% since fire)", events[0]["message"])


class SpikeTests(RadarBase):
    def test_single_venue_spike_never_reaches_radar(self):
        e = self.engine(persistence_seconds=10)
        r = single_venue_spike()
        a = e.assess(r)
        self.assertFalse(a["strict"])
        self.assertFalse(a["watch"])
        self.assertFalse(a["gate"]["market_structure"])
        self.assertFalse(a["gate"]["cross_venue"])
        _, _, fired = self.run_for(e, r, T, 600)
        self.assertFalse(fired)
        self.assertEqual(e.radar(T + 600)["state"], "NONE")

    def test_two_confirmations_but_structure_on_one_venue(self):
        e = self.engine(persistence_seconds=10)
        r = good(premove=99.0, confirmed=3)
        r["agg"]["family_venue_counts"] = {"thinning": 1, "no_replenish": 1, "bid_support": 0, "buy_flow": 3}
        a = e.assess(r)
        self.assertFalse(a["watch"] or a["strict"])

    def test_single_large_trade_never_reaches_radar(self):
        e = self.engine(persistence_seconds=10)
        r = single_print_spike()
        a = e.assess(r)
        self.assertFalse(a["strict"])
        self.assertFalse(a["watch"])
        self.assertIn("one large trade dominates the tape", a["missing"])
        _, _, fired = self.run_for(e, r, T, 600)
        self.assertFalse(fired)

    def test_market_structure_is_mandatory(self):
        """Buy flow everywhere, very high score, but no book-side family -> not even WATCH."""
        e = self.engine(persistence_seconds=10)
        r = good(premove=99.0, families=["buy_flow", "volume", "cross_venue"])
        r["agg"]["family_venue_counts"] = {"thinning": 0, "no_replenish": 0, "bid_support": 0, "buy_flow": 4}
        a = e.assess(r)
        self.assertFalse(a["gate"]["market_structure"])
        self.assertFalse(a["watch"] or a["strict"])
        weak_book = good(premove=99.0)
        weak_book["subscores"]["orderbook"] = 40.0
        self.assertFalse(e.assess(weak_book)["gate"]["market_structure"])

    def test_spikes_do_not_disturb_a_real_setup(self):
        e = self.engine(persistence_seconds=60)
        fired = set()
        for i in range(30):
            t = T + 5 * i
            for r in (good("QNT"), single_venue_spike(), single_print_spike()):
                if e.update(r, t)[2]:
                    fired.add(r["asset"])
        self.assertEqual(fired, {"QNT"})


class ReshufflingTests(RadarBase):
    def test_reshuffling_is_neutral_and_listed_as_not_counted(self):
        e = self.engine()
        w = e.assess(good(intel=RESHUFFLE))["wallet"]
        self.assertEqual(w["status"], "neutral")
        self.assertEqual(w["supportive"], [])
        self.assertIsNone(w["score"])
        self.assertIn("scarcity:reshuffling", w["reshuffle_not_counted"])
        self.assertIn("mm:routing", w["reshuffle_not_counted"])

    def test_mm_off_exchange_is_never_supportive(self):
        e = self.engine()
        a = e.assess(good(premove=84.0, intel=MM_OFF_EXCHANGE))
        self.assertEqual(a["wallet"]["status"], "neutral")
        self.assertIn("mm:off_exchange", a["wallet"]["reshuffle_not_counted"])
        self.assertEqual(a["threshold"], 86.0)                  # the lower 82 bar needs supportive wallets
        self.assertFalse(a["strict"])

    def test_unattributed_outflow_is_never_supportive(self):
        e = self.engine()
        a = e.assess(good(premove=84.0, intel=UNATTRIBUTED_OUTFLOW))
        self.assertEqual(a["wallet"]["status"], "neutral")
        self.assertNotIn("cex_flow:attributed_outflow", a["wallet"]["supportive"])
        self.assertFalse(a["strict"])

    def test_reshuffling_cannot_create_a_setup_by_itself(self):
        """Weak market + loud reshuffling: never WATCH, never fires."""
        e = self.engine(persistence_seconds=10)
        for intel in (RESHUFFLE, MM_OFF_EXCHANGE, UNATTRIBUTED_OUTFLOW):
            r = good(premove=60.0, intel=intel)
            r["subscores"]["orderbook"] = 30.0
            _, _, fired = self.run_for(e, r, T, 300)
            self.assertFalse(fired)
            self.assertFalse(e.assess(r)["watch"])

    def test_attributed_accumulation_is_supportive(self):
        e = self.engine(persistence_seconds=10)
        a = e.assess(good(premove=84.0, intel=SUPPORTIVE))
        self.assertEqual(a["wallet"]["status"], "supportive")
        self.assertEqual(a["threshold"], 82.0)
        self.assertTrue(a["strict"])
        self.assertIn("labelled holders accumulating (wallet supportive)", a["reasons"])
        # wallet support alone never replaces market structure
        r = good(premove=84.0, intel=SUPPORTIVE, families=["buy_flow", "volume", "cross_venue"])
        self.assertFalse(e.assess(r)["watch"])

    def test_hostile_flow_blocks_watch(self):
        e = self.engine()
        hostile = dict(SUPPORTIVE, cex_flow=80.0, cex_flow_direction="INFLOW")
        a = e.assess(good(intel=hostile))
        self.assertEqual(a["wallet"]["status"], "hostile")
        self.assertFalse(a["watch"] or a["strict"])

    def test_explicit_wallet_state_is_carried_to_the_radar(self):
        e = self.engine(persistence_seconds=1)
        r = good()
        r["wallet_status"] = {"state": "UNSUPPORTED", "label": "UNSUPPORTED", "reason": "native coin of its own chain"}
        self.run_for(e, r, T, 5)
        w = e.radar(T + 5)["primary"]["wallet"]
        self.assertEqual(w["state"], "UNSUPPORTED")
        self.assertEqual(w["status"], "unavailable")


class PersistenceRowsTests(RadarBase):
    def test_pop_changes_fire_refresh_end(self):
        e = self.engine(persistence_seconds=10, persist_update_seconds=30)
        self.run_for(e, good(), T, 10)
        rows = e.pop_changes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "HIGH_CONVICTION")
        self.assertIsNone(rows[0]["ended_ts"])
        self.assertFalse(any(k.startswith("_") for k in rows[0]))
        self.assertEqual(e.pop_changes(), [])
        self.run_for(e, good(), T + 15, 10)                       # < persist_update_seconds
        self.assertEqual(e.pop_changes(), [])
        self.run_for(e, good(premove=97.0), T + 30, 15)
        rows = e.pop_changes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["peak_premove"], 97.0)
        moving = good(price=102.0)
        moving["late"] = {"state": "MOVING"}
        e.update(moving, T + 50)
        rows = e.pop_changes()
        self.assertEqual(rows[0]["state"], "INVALIDATED")
        self.assertEqual(rows[0]["ended_ts"], T + 50)
        self.assertEqual(rows[0]["price_at_end"], 102.0)
        self.assertIn("price no longer flat", rows[0]["end_reason"])

    def test_labels_cover_all_states(self):
        self.assertEqual(set(LABELS.values()), {"NO HIGH-CONVICTION SETUP", "WATCH / CONFIRMING",
                                                "HIGH-CONVICTION BUY SETUP", "INVALIDATED"})


if __name__ == "__main__":
    unittest.main()
