"""v0.8 wallet intelligence: explicit states (OFF / NO KEY / DISCOVERING / WARMING / ACTIVE / UNSUPPORTED /
DEGRADED / N/A), coverage summary, provider registry, the asset registry (automatic chain / platform /
contract discovery, native coins, overrides, caching) and whale candidates.

v0.7.3 -> v0.8 on purpose: native coins (BTC, ETH, BNB, XRP, ...) and TRON / Solana tokens are no longer
UNSUPPORTED - they have providers now; only chains without a provider are, with the reason
"provider not implemented". "OK / ON" is now ACTIVE, and a pending lookup is DISCOVERING (was WARMING).
"""
import asyncio
import json
import os
import shutil
import unittest

from server.intel.labels import Label, LabelRegistry
from server.intel.model import TrackedAsset
from server.intel.monitor import WalletMonitor
from server.intel.providers import EvmProvider, ProviderRegistry, build_providers
from server.intel.registry import (ERROR, NEEDS_VERIFICATION, NOT_FOUND, READY, REGISTRY_COLS, UNSUPPORTED,
                                   AssetRegistry, decide, override_decision)
from server.intel.scores import compute_scores
from server.intel.status import LABEL, asset_status, coverage_summary, gate_scores, status_list
from server.storage.db import Database
from server.storage.history import load_registry, registry_row
from tests.helpers import cfg, scratch_dir
from tests.test_intel import A, COVERED, registry, tx

NOW = 1_760_000_000.0
QNT = "0x4a220e6096b25eadb88358cb44068a3248254675"
LINK = "0x514910771af9ca656af840dff83e8264ecf986ca"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


class FakeProvider:
    name = "fake"
    label = "Fake"
    family = "evm"
    requires_key = False

    def __init__(self, chains=("ethereum",), keyed=True, state="ok", reason="ok"):
        self.chains = frozenset(chains)
        self.keyed = keyed
        self.enabled = True
        self.daily = 1000
        self.calls = 0
        self._state = {"state": state, "reason": reason}
        self.key_env = "FAKE_KEY"
        self.key = ""

    def supports(self, chain):
        return chain in self.chains

    def state(self, chain=None):
        return dict(self._state)

    async def fetch_address(self, chain, address, assets, cursor):
        from server.intel.model import FetchResult
        self.calls += 1
        return FetchResult(cursor={"n": self.calls})

    async def fetch_balance(self, chain, address, asset):
        return None

    def supports_token_wide(self):
        return False

    def remaining_today(self):
        return self.daily - self.calls

    def stats(self):
        return {"keyed": self.keyed, "used_today": self.calls, "daily_budget": self.daily}


def labels(*types):
    r = LabelRegistry()
    for i, t in enumerate(types):
        r.add(Label("ethereum", "0x" + f"{i + 1:02x}" * 20, f"Entity {i}", t, "HIGH", "test"))
    return r


def monitor(lab=None, tokens=None, chains=("ethereum",), clock=lambda: NOW, provider=None):
    icfg = cfg(intel={"enabled": True})["intel"]
    icfg["tokens"] = tokens if tokens is not None else {"QNT": {"chain": "ethereum", "contract": QNT}}
    reg = ProviderRegistry([provider or FakeProvider(chains)])
    m = WalletMonitor(icfg, lab or labels(), reg, lambda a: 1.0, clock=clock)
    m.reg = reg
    return m


def st(asset, m, **kw):
    return asset_status(asset, enabled=True, monitor=m, providers=m.reg, labels=m.labels, **kw)


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
    "ripple": {"symbol": "xrp", "name": "XRP", "asset_platform_id": None, "platforms": {}},
    "xdce-crowd-sale": {"symbol": "xdc", "name": "XDC Network", "asset_platform_id": None, "platforms": {"": ""}},
    "sui": {"symbol": "sui", "name": "Sui", "asset_platform_id": None, "platforms": {"": ""}},
    "kaspa-like": {"symbol": "kas", "name": "Kaspa-like", "asset_platform_id": None, "platforms": {}},
    "jupiter-exchange-solana": {"symbol": "jup", "name": "Jupiter", "asset_platform_id": "solana",
                                "platforms": {"solana": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"}},
    "tron-bridged": {"symbol": "btt", "name": "BitTorrent", "asset_platform_id": "tron",
                     "platforms": {"tron": "TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4", "ethereum": "0x" + "22" * 20}},
    "polygon-token": {"symbol": "pol", "name": "Polygon token", "asset_platform_id": "polygon-pos",
                      "platforms": {"polygon-pos": "0x" + "33" * 20}},
    "osmo-token": {"symbol": "osm", "name": "Osmo token", "asset_platform_id": "osmosis",
                   "platforms": {"osmosis": "ibc/ABC"}},
    "multi-no-native": {"symbol": "mlt", "name": "Multi", "asset_platform_id": None,
                        "platforms": {"ethereum": "0x" + "44" * 20, "binance-smart-chain": "0x" + "55" * 20}},
    "broken": {"symbol": "brk", "name": "Broken", "asset_platform_id": "ethereum", "platforms": {"ethereum": "0x1234"}},
    "chainlink": {"symbol": "link", "name": "Chainlink", "asset_platform_id": "ethereum", "platforms": {"ethereum": LINK}},
}


def providers():
    return build_providers(cfg(intel={"enabled": True})["intel"], None, {})


class StatusStateTests(unittest.TestCase):
    def check_all(self, s, state):
        self.assertEqual(s["state"], state)
        self.assertEqual(s["label"], LABEL[state])
        for k in ("mm", "whale", "cex_flow", "scarcity"):
            self.assertEqual(s["scores"][k]["state"], state, k)
            self.assertIsNone(s["scores"][k]["value"])

    def test_off(self):
        s = asset_status("QNT", enabled=False, monitor=monitor())
        self.check_all(s, "OFF")
        self.assertIn("intel.enabled", s["reason"])

    def test_no_key(self):
        os.environ.pop("PMS_TEST_NOKEY", None)
        prov = EvmProvider(None, {"api_key_env": "PMS_TEST_NOKEY"})
        m = monitor(provider=prov)
        s = st("QNT", m)
        self.check_all(s, "NO_KEY")
        self.assertEqual(s["label"], "NO KEY")
        self.assertIn("PMS_TEST_NOKEY", s["reason"])

    def test_discovering_unsupported_and_na_from_the_registry(self):
        reg = AssetRegistry(DetailCG(DETAILS), cfg()["assets"], providers(), clock=lambda: NOW)
        m = monitor()
        s = asset_status("BTC", enabled=True, monitor=m, registry=reg, providers=m.reg)
        self.check_all(s, "DISCOVERING")                                    # nothing known yet
        reg.note_member("BTC", "bitcoin", "Bitcoin", 1, False)
        self.check_all(asset_status("BTC", enabled=True, monitor=m, registry=reg, providers=m.reg), "DISCOVERING")
        asyncio.run(reg.run_once([("SUI", "sui"), ("BRK", "broken"), ("MLT", "multi-no-native")], max_calls=5))
        s = asset_status("SUI", enabled=True, monitor=m, registry=reg, providers=m.reg)
        self.check_all(s, "UNSUPPORTED")
        self.assertEqual(s["reason"], "native coin of Sui · provider not implemented")
        self.assertIn("provider not implemented", s["short"])               # never a bare UNSUPPORTED
        self.check_all(asset_status("BRK", enabled=True, monitor=m, registry=reg, providers=m.reg), "NA")
        s = asset_status("MLT", enabled=True, monitor=m, registry=reg, providers=m.reg)
        self.check_all(s, "NA")
        self.assertIn("override", s["reason"])
        reg.entries["ERR"] = {"symbol": "ERR", "state": ERROR, "reason": "metadata lookup failed (ConnectionError)"}
        self.check_all(asset_status("ERR", enabled=True, monitor=m, registry=reg, providers=m.reg), "DISCOVERING")

    def test_chain_without_provider_is_unsupported_with_reason(self):
        m = monitor(tokens={"QNT": {"chain": "sui", "contract": "0x" + "11" * 32}})
        m.reg = ProviderRegistry([FakeProvider(("ethereum",))])
        s = st("QNT", m)
        self.check_all(s, "UNSUPPORTED")
        self.assertIn("provider not implemented", s["reason"])

    def test_degraded_provider(self):
        m = monitor(provider=FakeProvider(state="degraded", reason="Esplora: rate-limited (HTTP 429)"))
        s = st("QNT", m)
        self.check_all(s, "DEGRADED")
        self.assertIn("rate-limited", s["reason"])
        # a real provider: budget exhausted / chain not on the API plan -> DEGRADED with the reason
        os.environ["PMS_TEST_KEY"] = "k"
        prov = EvmProvider(None, {"api_key_env": "PMS_TEST_KEY", "daily_call_budget": 0})
        m = monitor(provider=prov)
        self.assertEqual(st("QNT", m)["state"], "DEGRADED")
        prov = EvmProvider(None, {"api_key_env": "PMS_TEST_KEY"})
        prov.chain_errors["ethereum"] = "Etherscan (ethereum): Free API access is not supported for this chain"
        m = monitor(provider=prov)
        s = st("QNT", m)
        self.assertEqual(s["state"], "DEGRADED")
        self.assertIn("not supported for this chain", s["reason"])

    def test_rejected_contract_is_na(self):
        m = monitor(labels("CEX_HOT"))
        from server.intel.model import RawTransfer
        m.ingest([RawTransfer("ethereum", "QNT", QNT, "0x1", 1, NOW, "0x" + "01" * 20, "0x" + "99" * 20, 1.0,
                              symbol="FAKE", decimals=18)])
        s = st("QNT", m)
        self.check_all(s, "NA")
        self.assertIn("contract symbol is FAKE", s["reason"])

    def test_no_labels_is_na(self):
        for tw in ("off", "auto"):      # token-wide polling alone cannot attribute anything
            m = monitor(LabelRegistry(), tokens={"QNT": {"chain": "ethereum", "contract": QNT, "token_wide": tw}})
            s = st("QNT", m)
            self.check_all(s, "NA")
            self.assertIn("labelled", s["reason"])

    def test_first_polls_then_history_window_warming(self):
        clock = [NOW]
        m = monitor(labels("CEX_HOT", "WHALE"), clock=lambda: clock[0])
        s = st("QNT", m, now=NOW)
        self.check_all(s, "WARMING")
        self.assertIn("first polls pending", s["reason"])
        poll_all(m)
        self.assertEqual(m.first_ok_ts["ethereum"], NOW)
        s = st("QNT", m, now=NOW + 600, warm_seconds=3600)
        self.check_all(s, "WARMING")
        self.assertIn("collecting transfer history (10/60 min)", s["reason"])

    def test_active_values_zero_is_not_na(self):
        m = monitor(labels("CEX_HOT", "WHALE"))
        poll_all(m)
        scores = {"cex_flow": 0.0, "cex_flow_direction": "NEUTRAL", "whale": 42.0, "whale_direction": "ACCUMULATION",
                  "scarcity": 0.0, "scarcity_direction": "NEUTRAL", "mm": 0.0, "mm_direction": "NEUTRAL",
                  "status": "ok"}
        s = st("QNT", m, scores=scores, now=NOW + 7200, warm_seconds=3600)
        self.assertEqual((s["state"], s["label"]), ("ACTIVE", "ACTIVE"))
        self.assertEqual(s["scores"]["whale"]["value"], 42.0)
        self.assertEqual(s["scores"]["cex_flow"]["state"], "ACTIVE")
        self.assertEqual(s["scores"]["cex_flow"]["value"], 0.0)          # covered and quiet: a real 0
        self.assertEqual(s["scores"]["mm"]["state"], "NA")                 # no labelled market makers
        self.assertIn("market-maker", s["scores"]["mm"]["reason"])
        g = gate_scores(scores, s)
        self.assertIsNone(g["mm"])
        self.assertIsNone(g["mm_direction"])
        self.assertEqual(g["whale"], 42.0)
        self.assertEqual(g["cex_flow"], 0.0)
        self.assertIsNone(gate_scores(None, s))

    def test_no_usd_reference_is_na(self):
        m = monitor(labels("CEX_HOT", "WHALE", "MM"))
        poll_all(m)
        na = compute_scores("QNT", [], m.coverage("QNT"), NOW, None)
        s = st("QNT", m, scores=na, now=NOW + 7200, warm_seconds=3600)
        self.check_all(s, "NA")
        self.assertIn("USD", s["reason"])
        g = gate_scores(dict(na, mm=5.0, mm_direction="NEUTRAL"), s)
        self.assertIsNone(g["mm"])                                          # a non-ACTIVE value never leaks through

    def test_lagging_coverage_is_partial(self):
        m = monitor(labels("CEX_HOT", "WHALE"))
        poll_all(m)
        m.addr_state[("ethereum", m.labels.monitored("ethereum")[0].address)]["lagging"] = True
        scores = {k: 0.0 for k in ("cex_flow", "whale", "scarcity", "mm")}
        s = st("QNT", m, scores=scores, now=NOW + 7200, warm_seconds=3600)
        self.assertEqual(s["label"], "ACTIVE (partial)")


class CoverageSummaryTests(unittest.TestCase):
    def test_states_and_text(self):
        self.assertEqual(coverage_summary({}, enabled=False)["text"], "Wallet intel OFF")
        s = coverage_summary({"A": {"state": "NO_KEY"}, "B": {"state": "NO_KEY"}}, enabled=True)
        self.assertEqual((s["state"], s["label"]), ("NO_KEY", "NO KEY"))
        m = monitor(labels("CEX_HOT", "WHALE"))
        m.add_token("UNI", "ethereum", UNI)
        poll_all(m)
        reg = AssetRegistry(DetailCG(DETAILS), cfg()["assets"], providers(), clock=lambda: NOW)
        statuses = {"QNT": {"state": "ACTIVE"}, "LINK": {"state": "WARMING"}, "BTC": {"state": "DISCOVERING"},
                    "SUI": {"state": "UNSUPPORTED"}, "XYZ": {"state": "NA"}, "SOL": {"state": "DEGRADED"}}
        s = coverage_summary(statuses, enabled=True, monitor=m, registry=reg, labels=m.labels,
                             chains=["ethereum", "bsc"])
        self.assertEqual(s["state"], "ACTIVE")
        self.assertEqual(s["text"], "Wallet intel · 1/6 active · 1 discovering · 1 warming · 1 degraded · "
                                    "1 unsupported · 1 N/A")
        self.assertEqual(s["tokens"], {"config": 1, "discovered": 1})
        self.assertEqual(s["labelled_addresses"], {"ethereum": {"CEX_HOT": 1, "WHALE": 1}})
        self.assertEqual(s["last_poll"], NOW)
        self.assertEqual(s["discovery"]["known"], 0)
        self.assertEqual(status_list(statuses, ["UNSUPPORTED", "NA"]), ["SUI", "XYZ"])
        self.assertEqual(coverage_summary({"A": {"state": "DISCOVERING"}}, enabled=True)["state"], "WARMING")
        self.assertEqual(coverage_summary({"A": {"state": "DEGRADED"}}, enabled=True)["state"], "DEGRADED")


class RegistryDecideTests(unittest.TestCase):
    def d(self, sym, cid):
        return decide(sym, cid, DETAILS[cid], providers(), NOW)

    def test_token_contract(self):
        r = self.d("QNT", "quant-network")
        self.assertEqual((r["state"], r["native_chain"], r["contract_address"], r["decimals"], r["native_asset"]),
                         (READY, "ethereum", QNT, 18, 0))
        self.assertEqual((r["wallet_provider"], r["wallet_supported"], r["discovery_confidence"], r["verified"]),
                         ("etherscan", 1, "high", 0))
        self.assertIn("on-chain symbol check pending", r["reason"])
        self.assertEqual(json.loads(r["platforms"])["binance-smart-chain"], "0x" + "11" * 20)   # listed, not tracked

    def test_native_assets(self):
        for sym, cid, chain, prov in (("BTC", "bitcoin", "bitcoin", "esplora"), ("ETH", "ethereum", "ethereum", "etherscan"),
                                      ("BNB", "binancecoin", "bsc", "etherscan"), ("XRP", "ripple", "xrpl", "xrpl"),
                                      ("XDC", "xdce-crowd-sale", "xdc", "etherscan")):
            r = self.d(sym, cid)
            self.assertEqual((r["state"], r["native_chain"], r["native_asset"], r["contract_address"], r["wallet_provider"]),
                             (READY, chain, 1, None, prov), sym)

    def test_non_evm_tokens_and_bridged_copies(self):
        r = self.d("JUP", "jupiter-exchange-solana")
        self.assertEqual((r["state"], r["native_chain"], r["wallet_provider"]), (READY, "solana", "solana_rpc"))
        r = self.d("BTT", "tron-bridged")          # native to TRON: the ERC-20 bridged copy is never tracked
        self.assertEqual((r["state"], r["native_chain"], r["contract_address"]),
                         (READY, "tron", "TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4"))
        self.assertEqual(self.d("POL", "polygon-token")["native_chain"], "polygon")

    def test_unsupported_needs_verification_and_not_found(self):
        r = self.d("SUI", "sui")
        self.assertEqual((r["state"], r["native_chain"]), (UNSUPPORTED, "sui"))
        self.assertIn("provider not implemented", r["reason"])
        r = self.d("KAS", "kaspa-like")
        self.assertEqual(r["state"], UNSUPPORTED)
        self.assertIn("native coin of its own chain (Kaspa-like) · provider not implemented", r["reason"])
        r = self.d("OSM", "osmo-token")
        self.assertIn("token on 'osmosis' · provider not implemented", r["reason"])
        r = self.d("MLT", "multi-no-native")      # ambiguous multi-platform: never guessed
        self.assertEqual((r["state"], r["contract_address"], r["discovery_confidence"]), (NEEDS_VERIFICATION, None, "low"))
        self.assertEqual(self.d("BRK", "broken")["state"], NOT_FOUND)
        r = decide("QNTX", "quant-network", DETAILS["quant-network"], providers(), NOW)
        self.assertEqual(r["state"], NOT_FOUND)
        self.assertIn("does not match", r["reason"])

    def test_overrides(self):
        p = providers()
        r = override_decision("MLT", {"chain": "ethereum", "contract": "0x" + "44" * 20, "decimals": 6}, p, NOW)
        self.assertEqual((r["state"], r["contract_address"], r["verified"], r["discovery_confidence"], r["decimals"]),
                         (READY, "0x" + "44" * 20, 1, "override", 6))
        r = override_decision("ABC", {"chain": "xdc", "native": True}, p, NOW)
        self.assertEqual((r["state"], r["native_asset"], r["native_chain"], r["decimals"]), (READY, 1, "xdc", 18))
        self.assertEqual(override_decision("DEF", {"unsupported": "exchange-only token"}, p, NOW)["state"], UNSUPPORTED)
        r = override_decision("BAD", {"chain": "tron", "contract": "0x" + "44" * 20}, p, NOW)   # EVM address on TRON
        self.assertEqual(r["state"], NOT_FOUND)
        r = override_decision("SUIX", {"chain": "sui", "native": True}, p, NOW)
        self.assertIn("provider not implemented", r["reason"])


class AssetRegistryTests(unittest.TestCase):
    def test_paced_cached_ttl_and_overrides_skipped(self):
        clock = [NOW]
        cg = DetailCG(DETAILS)
        reg = AssetRegistry(cg, cfg()["assets"], providers(), overrides={"LINK": {"chain": "ethereum", "contract": LINK}},
                            clock=lambda: clock[0])
        self.assertEqual(reg.get("LINK")["state"], READY)                      # override applied at start
        assets = [("QNT", "quant-network"), ("BTC", "bitcoin"), ("LINK", "chainlink")]
        new = asyncio.run(reg.run_once(assets, max_calls=1))
        self.assertEqual([r["symbol"] for r in new], ["QNT"])
        self.assertEqual(reg.stats()["pending"], 2)
        asyncio.run(reg.run_once(assets, max_calls=5))
        self.assertEqual(cg.calls, ["quant-network", "bitcoin"])              # LINK is overridden: never looked up
        self.assertEqual(asyncio.run(reg.run_once(assets)), [])               # cached
        clock[0] += 31 * 86400                                                  # TTL expired: re-checked
        self.assertEqual(len(asyncio.run(reg.run_once(assets, max_calls=5))), 2)
        cache = {k: dict(v) for k, v in reg.entries.items()}
        reg2 = AssetRegistry(cg, cfg()["assets"], providers(), cache=cache, clock=lambda: clock[0])
        self.assertEqual(reg2.queue(assets), [])                               # restart: no rediscovery
        reg2.note_member("QNT", "other-id", "Quant", 50, True)
        self.assertEqual(reg2.queue([("QNT", "other-id")]), [("QNT", "other-id")])   # coin id changed

    def test_errors_retry_after_an_hour_and_keep_good_decisions(self):
        clock = [NOW]
        cg = DetailCG(DETAILS, fail={"quant-network"})
        reg = AssetRegistry(cg, cfg()["assets"], providers(), clock=lambda: clock[0])
        r = asyncio.run(reg.run_once([("QNT", "quant-network")]))[0]
        self.assertEqual(r["state"], ERROR)
        clock[0] += 1800
        self.assertEqual(asyncio.run(reg.run_once([("QNT", "quant-network")])), [])
        cg.fail.clear()
        clock[0] += 1900
        self.assertEqual(asyncio.run(reg.run_once([("QNT", "quant-network")]))[0]["state"], READY)
        cg.fail.add("quant-network")                                            # a failed refresh keeps READY
        clock[0] += 31 * 86400
        asyncio.run(reg.run_once([("QNT", "quant-network")]))
        self.assertEqual(reg.get("QNT")["state"], READY)

    def test_tracked_assets_and_verification(self):
        reg = AssetRegistry(DetailCG(DETAILS), cfg()["assets"], providers(), clock=lambda: NOW)
        asyncio.run(reg.run_once([("QNT", "quant-network"), ("BTC", "bitcoin"), ("SUI", "sui")], max_calls=5))
        t = {x.asset: x for x in reg.tracked()}
        self.assertEqual(sorted(t), ["BTC", "QNT"])
        self.assertEqual((t["BTC"].native, t["BTC"].chain, t["BTC"].status), (True, "bitcoin", "ok"))
        self.assertEqual((t["QNT"].native, t["QNT"].token, t["QNT"].status), (False, QNT, "pending"))
        reg.mark_verified("QNT", True, "on-chain token symbol QNT verified")
        self.assertEqual(reg.public("QNT")["verified"], True)
        reg.mark_verified("QNT", False, "invalid: contract symbol is FAKE")
        self.assertEqual(reg.get("QNT")["state"], NOT_FOUND)

    def test_persistence_roundtrip(self):
        d = scratch_dir("registry_db")
        for p in d.glob("*"):
            p.unlink()
        db = Database(d / "r.db", cfg()["storage"])
        try:
            reg = AssetRegistry(DetailCG(DETAILS), cfg()["assets"], providers(), clock=lambda: NOW)
            asyncio.run(reg.run_once([("QNT", "quant-network"), ("XRP", "ripple")], max_calls=5))
            rows = reg.pop_dirty()
            db.insert("asset_registry", REGISTRY_COLS, [registry_row(r) for r in rows])
            db.flush()
            got = db.read_sync(load_registry)
            self.assertEqual(got["QNT"]["contract_address"], QNT)
            self.assertEqual((got["XRP"]["native_chain"], got["XRP"]["native_asset"]), ("xrpl", 1))
            self.assertEqual(reg.pop_dirty(), [])
        finally:
            db.close()
            shutil.rmtree(d, ignore_errors=True)


class ProviderAndMonitorTests(unittest.TestCase):
    def test_registry_routing(self):
        reg = ProviderRegistry([FakeProvider(("ethereum", "bsc"), keyed=False), FakeProvider(("tron",), keyed=True)])
        self.assertTrue(reg.supports("bsc"))
        self.assertFalse(reg.supports("solana"))
        self.assertEqual(reg.supported_chains(), ["bsc", "ethereum", "tron"])
        self.assertTrue(reg.keyed())
        self.assertFalse(reg.keyed("ethereum"))
        self.assertEqual(reg.remaining_today(), 2000)
        self.assertEqual(reg.chain_state("aptos")["reason"], "Aptos: provider not implemented")
        self.assertEqual(ProviderRegistry([]).supported_chains(), [])

    def test_all_providers_built_with_env_names_only(self):
        p = providers()
        self.assertEqual({x.family for x in p.providers},
                         {"evm", "bitcoin", "xrpl", "tron", "solana", "hedera", "cardano"})
        self.assertEqual(p.for_chain("xdc").name, "etherscan")
        self.assertEqual(p.for_chain("cardano").name, "koios")
        for x in p.providers:
            s = json.dumps(x.stats())
            self.assertNotIn("apikey", s.lower())
            if x.key_env:
                self.assertEqual(x.stats()["key_env"], x.key_env)

    def test_track_config_wins_and_contract_owner_unique(self):
        m = monitor()
        self.assertFalse(m.add_token("QNT", "ethereum", "0x" + "44" * 20))
        self.assertEqual(m.tokens["QNT"]["contract"], QNT)
        self.assertEqual(m.tokens["QNT"]["source"], "config")
        self.assertFalse(m.add_token("QNTX", "ethereum", QNT.upper().replace("0X", "0x")))   # same contract
        self.assertTrue(m.add_token("uni", "ethereum", UNI.upper().replace("0X", "0x"), 18))
        t = m.tokens["UNI"]
        self.assertEqual((t["source"], t["token_wide"], t["status"], t["decimals"]), ("discovered", "off", "pending", 18))
        self.assertEqual(m.asset_for("ethereum", UNI), "UNI")
        m.sync([TrackedAsset("BTC", "bitcoin", True, source="discovered", status="ok")], keep={"QNT", "BTC"})
        self.assertEqual(sorted(m.assets), ["BTC", "QNT"])                 # UNI left the universe; config stays


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
                tx("binance", "okx", 5_000_000, reg)]                       # CEX->CEX routing is excluded
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
