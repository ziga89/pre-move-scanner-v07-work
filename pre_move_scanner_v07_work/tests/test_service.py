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
