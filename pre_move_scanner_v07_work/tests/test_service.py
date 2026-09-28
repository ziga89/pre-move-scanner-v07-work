"""Service-level tests in SIM mode (real asyncio feeds, real SQLite, no network)."""
import asyncio
import json
import shutil
import unittest

from tests.helpers import cfg, scratch_dir
from server.service import ScannerService


def sim_cfg(d, **kw):
    c = cfg(mode="sim", feeds={"backend": "sim", "resync_grace_seconds": 2},
            sim={"assets": 5, "venues_per_asset": 3, "seed": 5, "scenarios": False},
            storage={"path": str(d / "svc.db")}, **kw)
    return c


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("service")
        for p in self.d.glob("*"):
            p.unlink()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_end_to_end_sim(self):
        async def go():
            svc = ScannerService(sim_cfg(self.d))
            await svc.start()
            try:
                await asyncio.sleep(7)
                top = svc.top_payload()
                self.assertEqual(len(top["rows"]), 5)
                self.assertEqual(top["mode"], "sim")
                json.dumps(top)  # JSON-safe
                a = top["rows"][0]["asset"]
                coin = svc.coin_payload(a)
                self.assertEqual(coin["asset"], a)
                self.assertTrue(coin["venues"])
                self.assertIn("selection", coin)
                self.assertTrue(coin["books"])
                self.assertIsNone(coin["subscores"]["whale"])     # no wallet intel -> N/A, not 0
                self.assertIsNone(svc.coin_payload("NOPE"))
                h = svc.health_payload()
                self.assertEqual(h["status"]["markets"], 15)
                self.assertTrue(all(e["state"] in ("LIVE", "PARTIAL") for e in h["engine"]["exchanges"].values()))
                self.assertGreater(h["status"]["ticks"], 3)
                self.assertEqual(svc.universe_payload()["target"], 100)
                compat = svc.state_compat()
                self.assertIn(a, compat["assets"])
                svc.db.flush()
                hist = await svc.history(a, 1)
                self.assertTrue(hist["composite"])
                self.assertTrue(hist["venues"])
                n_sel = svc.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM venue_selection").fetchone()[0])
                self.assertGreaterEqual(n_sel, 15)
                n_uni = svc.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0])
                self.assertGreaterEqual(n_uni, 5)
                json.dumps(await svc.timeline(a))
                json.dumps(await svc.outcomes())
            finally:
                await svc.stop()
        asyncio.run(go())

    def test_radar_and_explicit_wallet_states_in_sim(self):
        async def go():
            svc = ScannerService(sim_cfg(self.d))
            await svc.start()
            try:
                await asyncio.sleep(4)
                top = svc.top_payload()
                json.dumps(top)
                self.assertIn(top["radar"]["state"], ("NONE", "WATCH", "CONFIRMING", "HIGH_CONVICTION", "INVALIDATED"))
                self.assertTrue(top["radar"]["label"])
                self.assertIn("feeds", top["radar"])
                w = top["wallet_intel"]
                self.assertEqual((w["state"], w["text"]), ("OFF", "Wallet intel OFF"))
                self.assertEqual(w["by_state"], {"OFF": 5})
                for r in top["rows"]:                  # explicit OFF, never a bare N/A
                    self.assertEqual(r["wallet"]["state"], "OFF")
                    for k in ("mm", "whale", "cex_flow", "scarcity"):
                        self.assertEqual(r["wallet"]["scores"][k]["label"], "OFF")
                coin = svc.coin_payload(top["rows"][0]["asset"])
                self.assertEqual(coin["wallet_status"]["state"], "OFF")
                self.assertIn("radar_entry", coin)
                h = svc.health_payload()
                self.assertEqual(h["wallet_intel"]["state"], "OFF")
                self.assertIn("radar_state", h["alerts"])
                self.assertEqual(h["etherscan"], {"enabled": False})
                self.assertEqual(svc.radar_payload()["state"], top["radar"]["state"])
            finally:
                await svc.stop()
        asyncio.run(go())

    def test_alert_fires_persists_marks_history_and_closes_on_restart(self):
        """Service plumbing: a (forced) setup fires, is written to SQLite, shows on the radar and the
        chart history, ends through the normal invalidation path, and an alert left open is closed
        at the next start. The strict checks themselves are covered in test_signal_radar."""
        c = sim_cfg(self.d, alerts={"persistence_seconds": 2, "clear_after_seconds": 2})
        forced = set()

        def force(svc):
            orig = svc.alerts.assess

            def assess(res):
                a = orig(res)
                if res["asset"] in forced and res.get("premove") is not None:
                    a = dict(a, strict=True, watch=True, checks={k: True for k in a["checks"]}, missing=[])
                return a
            svc.alerts.assess = assess

        async def wait_for(pred, timeout=15.0):
            for _ in range(int(timeout / 0.25)):
                if pred():
                    return True
                await asyncio.sleep(0.25)
            return False

        async def run1():
            svc = ScannerService(c)
            force(svc)
            await svc.start()
            try:
                await asyncio.sleep(3)
                target = svc.top_payload()["rows"][0]["asset"]
                forced.add(target)
                self.assertTrue(await wait_for(lambda: svc.radar.get("state") == "HIGH_CONVICTION"))
                self.assertEqual(svc.radar["primary"]["asset"], target)
                self.assertEqual(svc.top_payload()["radar"]["label"], "HIGH-CONVICTION BUY SETUP")
                self.assertIsNotNone(svc.coin_payload(target)["high_conviction_alert"])
                forced.discard(target)                  # confirmation fades -> invalidated after 2 s
                self.assertTrue(await wait_for(lambda: svc.alerts.get(target) is None))
                self.assertEqual(svc.radar["state"], "INVALIDATED")
                svc.db.flush()
                hist = await svc.history(target, 1)
                self.assertEqual(len(hist["alerts"]), 1)
                al = hist["alerts"][0]
                self.assertEqual(al["state"], "INVALIDATED")
                self.assertTrue(al["end_reason"])
                self.assertIsNotNone(al["fired_ts"])
                kinds = {e["event_type"] for e in hist["events"] if e["category"] == "ALERT"}
                self.assertEqual(kinds, {"high_conviction_buy_setup", "high_conviction_cleared"})
                ah = await svc.alert_history(1)
                self.assertEqual([a["id"] for a in ah["alerts"]], [al["id"]])
                forced.add(target)                      # fire again and stop while it is open
                self.assertTrue(await wait_for(lambda: svc.alerts.get(target) is not None))
                svc.db.flush()
                return target
            finally:
                forced.clear()
                await svc.stop()

        async def run2(target):
            svc = ScannerService(c)
            await svc.start()
            try:
                self.assertEqual(svc.status.get("alerts_closed_at_start"), 1)
                rows = (await svc.alert_history(1))["alerts"]
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(r["state"] == "INVALIDATED" and r["ended_ts"] for r in rows))
                self.assertIn("scanner restarted", rows[0]["end_reason"])
            finally:
                await svc.stop()

        target = asyncio.run(run1())
        asyncio.run(run2(target))

    def test_top_ordering_puts_late_below_early(self):
        async def go():
            svc = ScannerService(sim_cfg(self.d))
            base = {"subscores": {}, "returns": {}, "late": {}}
            svc.results = {
                "A": dict(base, asset="A", status="LATE", premove=25.0, rank=1),
                "B": dict(base, asset="B", status="EMERGING", premove=58.0, rank=50),
                "C": dict(base, asset="C", status="NORMAL", premove=12.0, rank=2),
                "D": dict(base, asset="D", status="WARMING", premove=None, rank=3),
                "E": dict(base, asset="E", status="MOVE IN PROGRESS", premove=40.0, rank=4),
            }
            order = [r["asset"] for r in svc.top_payload()["rows"]]
            self.assertEqual(order, ["B", "C", "E", "A", "D"])
            await svc.stop()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
