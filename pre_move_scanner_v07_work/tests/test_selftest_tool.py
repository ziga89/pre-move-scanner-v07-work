"""tools/selftest.py must never report a feed incident as PASS (offline, SIM exchanges)."""
import asyncio
import importlib.util
import unittest
from pathlib import Path

from server.feeds.registry import build_adapters
from server.universe.fx import FxService
from tests.helpers import cfg as base_cfg

ROOT = Path(__file__).resolve().parents[1]


def load_selftest():
    spec = importlib.util.spec_from_file_location("selftest_tool", ROOT / "tools" / "selftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SelftestIncidentTest(unittest.TestCase):
    def test_incidents_are_reported_not_passed(self):
        st = load_selftest()

        async def go():
            cfg = base_cfg()
            cfg["mode"] = "sim"
            cfg["feeds"].update(backoff_initial_seconds=0.02, backoff_max_seconds=0.05)
            adapters, driver = build_adapters(cfg)
            dtask = asyncio.ensure_future(driver.run())
            markets = [(a, ex, s, "USDT", 1e6, "selected") for (ex, s), a in
                       [((ex, s), s.split("/")[0]) for ex, s in driver.world.markets][:6]]
            bad_ex = markets[0][1]

            async def inject():
                await asyncio.sleep(0.6)
                driver.fail[bad_ex] = 3          # three consecutive stream errors on one exchange
            inj = asyncio.ensure_future(inject())
            worst = await st.stream_round(cfg, adapters, markets, FxService(), 2.0, "9")
            await inj
            dtask.cancel()
            return worst, bad_ex
        worst, bad_ex = asyncio.run(go())
        res = {r["step"]: r for r in st.RESULTS}
        ex_line = res[f"9 exchange {bad_ex}"]
        self.assertEqual(ex_line["status"], "WARN", ex_line)
        self.assertIn("feed errors", ex_line["detail"])
        self.assertNotIn("feed errors 0,", ex_line["detail"])
        bad_markets = [r for k, r in res.items() if k.startswith(f"9 stream {bad_ex} ")]
        self.assertTrue(bad_markets)
        self.assertTrue(all(r["status"] != "PASS" for r in bad_markets), bad_markets)
        self.assertTrue(all("INCIDENTS" in r["detail"] for r in bad_markets))
        self.assertEqual(res["9 realtime summary"]["status"], "WARN")
        self.assertEqual(worst, "WARN")


if __name__ == "__main__":
    unittest.main()
