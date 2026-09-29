"""v0.8 manual assets: Top-N ∪ manual = one deduplicated universe, added / removed at runtime without a
restart, persistent across restarts, never pinned to the top of the ranking, and never resolved from a
ticker by guessing."""
import asyncio
import copy
import shutil
import unittest

from server.service import ManualAssetError, ScannerService
from server.storage.db import Database
from server.universe.filters import classify_exclusion
from server.universe.manual import ManualAssets
from server.universe.universe import UniverseManager
from server.universe.venues import AssetInfo, VenueSelector
from tests.helpers import cfg, scratch_dir
from tests.test_universe import NOW, load


def sim_cfg(d, **kw):
    return cfg(mode="sim", feeds={"backend": "sim", "resync_grace_seconds": 2},
               sim={"assets": 5, "venues_per_asset": 3, "seed": 5, "scenarios": False},
               universe={"target_size": 3}, storage={"path": str(d / "manual.db")}, **kw)


async def wait_for(pred, timeout=15.0):
    for _ in range(int(timeout / 0.25)):
        if pred():
            return True
        await asyncio.sleep(0.25)
    return False


class ManualAssetServiceTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("manual_svc")
        for p in self.d.glob("*"):
            p.unlink()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_add_duplicate_remove_and_persist_across_restart(self):
        c = sim_cfg(self.d)

        async def run1():
            svc = ScannerService(c)
            await svc.start()
            try:
                self.assertEqual(sorted(svc.info), ["ALPHA", "BRAVO", "CHARLIE"])       # Top-3 (SIM)
                # a ticker alone is never resolved: candidates come back, nothing is added
                with self.assertRaises(ManualAssetError) as cm:
                    await svc.add_manual_asset(query="echo")
                self.assertEqual(cm.exception.status, 409)
                self.assertEqual([x["coingecko_id"] for x in cm.exception.payload["candidates"]], ["echo"])
                with self.assertRaises(ManualAssetError) as cm:
                    await svc.add_manual_asset(coingecko_id="does-not-exist")
                self.assertEqual(cm.exception.status, 404)
                # outside the Top-3: joins the universe at runtime (no restart), feeds start
                res = await svc.add_manual_asset(coingecko_id="echo")
                self.assertEqual(res["status"], "added")
                self.assertIn("monitoring started", res["integration"])
                self.assertEqual(sorted(svc.info), ["ALPHA", "BRAVO", "CHARLIE", "ECHO"])  # 3 + 1 = 4 unique
                self.assertTrue(svc.info["ECHO"].manual and not svc.info["ECHO"].in_top)
                self.assertEqual(svc.status["markets"], 12)          # 4 assets x 3 SIM venues subscribed
                self.assertTrue(await wait_for(lambda: "ECHO" in svc.results))
                row = next(r for r in svc.top_payload()["rows"] if r["asset"] == "ECHO")
                self.assertTrue(row["manual"])
                # already a Top-3 member: marked manual, not duplicated
                res = await svc.add_manual_asset(coingecko_id="alpha")
                self.assertIn("Top-100 member", res["integration"])
                self.assertEqual(len(svc.info), 4)
                self.assertEqual(svc.status["markets"], 12)          # no duplicate subscription
                self.assertEqual(len({r["asset"] for r in svc.top_payload()["rows"]}), len(svc.top_payload()["rows"]))
                self.assertEqual((await svc.add_manual_asset(coingecko_id="echo"))["status"], "already")
                man = {m["symbol"]: m for m in svc.universe_payload()["manual"]}
                self.assertEqual(set(man), {"ALPHA", "ECHO"})
                self.assertTrue(man["ALPHA"]["in_top"])
                self.assertFalse(man["ECHO"]["in_top"])
                u = svc.universe_payload()
                self.assertEqual(u["counts"], {"top": 3, "manual": 2, "manual_outside_top": 1, "monitored": 4})
                self.assertIsNotNone(svc.asset_payload("ECHO"))
                await asyncio.sleep(6)                      # a few 5 s rows for ECHO
                svc.db.flush()
            finally:
                await svc.stop()

        async def run2():
            svc = ScannerService(c)
            await svc.start()
            try:
                # persisted: still manual and monitored after a restart
                self.assertIn("ECHO", svc.info)
                self.assertTrue(svc.info["ECHO"].manual)
                self.assertTrue(svc.info["ALPHA"].manual)
                # removing a manual-only asset stops monitoring but keeps history
                res = await svc.remove_manual_asset("ECHO")
                self.assertIn("monitoring stopped", res["result"])
                self.assertNotIn("ECHO", svc.info)
                self.assertNotIn("ECHO", {r["asset"] for r in svc.top_payload()["rows"]})
                n = svc.db.read_sync(lambda con: con.execute(
                    "SELECT COUNT(*) FROM asset_metrics_5s WHERE asset='ECHO'").fetchone()[0])
                self.assertGreater(n, 0)
                # removing a Top-3 member's manual flag keeps it monitored
                res = await svc.remove_manual_asset("ALPHA")
                self.assertIn("still monitored", res["result"])
                self.assertIn("ALPHA", svc.info)
                self.assertFalse(svc.info["ALPHA"].manual)
                with self.assertRaises(ManualAssetError):
                    await svc.remove_manual_asset("ECHO")
            finally:
                await svc.stop()

        async def run3():
            svc = ScannerService(c)
            await svc.start()
            try:
                self.assertEqual(sorted(svc.info), ["ALPHA", "BRAVO", "CHARLIE"])     # removal persisted
                self.assertEqual(svc.manual.active(), [])
                self.assertEqual(svc.manual.rows["ECHO"]["note"], "removed (history kept)")
            finally:
                await svc.stop()

        asyncio.run(run1())
        asyncio.run(run2())
        asyncio.run(run3())

    def test_manual_flag_never_changes_ranking(self):
        async def go():
            svc = ScannerService(sim_cfg(self.d))
            base = {"subscores": {}, "returns": {}, "late": {}, "status": "NORMAL"}
            svc.results = {
                "RAIL": dict(base, asset="RAIL", premove=91.0, rank=480, manual=True),
                "BTC": dict(base, asset="BTC", premove=40.0, rank=1, manual=False),
                "QNT": dict(base, asset="QNT", premove=15.0, rank=90, manual=True),
                "ETH": dict(base, asset="ETH", premove=15.0, rank=2, manual=False),
            }
            order = [r["asset"] for r in svc.top_payload()["rows"]]
            self.assertEqual(order, ["RAIL", "BTC", "ETH", "QNT"])      # score first; ties by market cap, not manual
            svc.results["RAIL"]["premove"] = 5.0
            self.assertEqual([r["asset"] for r in svc.top_payload()["rows"]][-1], "RAIL")
            for flag in (True, False):                                 # flipping the flag changes nothing
                for r in svc.results.values():
                    r["manual"] = flag
                self.assertEqual([r["asset"] for r in svc.top_payload()["rows"]], ["BTC", "ETH", "QNT", "RAIL"])
            await svc.stop()
        asyncio.run(go())


class FakeCG:
    """CoinGecko /search, /coins/markets and coin detail for the live code paths (no network)."""
    COINS = [
        {"id": "railgun", "symbol": "rail", "name": "Railgun", "market_cap_rank": 480},
        {"id": "rail-other", "symbol": "rail", "name": "Rail Other", "market_cap_rank": 2400},
        {"id": "railroad", "symbol": "rrd", "name": "Railroad", "market_cap_rank": None},
    ]
    DETAIL = {"railgun": {"symbol": "rail", "name": "Railgun", "asset_platform_id": "ethereum",
                          "platforms": {"ethereum": "0xe76c6c83af64e4c60245d8c7de953df673a7a33d"},
                          "detail_platforms": {"ethereum": {"decimal_place": 18}}}}

    def __init__(self):
        self.calls = []

    async def search(self, q):
        self.calls.append(("search", q))
        return [dict(c) for c in self.COINS if q.lower() in (c["symbol"], c["id"]) or q.lower() in c["name"].lower()]

    async def markets(self, page=1, per_page=250, category=None, ids=None):
        self.calls.append(("markets", tuple(ids or ())))
        return [{"id": c["id"], "symbol": c["symbol"], "name": c["name"], "market_cap_rank": c["market_cap_rank"],
                 "current_price": 1.2, "total_volume": 5e6, "market_cap": 1e8} for c in self.COINS if c["id"] in (ids or [])]

    async def coin_detail(self, cid):
        self.calls.append(("detail", cid))
        return self.DETAIL[cid]


class SearchResolveTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("manual_search")
        for p in self.d.glob("*"):
            p.unlink()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_ambiguous_ticker_resolve_and_add_without_venue(self):
        from server.intel.registry import AssetRegistry

        async def go():
            svc = ScannerService(sim_cfg(self.d))
            try:
                svc.sim = False                           # exercise the live CoinGecko code paths
                svc.cg = FakeCG()
                svc.registry = AssetRegistry(svc.cg, svc.cfg["assets"], svc.providers, clock=svc.clock)
                res = await svc.search_assets("rail")
                ids = [c["coingecko_id"] for c in res["candidates"]]
                self.assertEqual(ids[:2], ["railgun", "rail-other"])   # exact ticker matches first, by rank
                self.assertTrue(res["ambiguous"])
                self.assertEqual(res["exact_ticker_matches"], 2)
                with self.assertRaises(ManualAssetError) as cm:
                    await svc.add_manual_asset(query="RAIL")
                self.assertEqual(cm.exception.status, 409)            # never guessed
                self.assertEqual(svc.manual.active(), [])
                with self.assertRaises(ManualAssetError) as cm:
                    await svc.add_manual_asset(query="nothing-like-this")
                self.assertEqual(cm.exception.status, 404)
                prev = await svc.resolve_asset("railgun")
                m = prev["candidate"]["metadata"]
                self.assertEqual((m["state"], m["native_chain"], m["native_asset"], m["wallet_provider"]),
                                 ("READY", "ethereum", 0, "etherscan"))
                self.assertEqual(m["contract_address"], "0xe76c6c83af64e4c60245d8c7de953df673a7a33d")
                self.assertTrue(prev["wallet"]["supported"])
                res = await svc.add_manual_asset(coingecko_id="railgun")
                self.assertEqual(res["status"], "added")
                self.assertIn("no usable realtime venue", res["integration"])   # kept, re-checked each refresh
                self.assertEqual([r["symbol"] for r in svc.manual.active()], ["RAIL"])
                self.assertNotIn("RAIL", svc.info)
                entry = next(x for x in svc.manual_payload() if x["symbol"] == "RAIL")
                self.assertEqual((entry["state"], entry["coingecko_id"], entry["chain"]), ("NO VENUE", "railgun", "Ethereum"))
                self.assertEqual(svc.registry.get("RAIL")["manual"], 1)
                with self.assertRaises(ManualAssetError) as cm:
                    await svc.resolve_asset("unknown-id")
                self.assertEqual(cm.exception.status, 404)
            finally:
                svc.sim = True
                await svc.stop()
        asyncio.run(go())


class UniverseCompositionTest(unittest.TestCase):
    """Top-100 ∪ manual with the recorded CoinGecko / exchange fixtures."""

    def setUp(self):
        self.rows, self.cats, self.fx = load()
        self.c = cfg()
        self.vs = VenueSelector(self.c["discovery"])

    def usable(self, r):
        s = self.vs.preview(AssetInfo.from_row(r), self.cats, self.fx, NOW)
        if not s.selected:
            return False, "no usable realtime venue"
        if s.total_volume() < self.c["universe"]["min_usable_volume_usd"]:
            return False, "insufficient spot volume"
        return True, ""

    def outsiders(self, members, n):
        ids = {m["id"] for m in members}
        return [r for r in sorted(self.rows, key=lambda r: r["market_cap_rank"])
                if r["id"] not in ids and self.usable(r)[0] and classify_exclusion(r, {}, self.c["universe"]) is None
                and r["id"] != "fake-link-clone"][:n]

    def test_100_plus_manual_outside_is_one_unique_universe(self):
        """Top-100 = 100, k manual assets outside it -> 100 + k unique (the fixtures have k = 3 usable outsiders;
        the specification's example is 100 + 5 = 105)."""
        um = UniverseManager(self.c["universe"])
        probe = um.build(self.rows, {}, self.usable, now=NOW, manual=[])
        extra = self.outsiders(probe["members"], 5)
        k = len(extra)
        self.assertGreaterEqual(k, 3)
        top_sym = probe["members"][0]
        manual = [{"symbol": r["symbol"].upper(), "coingecko_id": r["id"]} for r in extra]
        manual.append({"symbol": top_sym["symbol"].upper(), "coingecko_id": top_sym["id"]})    # already Top-100
        u = UniverseManager(self.c["universe"]).build(self.rows, {}, self.usable, now=NOW, manual=manual)
        self.assertEqual(len(u["members"]), 100)
        outside = [m for m in u["manual"] if m["usable"] and not m["in_top"]]
        self.assertEqual(len(outside), k)
        monitored = {m["id"] for m in u["members"]} | {m["id"] for m in outside}
        self.assertEqual(len(monitored), 100 + k)
        dup = next(m for m in u["manual"] if m["id"] == top_sym["id"])
        self.assertTrue(dup["in_top"])
        self.assertIn("not duplicated", dup["status"])

    def test_ticker_resolution_never_guesses(self):
        rows = copy.deepcopy(self.rows)
        # two coins share a ticker outside the ranking's duplicate filter
        twin = dict(next(r for r in rows if r["symbol"].upper() == "QNT"), id="qnt-twin", market_cap_rank=9999)
        rows.append(twin)
        u = UniverseManager(self.c["universe"]).build(rows, {}, self.usable, now=NOW, manual=[
            {"symbol": "QNT", "coingecko_id": None, "source": "config"},          # ambiguous: 2 coins
            {"symbol": "NOPE", "coingecko_id": None, "source": "config"},         # not found
            {"symbol": "LINK", "coingecko_id": "does-not-exist", "source": "user"},  # user-added id vanished
            {"symbol": "XDC", "coingecko_id": "wrong-id", "source": "config"}])     # seeded: unique ticker
        by = {m["symbol"]: m for m in u["manual"]}
        self.assertIn("ambiguous ticker: 2 CoinGecko coins use QNT", by["QNT"]["status"])
        self.assertEqual({c["id"] for c in by["QNT"]["candidates"]}, {"quant-network", "qnt-twin"})
        self.assertIn("not found", by["NOPE"]["status"])
        self.assertIn("CoinGecko id 'does-not-exist' not found", by["LINK"]["status"])
        self.assertTrue(by["XDC"]["resolved_by_ticker"])
        self.assertFalse(by["QNT"]["usable"])

    def test_ticker_collision_with_a_top_member_is_reported(self):
        probe = UniverseManager(self.c["universe"]).build(self.rows, {}, self.usable, now=NOW, manual=[])
        m0 = probe["members"][0]
        clone = dict(m0, id="clone-coin", market_cap_rank=4000, name="Clone")
        u = UniverseManager(self.c["universe"]).build(self.rows, {}, self.usable, [clone], NOW,
                                                      manual=[{"symbol": m0["symbol"].upper(), "coingecko_id": "clone-coin"}])
        m = u["manual"][0]
        self.assertFalse(m["usable"])
        self.assertIn("already used by Top-100 member", m["status"])


class ManualStoreTest(unittest.TestCase):
    def test_seed_once_remove_readd(self):
        d = scratch_dir("manual_store")
        for p in d.glob("*"):
            p.unlink()
        db = Database(d / "m.db", cfg()["storage"])
        try:
            ids = {"QNT": "quant-network", "LINK": "chainlink", "XDC": "xdce-crowd-sale"}
            ms = ManualAssets(db, clock=lambda: NOW)
            self.assertEqual(ms.seed(["QNT", "LINK", "XDC"], ids), ["QNT", "LINK", "XDC"])
            self.assertTrue(ms.remove("QNT"))
            db.flush()
            ms2 = ManualAssets(db, clock=lambda: NOW + 10)                      # "restart"
            self.assertEqual(ms2.symbols(), ["LINK", "XDC"])
            self.assertEqual(ms2.seed(["QNT", "LINK", "XDC", "SOL"], ids), ["SOL"])   # QNT stays removed
            self.assertIsNone(ms2.get("SOL")["coingecko_id"])                    # resolved later, never guessed
            ms2.add("QNT", "quant-network", "Quant")
            self.assertEqual(ms2.get("QNT")["note"], "re-added")
            db.flush()
            self.assertEqual(sorted(ManualAssets(db).symbols()), ["LINK", "QNT", "SOL", "XDC"])
        finally:
            db.close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
