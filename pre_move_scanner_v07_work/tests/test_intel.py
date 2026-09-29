"""Wallet intelligence: classification into the v0.8 event types, labels, scores (null semantics,
routing, accumulation / distribution, supply drain) and the address-centric EVM monitor path.

v0.7 -> v0.8 changes pinned here on purpose (specification sections 8 / 9):
* class names are the normalised event types (CEX_IN / CEX_OUT / ACCUMULATION / DISTRIBUTION /
  INTERNAL_SHIFT / MM_ROUTING / CUSTODY_SHIFT / BRIDGE / DEX_FLOW / UNKNOWN_TRANSFER);
* exchange -> institutional custody is a CUSTODY_SHIFT (v0.7 counted it as accumulation-side);
* holder <-> holder is UNKNOWN_TRANSFER (possible OTC), not a reserve shift.
"""
import asyncio
import os
import unittest

from tests.helpers import cfg, scratch_dir
from server.intel.classify import (ACCUMULATION, BRIDGE, CEX_IN, CEX_OUT, CUSTODY_SHIFT, DEX_FLOW, DISTRIBUTION,
                                   INTERNAL_SHIFT, MM_ROUTING, UNKNOWN_TRANSFER, classify, classify_transfer)
from server.intel.etherscan import EtherscanAuthError, EtherscanBudgetExceeded, EtherscanClient, EtherscanError
from server.intel.labels import Label, LabelRegistry, entity_type
from server.intel.monitor import WalletMonitor
from server.intel.providers import ProviderRegistry, provider_config
from server.intel.scores import compute_scores, entity_balances

NOW = 1_760_000_000.0
QNT = "0x4a220e6096b25eadb88358cb44068a3248254675"
A = {  # illustrative addresses (not real attributions)
    "binance": "0x" + "b1" * 20, "binance_cold": "0x" + "b2" * 20, "okx": "0x" + "0c" * 20,
    "coinbase_hot": "0x" + "c1" * 20, "coinbase_prime": "0x" + "c2" * 20, "kraken": "0x" + "4b" * 20,
    "wintermute": "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa", "custodian": "0x" + "cc" * 20,
    "whale1": "0x" + "a1" * 20, "whale2": "0x" + "a2" * 20, "whale3": "0x" + "a3" * 20, "pool": "0x" + "d1" * 20,
    "bridge": "0x" + "be" * 20, "treasury": "0x" + "7e" * 20, "low": "0x" + "99" * 20, "anon": "0x" + "ee" * 20,
    "anon2": "0x" + "ef" * 20, "candidate": "0x" + "ca" * 20, "zero": "0x" + "00" * 20,
}


def registry():
    r = LabelRegistry()
    for key, ent, typ, conf in (("binance", "Binance", "CEX_HOT", "HIGH"), ("binance_cold", "Binance", "CEX_COLD", "HIGH"),
                                ("okx", "OKX", "CEX_HOT", "HIGH"), ("coinbase_hot", "Coinbase", "CEX_HOT", "HIGH"),
                                ("coinbase_prime", "Coinbase", "CEX_CUSTODY", "HIGH"),
                                ("kraken", "Kraken", "CEX_HOT", "HIGH"), ("wintermute", "Wintermute", "MM", "MEDIUM"),
                                ("custodian", "Anchorage", "CUSTODY_INSTITUTIONAL", "HIGH"),
                                ("whale1", "Whale 1", "WHALE", "MEDIUM"), ("whale2", "Whale 2", "WHALE", "MEDIUM"),
                                ("whale3", "Whale 3", "WHALE", "MEDIUM"), ("pool", "Uniswap QNT/WETH", "DEX_POOL", "HIGH"),
                                ("bridge", "Some bridge", "BRIDGE", "HIGH"), ("treasury", "QNT treasury", "PROTOCOL_TREASURY", "HIGH"),
                                ("low", "Maybe whale", "WHALE", "LOW"), ("candidate", "big unknown", "WHALE_CANDIDATE", "HIGH")):
        r.add(Label("ethereum", A[key], ent, typ, conf, "test"))
    return r


def tx(frm, to, usd, reg, ts=NOW - 600):
    fl, tl = reg.lookup("ethereum", A[frm]), reg.lookup("ethereum", A[to])
    c, conf, why = classify_transfer(fl, tl, A[frm], A[to])
    return {"asset": "QNT", "ts": ts, "from_addr": A[frm], "to_addr": A[to], "usd_value": usd, "amount": usd / 100,
            "from_type": fl.entity_type if fl and fl.reliable else None, "to_type": tl.entity_type if tl and tl.reliable else None,
            "from_entity": fl.entity if fl else None, "to_entity": tl.entity if tl else None,
            "classification": c, "class_confidence": conf, "explanation": why}


COVERED = {"covered": True, "addresses": 5, "polled": 5, "lagging": []}


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        self.r = registry()

    def c(self, a, b):
        return classify_transfer(self.r.lookup("ethereum", A[a]), self.r.lookup("ethereum", A[b]), A[a], A[b])[0]

    def test_matrix(self):
        self.assertEqual(self.c("coinbase_hot", "coinbase_prime"), CUSTODY_SHIFT)   # NOT an institution buying
        self.assertEqual(self.c("binance", "custodian"), CUSTODY_SHIFT)             # custody transfer, not buying
        self.assertEqual(self.c("binance", "binance_cold"), INTERNAL_SHIFT)         # same entity
        self.assertEqual(self.c("binance", "okx"), INTERNAL_SHIFT)                  # exchange <-> exchange routing
        self.assertEqual(self.c("binance", "wintermute"), MM_ROUTING)
        self.assertEqual(self.c("wintermute", "kraken"), MM_ROUTING)
        self.assertEqual(self.c("binance", "whale1"), ACCUMULATION)
        self.assertEqual(self.c("binance", "treasury"), ACCUMULATION)
        self.assertEqual(self.c("whale1", "binance"), DISTRIBUTION)
        self.assertEqual(self.c("binance", "anon"), CEX_OUT)                        # CEX withdrawal != purchase
        self.assertEqual(self.c("anon", "binance"), CEX_IN)                         # CEX deposit
        self.assertEqual(self.c("binance", "low"), CEX_OUT)                         # LOW-confidence label ignored
        self.assertEqual(self.c("binance", "candidate"), CEX_OUT)                   # a candidate is never an identity
        self.assertEqual(self.c("pool", "whale2"), DEX_FLOW)
        self.assertEqual(self.c("whale2", "pool"), DEX_FLOW)
        self.assertEqual(self.c("bridge", "anon"), BRIDGE)
        self.assertEqual(self.c("wintermute", "whale1"), UNKNOWN_TRANSFER)          # possible OTC, not inferred
        self.assertEqual(self.c("whale1", "whale2"), UNKNOWN_TRANSFER)
        self.assertEqual(self.c("anon", "anon2"), UNKNOWN_TRANSFER)
        self.assertEqual(self.c("zero", "binance"), UNKNOWN_TRANSFER)               # mint
        why = classify_transfer(self.r.lookup("ethereum", A["binance"]), self.r.lookup("ethereum", A["whale1"]))[2]
        self.assertIn("not proof of a purchase", why)

    def test_attribution_and_direction(self):
        lk = lambda k: self.r.lookup("ethereum", A[k])    # noqa: E731
        c = classify(lk("binance"), lk("okx"), A["binance"], A["okx"])
        self.assertEqual((c.attribution, c.confidence, c.entity_type), ("HIGH", "HIGH", "CEX_HOT"))
        c = classify(lk("binance"), None, A["binance"], A["anon"])
        self.assertEqual((c.event_type, c.attribution, c.entity_type), (CEX_OUT, "LOW", "CEX_HOT"))
        c = classify(lk("pool"), lk("whale2"), A["pool"], A["whale2"])
        self.assertEqual(c.direction, "into_token")
        c = classify(None, None, A["anon"], A["anon2"])
        self.assertEqual((c.attribution, c.entity_type), ("NONE", "UNKNOWN"))

    def test_taxonomy_aliases(self):
        self.assertEqual(entity_type("MM"), "MARKET_MAKER")
        self.assertEqual(entity_type("protocol_treasury"), "TREASURY")
        self.assertEqual(self.r.lookup("ethereum", A["wintermute"]).entity_type, "MARKET_MAKER")
        self.assertFalse(self.r.lookup("ethereum", A["candidate"]).reliable)

    def test_labels_csv(self):
        d = scratch_dir("labels")
        p = d / "l.csv"
        p.write_text("# comment\nchain,address,entity,entity_type,confidence,source,notes\n"
                     f"ethereum,{A['whale1'].upper().replace('0X', '0x')},W,WHALE,high,me,\n"
                     "ethereum,0xabc,X,NOT_A_TYPE,HIGH,me,\n"
                     "bitcoin,1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa,Some holder,WHALE,MEDIUM,me,\n"
                     "bitcoin,1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb,Bad checksum,WHALE,MEDIUM,me,\n"
                     "xdc,xdc" + "ab" * 20 + ",XDC exchange,CEX_HOT,HIGH,me,\n")
        r = LabelRegistry()
        r.load_csv(p)
        self.assertEqual(r.lookup("ethereum", A["whale1"]).confidence, "HIGH")
        # base58 is case-sensitive: kept exactly; an invalid checksum is rejected
        self.assertEqual(r.lookup("bitcoin", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa").entity, "Some holder")
        self.assertIsNone(r.lookup("bitcoin", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb"))
        self.assertEqual(r.lookup("xdc", "0x" + "ab" * 20).entity, "XDC exchange")       # xdc... == 0x...
        self.assertEqual(len(r.errors), 2)
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
        self.assertEqual(s["cex_flow_direction"], "NEUTRAL")   # all MM routing, excluded from reserves
        self.assertTrue(any("routing" in r for r in s["reasons"]))
        self.assertNotEqual(s["scarcity_direction"], "REAL_SUPPLY_DRAIN")
        self.assertEqual(s["windows"]["24h"]["by_event"][MM_ROUTING]["n"], 4)

    def test_internal_and_custody_moves_never_count(self):
        t = [tx("binance", "binance_cold", 5.0e6, self.r), tx("coinbase_hot", "coinbase_prime", 4.0e6, self.r),
             tx("binance", "custodian", 3.0e6, self.r), tx("binance", "okx", 2.0e6, self.r)]
        s = compute_scores("QNT", t, COVERED, NOW, 20e6)
        self.assertEqual((s["cex_flow"], s["whale"]), (0.0, 0.0))
        self.assertEqual(s["cex_flow_direction"], "NEUTRAL")
        self.assertNotEqual(s["whale_direction"], "ACCUMULATION")
        self.assertEqual(s["scarcity_direction"], "RESHUFFLING")

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

    def test_distribution_is_inflow(self):
        t = [tx("whale1", "binance", 1.5e6, self.r), tx("anon", "okx", 1.0e6, self.r)]
        s = compute_scores("QNT", t, COVERED, NOW, 20e6)
        self.assertEqual(s["whale_direction"], "DISTRIBUTION")
        self.assertEqual(s["cex_flow_direction"], "INFLOW")

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


async def nosleep(s):
    return None


class MonitorTest(unittest.TestCase):
    def mk(self, handler, **kw):
        os.environ["PMS_TEST_ETHERSCAN_KEY"] = "k"
        c = icfg(etherscan_api_key_env="PMS_TEST_ETHERSCAN_KEY", **kw)
        http = FakeHttp(handler)
        client = EtherscanClient(http, provider_config(c, "evm"), clock=lambda: NOW, sleep=nosleep)
        events = []
        r = LabelRegistry()
        r.add(Label("ethereum", A["wintermute"], "Wintermute", "MM", "MEDIUM", "t"))
        r.add(Label("ethereum", A["binance"], "Binance", "CEX_HOT", "HIGH", "t"))
        mon = WalletMonitor(c, r, ProviderRegistry([client]), lambda a: 100.0, on_event=events.append, clock=lambda: NOW)
        mon.client = client
        return mon, http, events

    def test_address_centric_poll_classifies_and_emits(self):
        def h(p):
            if p.get("action") == "tokenbalance":
                return {"status": "1", "result": str(5000 * 10 ** 18)}
            if p.get("address") == A["binance"] and p.get("action") == "tokentx":
                return {"status": "1", "result": [row(A["binance"], A["wintermute"], 2000, 100, NOW - 60),
                                                  row(A["binance"], A["anon"], 10, 101, NOW - 50, contract="0x" + "77" * 20, sym="OTHER")]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, http, events = self.mk(h)
        asyncio.run(mon.run_once())
        self.assertEqual(len(mon.transfers), 1)                       # untracked token ignored, replay de-duplicated
        ev = mon.transfers[0]
        self.assertEqual(ev["classification"], MM_ROUTING)
        self.assertEqual((ev["event_type"], ev["attribution_confidence"], ev["entity_type"]), (MM_ROUTING, "MEDIUM", "MARKET_MAKER"))
        self.assertEqual(ev["usd_value"], 200000.0)
        self.assertTrue(events and "MM_ROUTING" in events[0]["message"])
        self.assertTrue(all(p.get("address") for p in http.calls if p.get("action") == "tokentx"))  # address-centric
        self.assertTrue(any(p.get("sort") == "desc" for p in http.calls))                           # seeded from newest
        cov = mon.coverage("QNT")
        self.assertTrue(cov["covered"])
        self.assertEqual(mon.assets["QNT"].status, "ok")                                           # symbol verified
        asyncio.run(mon.run_once())
        ents = entity_balances(mon.balances, mon.labels, QNT, NOW)
        self.assertTrue(ents and ents[0]["balance"] == 5000)

    def test_symbol_mismatch_invalidates_token(self):
        def h(p):
            if p.get("action") == "tokentx":
                return {"status": "1", "result": [row(A["binance"], A["anon"], 5, 10, NOW, sym="FAKE")]}
            return {"status": "0", "message": "No transactions found", "result": []}
        mon, _, _ = self.mk(h)
        mon.assets["QNT"].status = "pending"
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
        self.assertEqual(mon.client.used_today(), 1)                   # the refused call was never sent
        with self.assertRaises(EtherscanError):
            asyncio.run(mon.client.tokentx("solana", address="x"))    # unsupported chain reported

    def test_native_eth_transfers_from_txlist(self):
        def h(p):
            if p.get("action") == "txlist":
                return {"status": "1", "result": [
                    {"blockNumber": "50", "timeStamp": str(int(NOW - 30)), "hash": "0xaa", "from": A["binance"],
                     "to": A["anon"], "value": str(3 * 10 ** 18), "isError": "0", "txreceipt_status": "1"},
                    {"blockNumber": "51", "timeStamp": str(int(NOW - 20)), "hash": "0xbb", "from": A["binance"],
                     "to": A["anon"], "value": str(9 * 10 ** 18), "isError": "1"},            # failed: ignored
                    {"blockNumber": "52", "timeStamp": str(int(NOW - 10)), "hash": "0xcc", "from": A["binance"],
                     "to": A["anon"], "value": "0", "isError": "0"}]}                          # no value: ignored
            return {"status": "0", "message": "No transactions found", "result": []}
        from server.intel.model import TrackedAsset
        mon, _, _ = self.mk(h, tokens={})
        mon.track(TrackedAsset(asset="ETH", chain="ethereum", native=True, source="discovered", status="ok"))
        asyncio.run(mon.run_once())
        self.assertEqual([(t["asset"], t["amount"], t["token"], t["classification"]) for t in mon.transfers],
                         [("ETH", 3.0, "native", CEX_OUT)])
        self.assertIn("contract-internal", mon.coverage("ETH")["note"])


if __name__ == "__main__":
    unittest.main()
