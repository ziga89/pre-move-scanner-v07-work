import asyncio
import copy
import json
import unittest

from tests.helpers import ROOT, cfg
from server.universe.catalog import Catalog
from server.universe.coingecko import CoinGeckoClient
from server.universe.filters import classify_exclusion
from server.universe.fx import FxService
from server.universe.http import HttpError
from server.universe.universe import UniverseManager
from server.universe.venues import AssetInfo, VenueSelector, crosscheck_unsupported

FIX = ROOT / "tests" / "fixtures"
NOW = 1_760_000_000.0


def load():
    rows = json.loads((FIX / "coingecko_markets.json").read_text())
    cats = [Catalog.from_dict(d) for d in json.loads((FIX / "exchange_catalogs.json").read_text())]
    fx = FxService()
    fx.update_from_markets(rows)
    fx.update_from_catalogs(cats)
    return rows, cats, fx


def row(rows, sym):
    return next(r for r in rows if r["symbol"] == sym.lower() and r["id"] != "fake-link-clone")


class VenueSelectionTest(unittest.TestCase):
    def setUp(self):
        self.rows, self.cats, self.fx = load()
        self.c = cfg()
        self.vs = VenueSelector(self.c["discovery"])

    def sel(self, sym):
        return self.vs.select(AssetInfo.from_row(row(self.rows, sym)), self.cats, self.fx, NOW)

    def test_xdc_never_gets_binance(self):
        s = self.sel("XDC")
        exs = [m["exchange"] for m in s.selected]
        self.assertNotIn("binance", exs)
        self.assertEqual(exs[0], "bitrue")          # XDC's own biggest venue
        self.assertIn("kucoin", exs)
        self.assertLessEqual(len(exs), 5)
        stale = [r for r in s.rejected if r["exchange"] == "htx"]
        self.assertTrue(any("stale" in r["reason"] for r in stale))

    def test_ranking_is_per_coin_volume(self):
        s = self.sel("QNT")
        vols = [m["volume_24h_usd"] for m in s.selected]
        self.assertEqual(vols, sorted(vols, reverse=True))
        self.assertEqual(len({m["exchange"] for m in s.selected}), len(s.selected))  # one pair per exchange

    def test_ticker_collision_rejected(self):
        s = self.sel("QNT")
        mexc = [r for r in s.rejected if r["exchange"] == "mexc" and r["symbol"] == "QNT/USDT"]
        self.assertTrue(mexc and "different token" in mexc[0]["reason"])
        self.assertNotIn(("mexc", "QNT/USDT"), [(m["exchange"], m["symbol"]) for m in s.selected])

    def test_wide_spread_rejected(self):
        s = self.sel("QNT")
        self.assertTrue(any(r["exchange"] == "gate" and "spread" in r["reason"] for r in s.rejected))

    def test_krw_and_fx(self):
        self.assertAlmostEqual(self.fx.rate("EUR"), 1.17, places=2)
        self.assertAlmostEqual(self.fx.rate("TRY"), 1 / 41.5, places=4)
        s = self.sel("XRP")
        up = [m for m in s.candidates if m["exchange"] == "upbit"]
        self.assertTrue(up, "KRW market within the KRW tolerance must be accepted")
        self.assertEqual(up[0]["quote"], "KRW")

    def test_unavailable_market_promotes_next(self):
        first = self.sel("XDC")
        top = first.selected[0]
        self.vs.mark_unavailable(top["exchange"], top["symbol"], "no order book within 90 s", now=NOW)
        again = self.sel("XDC")
        keys = [(m["exchange"], m["symbol"]) for m in again.selected]
        self.assertNotIn((top["exchange"], top["symbol"]), keys)
        self.assertEqual(len(keys), 5)  # next-best market promoted
        self.assertTrue(any("realtime unavailable" in r["reason"] for r in again.rejected))

    def test_replacement_hysteresis(self):
        vs = VenueSelector(dict(self.c["discovery"], max_venues_per_asset=2))
        a = AssetInfo.from_row(row(self.rows, "QNT"))
        first = vs.select(a, self.cats, self.fx, NOW)
        cur = [m["exchange"] for m in first.selected]
        # boost a non-selected exchange far above the weakest selected one
        cats = copy.deepcopy(self.cats)
        challenger = next(m for m in first.candidates if m["exchange"] not in cur)
        c = next(c for c in cats if c.exchange == challenger["exchange"])
        c.tickers[challenger["symbol"]]["quoteVolume"] *= 1000
        second = vs.select(a, cats, self.fx, NOW)
        self.assertEqual([m["exchange"] for m in second.selected].count(challenger["exchange"]), 0)
        third = vs.select(a, cats, self.fx, NOW)   # second consecutive win -> replaced
        self.assertIn(challenger["exchange"], [m["exchange"] for m in third.selected])

    def test_crosscheck_reports_unsupported(self):
        tickers = [{"market": {"identifier": "whitebit", "name": "WhiteBIT"}, "base": "XDC", "target": "USDT",
                    "converted_volume": {"usd": 9e6}},
                   {"market": {"identifier": "kucoin", "name": "KuCoin"}, "base": "XDC", "target": "USDT",
                    "converted_volume": {"usd": 5e6}}]
        out = crosscheck_unsupported(tickers, {"kucoin"})
        self.assertEqual([o["identifier"] for o in out], ["whitebit"])


class UniverseTest(unittest.TestCase):
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

    def test_exclusions(self):
        reasons = {r["symbol"].upper(): classify_exclusion(r, {}, self.c["universe"]) for r in self.rows}
        for s in ("USDT", "USDC", "USDE", "DAI", "USDS", "FDUSD", "USD1", "PYUSD", "BSC-USD"):
            self.assertIn("stablecoin", reasons[s], s)
        for s in ("WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC", "SUSDE"):
            self.assertIn("wrapped", reasons[s], s)
        for s in ("PAXG", "XAUT"):
            self.assertEqual(reasons[s], "tokenized gold")
        for s in ("BTC", "ETH", "QNT", "XDC", "LINK", "HYPE"):
            self.assertIsNone(reasons[s], s)

    def test_category_membership_wins(self):
        r = {"id": "some-new-lst", "symbol": "xyz", "name": "Plain Name"}
        self.assertIn("liquid-staking", classify_exclusion(r, {"liquid-staking-tokens": {"some-new-lst"}}, {}))

    def test_backfill_to_100_usable_and_pinned_extra(self):
        probe = UniverseManager(self.c["universe"]).build(self.rows, {}, self.usable, now=NOW)
        ids = {m["id"] for m in probe["members"]}
        outsider = next(r for r in sorted(self.rows, key=lambda r: r["market_cap_rank"])
                        if r["id"] not in ids and self.usable(r)[0]
                        and classify_exclusion(r, {}, self.c["universe"]) is None and r["id"] != "fake-link-clone")
        OUT = outsider["symbol"].upper()
        um = UniverseManager(dict(self.c["universe"], pinned_assets=["QNT", "XDC", "LINK", OUT]))
        u = um.build(self.rows, {}, self.usable, now=NOW)
        members = u["members"]
        self.assertEqual(len(members), 100)
        self.assertGreater(u["cutoff_rank"], 100)  # had to go past rank 100
        syms = {m["symbol"].upper() for m in members}
        self.assertFalse(syms & {"USDT", "USDC", "WBTC", "STETH", "PAXG", "LEO", "WBT"})
        excl = {e["symbol"]: e["reason"] for e in u["excluded"]}
        self.assertIn("no usable", excl["WBT"])
        self.assertIn("duplicate", [e for e in u["excluded"] if e["id"] == "fake-link-clone"][0]["reason"])
        pinned = {p["symbol"].upper() for p in u["pinned"]}
        self.assertIn(OUT, pinned)                   # outside the 100 -> monitored in addition
        self.assertNotIn("QNT", pinned)              # already one of the 100 -> not duplicated

    def test_membership_hysteresis(self):
        um = UniverseManager(self.c["universe"])
        u1 = um.build(self.rows, {}, self.usable, now=NOW)
        last_in = u1["members"][-1]
        first_out = next(r for r in sorted(self.rows, key=lambda r: r["market_cap_rank"])
                         if r["market_cap_rank"] > last_in["market_cap_rank"] and self.usable(r)[0]
                         and classify_exclusion(r, {}, self.c["universe"]) is None)
        rows2 = copy.deepcopy(self.rows)
        for r in rows2:  # swap ranks of the last member and the first outsider
            if r["id"] == last_in["id"]:
                r["market_cap_rank"] = first_out["market_cap_rank"]
            elif r["id"] == first_out["id"]:
                r["market_cap_rank"] = last_in["market_cap_rank"]
        u2 = um.build(rows2, {}, self.usable, now=NOW + 3600)
        ids2 = {m["id"] for m in u2["members"]}
        self.assertIn(last_in["id"], ids2)            # not dropped on a small rank change
        self.assertNotIn(first_out["id"], ids2)       # not admitted on the first refresh
        u3 = um.build(rows2, {}, self.usable, now=NOW + 7200)
        ids3 = {m["id"] for m in u3["members"]}
        self.assertIn(first_out["id"], ids3)          # admitted after 2 consecutive refreshes
        self.assertEqual(len(ids3), 100)


class CoinGeckoClientTest(unittest.TestCase):
    def test_backoff_on_429(self):
        class FakeHttp:
            def __init__(self):
                self.n = 0

            async def get_json(self, url, params=None, headers=None):
                self.n += 1
                if self.n == 1:
                    raise HttpError(429, "slow down", retry_after=30)
                return [{"id": "bitcoin"}]
        t = {"now": 0.0}
        slept = []

        async def sleep(s):
            slept.append(s)
            t["now"] += s
        cg = CoinGeckoClient(FakeHttp(), cfg()["universe"], clock=lambda: t["now"], sleep=sleep)
        out = asyncio.run(cg.markets())
        self.assertEqual(out, [{"id": "bitcoin"}])
        self.assertEqual(cg.rate_limited, 1)
        self.assertGreaterEqual(sum(slept), 30)

    def test_token_bucket_limits_rate(self):
        class FakeHttp:
            async def get_json(self, url, params=None, headers=None):
                return []
        t = {"now": 0.0}

        async def sleep(s):
            t["now"] += s
        c = dict(cfg()["universe"], coingecko_calls_per_minute=6)
        cg = CoinGeckoClient(FakeHttp(), c, clock=lambda: t["now"], sleep=sleep)

        async def many():
            for _ in range(18):
                await cg.markets()
        asyncio.run(many())
        self.assertGreaterEqual(t["now"], 115)  # 18 calls at 6/min with a 6-token burst -> >= 2 minutes


if __name__ == "__main__":
    unittest.main()
