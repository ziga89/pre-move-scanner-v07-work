import os
import unittest
from unittest.mock import patch

from server.env import env_secret


class EnvSecretTests(unittest.TestCase):
    def test_direct_process_environment(self):
        with patch.dict(os.environ, {"PMS_SECRET_TEST": " abc123 "}, clear=False):
            self.assertEqual(env_secret("PMS_SECRET_TEST", "IGNORED"), "abc123")

    def test_configured_name_is_stripped(self):
        with patch.dict(os.environ, {"PMS_SECRET_TEST": "value"}, clear=False):
            self.assertEqual(env_secret("  PMS_SECRET_TEST  ", "IGNORED"), "value")

    def test_windows_persisted_fallback(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("server.env._windows_persisted_env", return_value="persisted") as reg:
            self.assertEqual(env_secret("ETHERSCAN_API_KEY", "ETHERSCAN_API_KEY"), "persisted")
            reg.assert_called_once_with("ETHERSCAN_API_KEY")


class SecretHandlingTests(unittest.TestCase):
    """Config holds only environment-variable NAMES; key values never reach config, logs, the API, the
    database or a release (the release side is in test_release.py)."""
    NAME = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")
    SECRETISH = __import__("re").compile(r"(api_?key|secret|password|passphrase|(^|_)token$|private_?url)")

    def walk(self, obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from self.walk(v, f"{path}.{k}")
                yield f"{path}.{k}", k, v
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from self.walk(v, f"{path}[{i}]")

    def test_config_contains_only_env_var_names(self):
        import json
        from server.config import DEFAULTS
        from tests.helpers import ROOT
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        for src in (DEFAULTS, example):
            for path, key, value in self.walk(src):
                if key.endswith("_env"):
                    self.assertRegex(str(value), self.NAME, path)            # a NAME, never a key
                if self.SECRETISH.search(key.lower()) and not key.lower().endswith(("_env", "_type")):
                    self.fail(f"{path}: a secret-looking setting must be an *_env name")
        self.assertEqual(example["intel"]["etherscan_api_key_env"], "ETHERSCAN_API_KEY")
        self.assertEqual(example["universe"]["coingecko_api_key_env"], "COINGECKO_API_KEY")

    def test_keys_are_loaded_and_never_exposed(self):
        import asyncio
        import json
        from server.intel.providers import build_providers
        from server.universe.coingecko import CoinGeckoClient
        from tests.helpers import cfg
        secret = "S3CRET-KEY-VALUE-123456"
        env = {"PMS_T_ES": secret, "PMS_T_CG": secret + "cg", "PMS_T_TRON": secret + "tron",
               "PMS_T_SOL": "https://rpc.example/?api-key=" + secret}
        with patch.dict(os.environ, env, clear=False):
            c = cfg(intel={"enabled": True, "etherscan_api_key_env": "PMS_T_ES",
                           "providers": {"tron": {"api_key_env": "PMS_T_TRON"}, "solana": {"rpc_url_env": "PMS_T_SOL"}}},
                    universe={"coingecko_api_key_env": "PMS_T_CG"})

            class Boom:
                async def get_json(self, url, params=None, headers=None):
                    raise ConnectionError(f"failed {url}?apikey={(params or {}).get('apikey')} headers={headers}")

                async def post_json(self, url, body, headers=None):
                    raise ConnectionError(f"failed {url} {headers}")
            reg = build_providers(c["intel"], Boom(), {})
            evm = reg.for_chain("ethereum")
            self.assertEqual(evm.key, secret)                              # loaded from the environment
            self.assertTrue(evm.keyed)
            for coro in (evm.tokentx("ethereum", address="0x" + "11" * 20), reg.for_chain("tron").probe(),
                         reg.for_chain("solana").probe()):
                with self.assertRaises(Exception) as cm:
                    asyncio.run(coro)
                self.assertNotIn(secret, str(cm.exception))
            dump = json.dumps(reg.stats())
            self.assertNotIn(secret, dump)
            self.assertIn("PMS_T_ES", dump)                                 # the NAME is shown, not the value
            cg = CoinGeckoClient(None, c["universe"])
            self.assertEqual(cg.headers.get("x-cg-demo-api-key"), secret + "cg")
            self.assertNotIn(secret, json.dumps(cg.stats()))


if __name__ == "__main__":
    unittest.main()
