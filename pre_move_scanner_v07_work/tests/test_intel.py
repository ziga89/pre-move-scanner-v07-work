import asyncio
import unittest

from tests.helpers import cfg, scratch_dir
from server.intel.classify import ACC, BUY, DIST, SELL, SHIFT, UNKNOWN, classify_transfer
from server.intel.etherscan import EtherscanAuthError, EtherscanBudgetExceeded, EtherscanClient, EtherscanError
from server.intel.labels import Label, LabelRegistry
from server.intel.monitor import IntelMonitor
from server.intel.scores import compute_scores, entity_balances

NOW = 1_760_000_000.0
QNT = "0x4a220e6096b25eadb88358cb44068a3248254675"
A = {  # illustrative addresses (not real attributions)
    "binance": "0x" + "b1" * 20, "okx": "0x" + "0c" * 20, "coinbase_hot": "0x" + "c1" * 20,
    "coinbase_prime": "0x" + "c2" * 20, "kraken": "0x" + "4b" * 20, "wintermute": "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa",
    "whale1": "0x" + "a1" * 20, "whale2": "0x" + "a2" * 20, "whale3": "0x" + "a3" * 20, "pool": "0x" + "d1" * 20,
    "low": "0x" + "99" * 20, "anon": "0x" + "ee" * 20,
}


def registry():
    r = LabelRegistry()
    for key, ent, typ, conf in (("binance", "Binance", "CEX_HOT", "HIGH"), ("okx", "OKX", "CEX_HOT", "HIGH"),
                                ("coinbase_hot", "Coinbase", "CEX_HOT", "HIGH"),
                                ("coinbase_prime", "Coinbase", "CEX_CUSTODY", "HIGH"),
                                ("kraken", "Kraken", "CEX_HOT", "HIGH"), ("wintermute", "Wintermute", "MM", "MEDIUM"),
                                ("whale1", "Whale 1", "WHALE", "MEDIUM"), ("whale2", "Whale 2", "WHALE", "MEDIUM"),
                                ("whale3", "Whale 3", "WHALE", "MEDIUM"), ("pool", "Uniswap QNT/WETH", "DEX_POOL", "HIGH"),
                                ("low", "Maybe whale", "WHALE", "LOW")):
        r.add(Label("ethereum", A[key], ent, typ, conf, "test"))
    return r


def tx(frm, to, usd, reg, cls=None, ts=NOW - 600):
    fl, tl = reg.lookup("ethereum", A[frm]), reg.lookup("ethereum", A[to])
    c, conf, why = classify_transfer(fl, tl, A[frm], A[to])
    return {"asset": "QNT", "ts": ts, "from_addr": A[frm], "to_addr": A[to], "usd_value": usd, "amount": usd / 100,
            "from_type": fl.entity_type if fl else None, "to_type": tl.entity_type if tl else None,
            "classification": c, "class_confidence": conf, "explanation": why}


COVERED = {"covered": True, "addresses": 5, "polled": 5, "lagging": []}


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.r = registry()

    def c(self, a, b):
        return classify_transfer(self.r.lookup("ethereum", A[a]), self.r.lookup("ethereum", A[b]), A[a], A[b])[0]

    def test_matrix(self):
        self.assertEqual(self.c("coinbase_hot", "coinbase_prime"), SHIFT)   # NOT an institution buying
        self.assertEqual(self.c("binance", "okx"), SHIFT)
        self.assertEqual(self.c("binance", "wintermute"), SHIFT)
        self.assertEqual(self.c("wintermute", "kraken"), SHIFT)
        self.assertEqual(self.c("binance", "whale1"), ACC)
        self.assertEqual(self.c("whale1", "binance"), DIST)
        self.assertEqual(self.c("binance", "anon"), UNKNOWN)                 # CEX withdrawal != purchase
        self.assertEqual(self.c("binance", "low"), UNKNOWN)                  # LOW-confidence label ignored
        self.assertEqual(self.c("pool", "whale2"), BUY)
        self.assertEqual(self.c("whale2", "pool"), SELL)
        self.assertEqual(self.c("wintermute", "whale1"), UNKNOWN)            # possible OTC, not inferred
        self.assertEqual(self.c("whale1", "whale2"), SHIFT)
        why = classify_transfer(self.r.lookup("ethereum", A["binance"]), self.r.lookup("ethereum", A["whale1"]))[2]
        self.assertIn("not proof of a purchase", why)

    def test_labels_csv(self):
        d = scratch_dir("labels")
        p = d / "l.csv"
        p.write_text("# comment\nchain,address,entity,entity_type,confidence,source,notes\n"
                     f"ethereum,{A['whale1'].upper()},W,WHALE,high,me,\nethereum,0xabc,X,NOT_A_TYPE,HIGH,me,\n")
        r = LabelRegistry()
        r.load_csv(p)
        self.assertEqual(r.lookup("ethereum", A["whale1"]).confidence, "HIGH")
        self.assertTrue(r.errors)
        p.unlink()


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.r = registry()

    def test_null_semantics(self):
        s = compute_scores("QNT", [], {"covered": False, "reason": "no reliable labelled addresses"}, NOW, 5e6)
        for k in ("mm", "whale", "cex_flow", "scarcity"):
            self.assertIsNone(s[k])
        self.assertIsNone(compute_scores("QNT", [], COVERED, NOW, None)["whale"])

    def test_covered_but_quiet_is_zero_not_null(self):
        s = compute_scores("QNT", [], COVERED, NOW, 5e6)
        self.assertEqual(s["whale"], 0.0)
        self.assertEqual(s["cex_flow"], 0.0)

    def test_qnt_wintermute_routing(self):
        t = [tx("binance", "wintermute", 2.0e6, self.r), tx("wintermute", "okx", 1.9e6, self.r),
             tx("coinbase_hot", "wintermute", 1.0e6, self.r), tx("wintermute", "kraken", 1.05e6, self.r)]
        s = compute_scores("QNT", t, COVERED, NOW, 20e6)
        self.assertEqual(s["mm_direction"], "ROUTING")
        self.assertLess(s["mm"], 10)
        self.assertEqual(s["cex_flow_direction"], "NEUTRAL")   # all SHIFTs
        self.assertTrue(any("routing" in r for r in s["reasons"]))
        self.assertNotEqual(s["scarcity_direction"], "REAL_SUPPLY_DRAIN")

    def test_simultaneous_accumulation_and_distribution(self):
        t = [tx("binance", "whale1", 3.0e6, self.r), tx("whale2", "binance", 2.5e6, self.r)]
        s = compute_scores("QNT", t, COVERED, NOW, 20e6)
        self.assertEqual(s["whale_direction"], "MIXED")
        self.assertEqual(s["whale_wallets"], {"accumulating": 1, "distributing": 1})
        self.assertNotEqual(s["scarcity_direction"], "REAL_SUPPLY_DRAIN")

    def test_real_supply_drain_requires_thinning_for_full_score(self):
        t = [tx("binance", "whale1", 1.5e6, self.r), tx("okx", "whale2", 1.4e6, self.r),
             tx("kraken", "whale3", 1.2e6, self.r)]
        with_book = compute_scores("QNT", t, COVERED, NOW, 20e6, ask_thinning=0.5)
        no_book = compute_scores("QNT", t, COVERED, NOW, 20e6, ask_thinning=0.0)
        self.assertEqual(with_book["scarcity_direction"], "REAL_SUPPLY_DRAIN")
        self.assertGreater(with_book["scarcity"], no_book["scarcity"])
        self.assertEqual(with_book["whale_direction"], "ACCUMULATION")
        self.assertEqual(with_book["cex_flow_direction"], "OUTFLOW")

    def test_returned_tokens_are_not_a_drain(self):
        t = [tx("binance", "whale1", 3.0e6, self.r), tx("whale1", "okx", 2.0e6, self.r)]
        s = compute_scores("QNT", t, COVERED, NOW, 20e6, ask_thinning=0.6)
        self.assertNotEqual(s["scarcity_direction"], "REAL_SUPPLY_DRAIN")

    def test_partial_coverage_damps(self):
        t = [tx("binance", "whale1", 2e6, self.r)]
        full = compute_scores("QNT", t, COVERED, NOW, 20e6)
        part = compute_scores("QNT", t, dict(COVERED, lagging=[A["binance"]]), NOW, 20e6)
        self.assertLess(part["whale"], full["whale"])
        self.assertIn("partial", part["status"])


class FakeHttp:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get_json(self, url, params=None, headers=None):
        self.calls.append(params)
        return self.handler(params)


def row(frm, to, value_tokens, block, ts, contract=QNT, sym="QNT", h=None):
    return {"blockNumber": str(block), "timeStamp": str(int(ts)), "hash": h or f"0x{block:064x}", "from": frm, "to": to,
            "value": str(int(value_tokens * 10 ** 18)), "contractAddress": contract, "tokenSymbol": sym,
            "tokenDecimal": "18", "logIndex": "1"}


def icfg(**kw):
    c = dict(cfg()["intel"])
    c.update(enabled=True, tokens={"QNT": {"chain": "ethereum", "contract": QNT, "token_wide": "off"}},
             min_event_usd=100000, max_pages_per_poll=2, page_size=3, address_poll_min_seconds=0)
    c.update(kw)
    return c


class MonitorTest(unittest.TestCase):
    def mk(self, handler, **kw):
        import os
        os.environ["PMS_TEST_ETHERSCAN_KEY"] = "k"
        c = icfg(etherscan_api_key_env="PMS_TEST_ETHERSCAN_KEY", **kw)
        http = FakeHttp(handler)

        async def nosleep(s):
            return None
        client = EtherscanClient(http, c, clock=lambda: NOW, sleep=nosleep)
        events = []
        r = LabelRegistry()
        r.add(Label("ethereum", A["wintermute"], "Wintermute", "MM", "MEDIUM", "t"))
        r.add(Label("ethereum", A["binance"], "Binance", "CEX_HOT", "HIGH", "t"))
        mon = IntelMonitor(c, r, client, lambda a: 100.0, on_event=events.append, clock=lambda: NOW)
        return mon, http, events

    def test_address_centric_poll_classifies_and_emits(self):
        def h(p):
            if p.get("action") == "tokenbalance":
                return {"status": "1", "result": str(5000 * 10 ** 18)}
            if p.get("address") == A["binance"]:
                return {"status": "1", "result": [row(A["binance"], A["wintermute"], 2000, 100, NOW - 60),
                                                  row(A["binance"], A["anon"], 10, 101, NOW - 50, contract="0x" + "77" * 20, sym="OTHER")]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, http, events = self.mk(h)
        asyncio.run(mon.run_once())
        self.assertEqual(len(mon.transfers), 1)                       # untracked token ignored
        self.assertEqual(mon.transfers[0]["classification"], SHIFT)
        self.assertTrue(events and "SHIFT" in events[0]["message"])
        self.assertTrue(all(p.get("address") for p in http.calls if p.get("action") == "tokentx"))  # address-centric
        cov = mon.coverage("QNT")
        self.assertTrue(cov["covered"])
        asyncio.run(mon.run_once())  # second cycle: balances now possible (decimals learned)
        ents = entity_balances(mon.balances, mon.labels, QNT, NOW)
        self.assertTrue(ents and ents[0]["balance"] == 5000)

    def test_symbol_mismatch_invalidates_token(self):
        def h(p):
            if p.get("action") == "tokentx":
                return {"status": "1", "result": [row(A["binance"], A["anon"], 5, 10, NOW, sym="FAKE")]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, _, _ = self.mk(h)
        asyncio.run(mon.run_once())
        self.assertFalse(mon.coverage("QNT")["covered"])
        self.assertIn("symbol is FAKE", mon.coverage("QNT")["reason"])

    def test_lagging_address_marked(self):
        def h(p):
            if p.get("action") == "tokentx" and p.get("address") == A["binance"]:
                b = int(p["startblock"]) + 1
                return {"status": "1", "result": [row(A["binance"], A["anon"], 1, b + i, NOW, h=f"0x{b + i:064x}") for i in range(3)]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, _, _ = self.mk(h)
        asyncio.run(mon.run_once())
        self.assertIn(A["binance"], mon.coverage("QNT")["lagging"])

    def test_token_wide_auto_disables_when_too_active(self):
        def h(p):
            if p.get("contractaddress") and not p.get("address"):
                return {"status": "1", "result": [row(A["anon"], A["anon"], 1, 1000 + i, NOW - 3600 + i * 3,
                                                       h=f"0x{i:064x}") for i in range(600)]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, _, _ = self.mk(h, tokens={"QNT": {"chain": "ethereum", "contract": QNT, "token_wide": "auto"}},
                            token_wide_max_transfers_per_hour=400)
        asyncio.run(mon.run_once())
        t = mon.tokens["QNT"]
        self.assertEqual(t["token_wide_status"], "disabled")
        self.assertIn("too active", t["token_wide_reason"])

    def test_etherscan_errors_and_budget(self):
        mon, _, _ = self.mk(lambda p: {"status": "0", "message": "NOTOK", "result": "Invalid API Key"})
        with self.assertRaises(EtherscanAuthError):
            asyncio.run(mon.client.tokentx("ethereum", address=A["binance"]))
        mon, _, _ = self.mk(lambda p: {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})
        with self.assertRaises(EtherscanError):
            asyncio.run(mon.client.tokentx("ethereum", address=A["binance"]))
        mon, _, _ = self.mk(lambda p: {"status": "1", "result": []}, daily_call_budget=1)
        asyncio.run(mon.client.tokentx("ethereum", address=A["binance"]))
        with self.assertRaises(EtherscanBudgetExceeded):
            asyncio.run(mon.client.tokentx("ethereum", address=A["binance"]))
        with self.assertRaises(EtherscanError):
            asyncio.run(mon.client.tokentx("solana", address="x"))    # unsupported chain reported


if __name__ == "__main__":
    unittest.main()
