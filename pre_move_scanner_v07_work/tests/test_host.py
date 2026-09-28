import asyncio
import time
import unittest

from tests.helpers import cfg
from server.engine.host import LocalEngineHost, MarketSpec, ProcessEngineHost
from server.feeds.sim_adapter import SimAdapter, SimDriver
from server.sim import SimWorld


def sim_cfg():
    return cfg(mode="sim", sim={"assets": 3, "venues_per_asset": 3, "seed": 3, "scenarios": False},
               feeds={"backend": "sim"})


def specs_for(world):
    return [MarketSpec(sym.split("/")[0], ex, sym, sym.split("/")[1], 1e6) for (ex, sym) in world.markets]


class LocalHostTest(unittest.TestCase):
    def test_local_host_streams_and_rewires(self):
        async def go():
            c = sim_cfg()
            world = SimWorld(n_assets=3, venues_per_asset=3, seed=3, scenarios=False)
            drv = SimDriver(world, step_s=0.05)
            dt = asyncio.ensure_future(drv.run())
            adapters = {ex: SimAdapter(ex, drv) for ex in {k[0] for k in world.markets}}
            calls = []
            host = LocalEngineHost(c, adapters, lambda q: 1.0, rehydrate=lambda s: calls.append(s) or [])
            specs = specs_for(world)
            r = await host.set_markets(specs)
            self.assertEqual(r["added"], len(specs))
            self.assertEqual(len(calls), len(specs))            # rehydration attempted per new market
            await asyncio.sleep(1.2)
            feats = host.tick(time.time())
            self.assertEqual(sum(len(v) for v in feats.values()), len(specs))
            states = {f["state"] for v in feats.values() for f in v}
            self.assertTrue(states <= {"WARMING", "RESYNCING", "LIVE"}, states)
            ex, sym = specs[0].exchange, specs[0].symbol
            self.assertTrue(host.book_summary(ex, sym)["ask"])
            r2 = await host.set_markets(specs[1:])
            self.assertEqual(r2["removed"], 1)
            self.assertNotIn((ex, sym), host.states)
            await host.stop()
            dt.cancel()
        asyncio.run(go())


class ProcessHostTest(unittest.TestCase):
    def test_worker_processes(self):
        async def go():
            c = sim_cfg()
            world = SimWorld(n_assets=3, venues_per_asset=3, seed=3, scenarios=False)
            specs = specs_for(world)
            exchanges = sorted({s.exchange for s in specs})
            host = ProcessEngineHost(c, 2, exchanges)
            await host.start()
            try:
                r = await host.set_markets(specs)
                self.assertEqual(sum(r["per_worker"].values()), len(specs))
                owners = {}
                for ex, w in host.assign.items():
                    owners.setdefault(ex, set()).add(w)
                self.assertTrue(all(len(v) == 1 for v in owners.values()))  # each exchange in one worker
                deadline = time.time() + 40
                while time.time() < deadline:
                    feats = host.tick(time.time())
                    if sum(len(v) for v in feats.values()) == len(specs):
                        break
                    await asyncio.sleep(0.5)
                feats = host.tick(time.time())
                self.assertEqual(sum(len(v) for v in feats.values()), len(specs))
                h = host.health()
                self.assertEqual(len(h["workers"]), 2)
                self.assertTrue(all(w["alive"] for w in h["workers"]))
                self.assertTrue(h["exchanges"])
            finally:
                await host.stop()
            self.assertTrue(all(not p.is_alive() for p in host.procs))
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
