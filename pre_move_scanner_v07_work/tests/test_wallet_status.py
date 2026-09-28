"""v0.7.3 wallet intelligence: explicit states (OFF / NO KEY / WARMING / UNSUPPORTED / N/A / value),
coverage summary, provider registry, safe EVM contract discovery and whale candidates."""
import asyncio
import unittest

from server.intel.discovery import ContractDiscovery, decide
from server.intel.etherscan import EtherscanClient
from server.intel.labels import Label, LabelRegistry
from server.intel.monitor import IntelMonitor
from server.intel.providers import ProviderRegistry
from server.intel.scores import compute_scores
from server.intel.status import LABEL, asset_status, coverage_summary, gate_scores, status_list
from tests.helpers import cfg
from tests.test_intel import A, COVERED, registry, tx

NOW = 1_760_000_000.0
QNT = "0x4a220e6096b25eadb88358cb44068a3248254675"
LINK = "0x514910771af9ca656af840dff83e8264ecf986ca"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


class FakeProvider:
    name = "fake"

    def __init__(self, chains=("ethereum",), keyed=True):
        self.chains = frozenset(chains)
        self.keyed = keyed
        self.daily = 1000
        self.calls = 0

    def supports(self, chain):
        return chain in self.chains

    async def tokentx(self, chain, address=None, contract=None, startblock=0, page=1, offset=1000):
        self.calls += 1
        return []

    async def tokenbalance(self, chain, contract, address):
        return None

    def remaining_today(self):
        return self.daily - self.calls

    def stats(self):
        return {"keyed": self.keyed, "used_today": self.calls, "daily_budget": self.daily}


def labels(*types):
    r = LabelRegistry()
    for i, t in enumerate(types):
        r.add(Label("ethereum", "0x" + f"{i + 1:02x}" * 20, f"Entity {i}", t, "HIGH", "test"))
    return r


def monitor(lab=None, tokens=None, chains=("ethereum",), clock=lambda: NOW):
    icfg = cfg(intel={"enabled": True})["intel"]
    icfg["tokens"] = tokens if tokens is not None else {"QNT": {"chain": "ethereum", "contract": QNT}}
    return IntelMonitor(icfg, lab or labels(), ProviderRegistry([FakeProvider(chains)]), lambda a: 1.0, clock=clock)


def poll_all(m):
    async def go():
        for lab in m.labels.monitored("ethereum"):
            await m.poll_address("ethereum", lab)
    asyncio.run(go())


class DetailCG:
    def __init__(self, details, fail=()):
        self.details = details
        self.fail = set(fail)
        self.calls = []

    async def coin_detail(self, cid):
        self.calls.append(cid)
        if cid in self.fail:
            raise ConnectionError("429 Too Many Requests")
        return self.details[cid]


DETAILS = {
    "quant-network": {"symbol": "qnt", "name": "Quant", "asset_platform_id": "ethereum",
                      "platforms": {"ethereum": QNT.upper().replace("0X", "0x"), "binance-smart-chain": "0x" + "11" * 20},
                      "detail_platforms": {"ethereum": {"decimal_place": 18, "contract_address": QNT}}},
    "bitcoin": {"symbol": "btc", "name": "Bitcoin", "asset_platform_id": None, "platforms": {"": ""}},
    "ethereum": {"symbol": "eth", "name": "Ethereum", "asset_platform_id": None, "platforms": {"": ""}},
    "binancecoin": {"symbol": "bnb", "name": "BNB", "asset_platform_id": None, "platforms": {}},
    "jupiter-exchange-solana": {"symbol": "jup", "name": "Jupiter", "asset_platform_id": "solana",
                                "platforms": {"solana": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}},
    "tron-bridged": {"symbol": "btt", "name": "BitTorrent", "asset_platform_id": "tron",
                     "platforms": {"tron": "TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4", "ethereum": "0x" + "22" * 20}},
    "polygon-token": {"symbol": "pol", "name": "Polygon token", "asset_platform_id": "polygon-pos",
                      "platforms": {"polygon-pos": "0x" + "33" * 20}},
    "broken": {"symbol": "brk", "name": "Broken", "asset_platform_id": "ethereum", "platforms": {"ethereum": "0x1234"}},
    "chainlink": {"symbol": "link", "name": "Chainlink", "asset_platform_id": "ethereum", "platforms": {"ethereum": LINK}},
}


class StatusStateTests(unittest.TestCase):
    def check_all(self, st, state):
        self.assertEqual(st["state"], state)
        self.assertEqual(st["label"], LABEL[state])
        for k in ("mm", "whale", "cex_flow", "scarcity"):
            self.assertEqual(st["scores"][k]["state"], state, k)
            self.assertIsNone(st["scores"][k]["value"])

    def test_off(self):
        st = asset_status("QNT", enabled=False, keyed=True, monitor=monitor())
        self.check_all(st, "OFF")
        self.assertIn("intel.enabled", st["reason"])

    def test_no_key(self):
        st = asset_status("QNT", enabled=True, keyed=False, monitor=monitor())
        self.check_all(st, "NO_KEY")
        self.assertEqual(st["label"], "NO KEY")
        self.assertIn("ETHERSCAN_API_KEY", st["reason"])

    def test_no_contract_without_discovery_is_na(self):
        st = asset_status("XYZ", enabled=True, keyed=True, monitor=monitor())
        self.check_all(st, "NA")
        self.assertEqual(st["label"], "N/A")

    def test_discovery_states(self):
        d = ContractDiscovery(DetailCG(DETAILS), cfg()["intel"], lambda c: c == "ethereum", clock=lambda: NOW)
        m = monitor()
        self.check_all(asset_status("BTC", enabled=True, keyed=True, monitor=m, discovery=d), "WARMING")  # lookup pending
        d.results["BTC"] = decide("BTC", "bitcoin", DETAILS["bitcoin"], lambda c: True, NOW)
        st = asset_status("BTC", enabled=True, keyed=True, monitor=m, discovery=d)
        self.check_all(st, "UNSUPPORTED")
        self.assertIn("native coin of its own chain", st["reason"])
        d.results["BRK"] = decide("BRK", "broken", DETAILS["broken"], lambda c: True, NOW)
        self.check_all(asset_status("BRK", enabled=True, keyed=True, monitor=m, discovery=d), "NA")
        d.results["ERR"] = {"asset": "ERR", "state": "error", "reason": "lookup failed: ConnectionError"}
        st = asset_status("ERR", enabled=True, keyed=True, monitor=m, discovery=d)
        self.check_all(st, "WARMING")
        self.assertIn("retried", st["reason"])

    def test_token_on_unsupported_chain(self):
        m = monitor(tokens={"QNT": {"chain": "tron", "contract": QNT}})
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels)
        self.check_all(st, "UNSUPPORTED")
        self.assertTrue(m.coverage("QNT").get("unsupported"))

    def test_rejected_contract_is_na(self):
        m = monitor(labels("CEX_HOT"))
        m._classify_rows([{"contractAddress": QNT, "tokenSymbol": "FAKE", "tokenDecimal": "18", "hash": "0x1",
                           "logIndex": "1", "value": "1", "from": "0x" + "01" * 20, "to": "0x" + "99" * 20,
                           "timeStamp": str(int(NOW)), "blockNumber": "1"}], "ethereum", "address")
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels)
        self.check_all(st, "NA")
        self.assertIn("contract symbol is FAKE", st["reason"])

    def test_no_labels_is_na(self):
        for tw in ("off", "auto"):      # token-wide polling alone cannot attribute anything
            m = monitor(LabelRegistry(), tokens={"QNT": {"chain": "ethereum", "contract": QNT, "token_wide": tw}})
            st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels)
            self.check_all(st, "NA")
            self.assertIn("labelled", st["reason"])

    def test_first_polls_then_history_window_warming(self):
        clock = [NOW]
        m = monitor(labels("CEX_HOT", "WHALE"), clock=lambda: clock[0])
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels, now=NOW)
        self.check_all(st, "WARMING")
        self.assertIn("first polls pending", st["reason"])
        poll_all(m)
        self.assertEqual(m.first_ok_ts["ethereum"], NOW)
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels, now=NOW + 600,
                          warm_seconds=3600)
        self.check_all(st, "WARMING")
        self.assertIn("collecting transfer history (10/60 min)", st["reason"])

    def test_values_and_per_score_na(self):
        m = monitor(labels("CEX_HOT", "WHALE"))
        poll_all(m)
        scores = {"cex_flow": 0.0, "cex_flow_direction": "NEUTRAL", "whale": 42.0, "whale_direction": "ACCUMULATION",
                  "scarcity": 0.0, "scarcity_direction": "NEUTRAL", "mm": 0.0, "mm_direction": "NEUTRAL",
                  "status": "ok"}
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels, scores=scores,
                          now=NOW + 7200, warm_seconds=3600)
        self.assertEqual(st["state"], "OK")
        self.assertEqual(st["label"], "ON")
        self.assertEqual(st["scores"]["whale"]["value"], 42.0)
        self.assertEqual(st["scores"]["cex_flow"]["state"], "OK")
        self.assertEqual(st["scores"]["cex_flow"]["value"], 0.0)          # covered and quiet: a real 0
        self.assertEqual(st["scores"]["mm"]["state"], "NA")                 # no labelled market makers
        self.assertIn("market-maker", st["scores"]["mm"]["reason"])
        g = gate_scores(scores, st)
        self.assertIsNone(g["mm"])
        self.assertIsNone(g["mm_direction"])
        self.assertEqual(g["whale"], 42.0)
        self.assertEqual(g["cex_flow"], 0.0)
        self.assertIsNone(gate_scores(None, st))

    def test_no_usd_reference_is_na(self):
        m = monitor(labels("CEX_HOT", "WHALE", "MM"))
        poll_all(m)
        na = compute_scores("QNT", [], m.coverage("QNT"), NOW, None)
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels, scores=na,
                          now=NOW + 7200, warm_seconds=3600)
        self.check_all(st, "NA")
        self.assertIn("USD", st["reason"])
        g = gate_scores(dict(na, mm=5.0, mm_direction="NEUTRAL"), st)
        self.assertIsNone(g["mm"])                                          # a non-OK value never leaks through

    def test_lagging_coverage_is_partial(self):
        m = monitor(labels("CEX_HOT", "WHALE"))
        poll_all(m)
        m.addr_state[("ethereum", m.labels.monitored("ethereum")[0].address)]["lagging"] = True
        scores = {k: 0.0 for k in ("cex_flow", "whale", "scarcity", "mm")}
        st = asset_status("QNT", enabled=True, keyed=True, monitor=m, labels=m.labels, scores=scores,
                          now=NOW + 7200, warm_seconds=3600)
        self.assertEqual(st["label"], "ON (partial)")


class CoverageSummaryTests(unittest.TestCase):
    def test_states_and_text(self):
        self.assertEqual(coverage_summary({}, enabled=False, keyed=False)["text"], "Wallet intel OFF")
        s = coverage_summary({}, enabled=True, keyed=False)
        self.assertEqual((s["state"], s["label"]), ("NO_KEY", "NO KEY"))
        m = monitor(labels("CEX_HOT", "WHALE"))
        m.add_token("UNI", "ethereum", UNI)
        poll_all(m)
        d = ContractDiscovery(DetailCG(DETAILS), cfg()["intel"], m.supports, clock=lambda: NOW)
        statuses = {"QNT": {"state": "OK"}, "LINK": {"state": "WARMING"}, "BTC": {"state": "UNSUPPORTED"},
                    "XYZ": {"state": "NA"}}
        s = coverage_summary(statuses, enabled=True, keyed=True, monitor=m, discovery=d, labels=m.labels,
                             chains=["ethereum", "bsc"])
        self.assertEqual(s["state"], "OK")
        self.assertEqual(s["text"], "Wallet intel ON · 1/4 assets with data · 1 warming · 1 unsupported chains")
        self.assertEqual(s["by_state"], {"OK": 1, "WARMING": 1, "UNSUPPORTED": 1, "NA": 1})
        self.assertEqual(s["tokens"], {"config": 1, "discovered": 1})
        self.assertEqual(s["labelled_addresses"], {"ethereum": {"CEX_HOT": 1, "WHALE": 1}})
        self.assertEqual(s["last_poll"], NOW)
        self.assertEqual(s["discovery"]["known"], 0)
        self.assertEqual(status_list(statuses, ["UNSUPPORTED", "NA"]), ["BTC", "XYZ"])
        s = coverage_summary({"A": {"state": "WARMING"}}, enabled=True, keyed=True)
        self.assertEqual(s["state"], "WARMING")


class DiscoveryDecideTests(unittest.TestCase):
    def d(self, asset, cid, supports=lambda c: c == "ethereum"):
        return decide(asset, cid, DETAILS[cid], supports, NOW)

    def test_native_evm_token_is_supported(self):
        r = self.d("QNT", "quant-network")
        self.assertEqual((r["state"], r["chain"], r["contract"], r["decimals"]), ("supported", "ethereum", QNT, 18))
        self.assertIn("on-chain symbol check pending", r["reason"])

    def test_native_coins_are_unsupported(self):
        self.assertIn("native coin of its own chain (Bitcoin)", self.d("BTC", "bitcoin")["reason"])
        r = self.d("ETH", "ethereum")
        self.assertEqual(r["state"], "unsupported")
        self.assertIn("native gas coin of Ethereum", r["reason"])
        self.assertIn("BNB Smart Chain", self.d("BNB", "binancecoin")["reason"])

    def test_non_evm_and_bridged_copies_are_unsupported(self):
        r = self.d("JUP", "jupiter-exchange-solana")
        self.assertEqual(r["state"], "unsupported")
        self.assertIn("solana", r["reason"])
        r = self.d("BTT", "tron-bridged")        # an ERC-20 bridged copy exists, but the token lives on TRON
        self.assertEqual(r["state"], "unsupported")
        self.assertIsNone(r["contract"])

    def test_evm_chain_without_provider(self):
        r = self.d("POL", "polygon-token")
        self.assertEqual((r["state"], r["chain"]), ("unsupported", "polygon"))
        self.assertEqual(self.d("POL", "polygon-token", supports=lambda c: True)["state"], "supported")

    def test_invalid_address_and_symbol_mismatch(self):
        self.assertEqual(self.d("BRK", "broken")["state"], "not_found")
        r = decide("QNTX", "quant-network", DETAILS["quant-network"], lambda c: True, NOW)
        self.assertEqual(r["state"], "not_found")
        self.assertIn("does not match", r["reason"])


class ContractDiscoveryTests(unittest.TestCase):
    def test_paced_cached_and_configured_skipped(self):
        clock = [NOW]
        cg = DetailCG(DETAILS)
        d = ContractDiscovery(cg, cfg()["intel"], lambda c: c == "ethereum", clock=lambda: clock[0])
        assets = [("QNT", "quant-network"), ("BTC", "bitcoin"), ("LINK", "chainlink")]
        new = asyncio.run(d.run_once(assets, configured=["LINK"], max_calls=1))
        self.assertEqual([r["asset"] for r in new], ["QNT"])
        self.assertEqual(d.stats()["pending"], 2)
        asyncio.run(d.run_once(assets, configured=["LINK"], max_calls=5))
        self.assertEqual(cg.calls, ["quant-network", "bitcoin"])           # LINK is configured: never looked up
        self.assertEqual(asyncio.run(d.run_once(assets, configured=["LINK"])), [])
        clock[0] += 31 * 86400                                              # TTL expired: re-checked
        self.assertEqual(len(asyncio.run(d.run_once(assets, configured=["LINK"], max_calls=5))), 2)
        cache = dict(d.results)
        d2 = ContractDiscovery(cg, cfg()["intel"], lambda c: True, cache=cache, clock=lambda: clock[0])
        self.assertEqual(d2.queue(assets, ["LINK"]), [])                     # cache from SQLite is honoured
        self.assertEqual(d2.queue([("QNT", "other-id")]), [("QNT", "other-id")])   # coin id changed

    def test_errors_retry_after_an_hour(self):
        clock = [NOW]
        cg = DetailCG(DETAILS, fail={"quant-network"})
        d = ContractDiscovery(cg, cfg()["intel"], lambda c: True, clock=lambda: clock[0])
        r = asyncio.run(d.run_once([("QNT", "quant-network")]))[0]
        self.assertEqual(r["state"], "error")
        self.assertEqual(d.stats()["errors"], 1)
        clock[0] += 1800
        self.assertEqual(asyncio.run(d.run_once([("QNT", "quant-network")])), [])
        cg.fail.clear()
        clock[0] += 1900
        self.assertEqual(asyncio.run(d.run_once([("QNT", "quant-network")]))[0]["state"], "supported")


class ProviderAndMonitorTests(unittest.TestCase):
    def test_registry(self):
        reg = ProviderRegistry([FakeProvider(("ethereum", "bsc"), keyed=False), FakeProvider(("tron",), keyed=True)])
        self.assertTrue(reg.supports("bsc"))
        self.assertFalse(reg.supports("solana"))
        self.assertEqual(reg.supported_chains(), ["bsc", "ethereum", "tron"])
        self.assertTrue(reg.keyed())
        self.assertFalse(reg.keyed("ethereum"))
        self.assertEqual(reg.remaining_today(), 2000)
        with self.assertRaises(LookupError):
            asyncio.run(reg.tokentx("solana", address="x"))
        self.assertEqual(asyncio.run(reg.tokentx("tron", address="x")), [])
        self.assertEqual(ProviderRegistry([]).supported_chains(), [])

    def test_etherscan_provider_interface(self):
        es = EtherscanClient(None, cfg()["intel"])
        self.assertEqual(es.name, "etherscan")
        self.assertTrue(es.supports("ethereum") and es.supports("base"))
        self.assertFalse(es.supports("tron"))
        self.assertIn("used_today", es.stats())

    def test_add_token_configured_wins(self):
        m = monitor()
        self.assertFalse(m.add_token("QNT", "ethereum", "0x" + "44" * 20))
        self.assertEqual(m.tokens["QNT"]["contract"], QNT)
        self.assertEqual(m.tokens["QNT"]["source"], "config")
        self.assertFalse(m.add_token("QNTX", "ethereum", QNT.upper().replace("0X", "0x")))   # same contract
        self.assertTrue(m.add_token("uni", "ethereum", UNI.upper().replace("0X", "0x"), 18))
        t = m.tokens["UNI"]
        self.assertEqual((t["source"], t["token_wide"], t["status"], t["decimals"]), ("discovered", "off", "pending", 18))
        self.assertEqual(m.asset_for("ethereum", UNI), "UNI")


class WhaleCandidateTests(unittest.TestCase):
    def test_unlabelled_large_wallet_is_candidate_and_never_counted(self):
        reg = registry()
        rows = [dict(tx("binance", "anon", 900_000, reg), from_entity="Binance", to_entity=None)]
        s = compute_scores("QNT", rows, COVERED, 1_760_000_000.0, 10_000_000)
        self.assertEqual(s["whale"], 0.0)                                   # no identity, no accumulation
        self.assertNotEqual(s["whale_direction"], "ACCUMULATION")
        self.assertEqual(s["cex_outflow_attributed_share"], 0.0)
        wc = s["whale_candidates"]
        self.assertEqual(len(wc), 1)
        self.assertEqual(wc[0]["label"], "UNKNOWN / WHALE CANDIDATE")
        self.assertEqual(wc[0]["address"], A["anon"])
        self.assertEqual(wc[0]["side"], "received from exchange")
        self.assertEqual(wc[0]["counterparty"], "Binance")          # the labelled side is named, the wallet is not

    def test_attributed_share(self):
        reg = registry()
        rows = [tx("binance", "anon", 500_000, reg), tx("binance", "whale1", 500_000, reg),
                tx("binance", "okx", 5_000_000, reg)]                       # CEX->CEX shift is excluded
        s = compute_scores("QNT", rows, COVERED, 1_760_000_000.0, 10_000_000)
        self.assertEqual(s["cex_outflow_attributed_share"], 0.5)
        self.assertEqual(len(s["whale_candidates"]), 1)
        small = compute_scores("QNT", [tx("binance", "anon", 1000, reg)], COVERED, 1_760_000_000.0, 10_000_000)
        self.assertEqual(small["whale_candidates"], [])                    # below whale_candidate_usd

    def test_uncovered_scores_are_none(self):
        s = compute_scores("QNT", [], {"covered": False, "reason": "x"}, NOW, 1e6)
        self.assertIsNone(s["cex_outflow_attributed_share"])
        self.assertEqual(s["whale_candidates"], [])


if __name__ == "__main__":
    unittest.main()
