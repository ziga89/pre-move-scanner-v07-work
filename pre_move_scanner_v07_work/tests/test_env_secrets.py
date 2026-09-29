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


if __name__ == "__main__":
    unittest.main()
