import json
import shutil
import unittest

from tests.helpers import scratch_dir
from server.config import load_config

V06_CONFIG = {  # the v0.6 config.example.json
    "watchlist": ["QNT", "LINK", "XDC"],
    "coingecko_ids": {"QNT": "quant-network", "LINK": "chainlink", "XDC": "xdc-network"},
    "venue_discovery": {"enabled": True, "max_venues_per_asset": 5, "candidate_markets": 20},
    "sample_interval_seconds": 1, "persist_interval_seconds": 5, "baseline_minutes": 120, "warmup_minutes": 30,
    "onchain": {"enabled": False, "etherscan_api_key_env": "ETHERSCAN_API_KEY", "poll_seconds": 15,
                "assets": {"QNT": {"contract": "0x4a220E6096B25EADb88358cb44068A3248254675",
                                   "watch_wallets": {"Wintermute": "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa"}}}},
}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("config")
        for p in self.d.glob("*"):
            p.unlink()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_v06_config_is_understood(self):
        (self.d / "config.json").write_text(json.dumps(V06_CONFIG))
        c = load_config(self.d)
        # v0.8: the v0.6 watchlist / v0.7 pinned list becomes the initial manual-asset list
        self.assertEqual(c["universe"]["manual_assets"][:3], ["QNT", "LINK", "XDC"])
        self.assertEqual(c["universe"]["coingecko_ids"]["QNT"], "quant-network")
        self.assertEqual(c["engine"]["baseline_minutes"], 120)
        self.assertEqual(c["engine"]["min_baseline_minutes"], 20)
        lab = c["intel"]["legacy_labels"][0]
        self.assertEqual((lab["entity"], lab["entity_type"]), ("Wintermute", "MM"))
        self.assertIn("QNT", c["intel"]["tokens"])
        self.assertTrue(any("watchlist" in w for w in c["_meta"]["warnings"]))

    def test_existing_config_never_overwritten(self):
        (self.d / "config.example.json").write_text(json.dumps({"mode": "sim"}))
        (self.d / "config.json").write_text(json.dumps({"mode": "live", "universe": {"target_size": 50}}))
        c = load_config(self.d)
        self.assertEqual(c["universe"]["target_size"], 50)
        self.assertEqual(json.loads((self.d / "config.json").read_text())["mode"], "live")

    def test_created_only_when_missing_and_bad_json_falls_back(self):
        (self.d / "config.example.json").write_text(json.dumps({"universe": {"target_size": 77}}))
        c = load_config(self.d)
        self.assertEqual(c["universe"]["target_size"], 77)
        self.assertTrue((self.d / "config.json").exists())
        (self.d / "config.json").write_text("{not json")
        c = load_config(self.d)
        self.assertEqual(c["universe"]["target_size"], 100)
        self.assertTrue(any("could not parse" in w for w in c["_meta"]["warnings"]))

    def test_legacy_usdt_watchlist(self):
        (self.d / "config.json").write_text(json.dumps({"watchlist": ["QNTUSDT", "AUDIOUSDT"]}))
        c = load_config(self.d)
        self.assertIn("AUDIO", c["universe"]["manual_assets"])


if __name__ == "__main__":
    unittest.main()
