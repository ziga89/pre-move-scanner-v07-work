"""Top-N engine benchmark (no network): N coins x V venues of synthetic data.

    python tools/bench_engine.py --assets 100 --venues 4 --minutes 3

Measures, separately from the synthetic data generator:
  * ingestion CPU (book diffs + trades) per second of market data
  * 1 Hz tick CPU (all MarketState.tick + AssetState.update)
  * memory of engine state (tracemalloc), incl. full 24 h minute rings
"""
from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import DEFAULTS, deep_merge  # noqa: E402
from server.engine.asset_state import AssetState  # noqa: E402
from server.engine.market_state import MarketState  # noqa: E402
from server.sim import SimWorld  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", type=int, default=100)
    ap.add_argument("--venues", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--book-hz", type=float, default=4.0, help="book updates per second per market")
    a = ap.parse_args()
    cfg = deep_merge(DEFAULTS, {"engine": {"min_baseline_minutes": 1, "baseline_lag_minutes": 0}})
    tracemalloc.start()
    world = SimWorld(n_assets=a.assets, venues_per_asset=min(a.venues, 5), seed=3, scenarios=False)
    snap0 = tracemalloc.take_snapshot()
    states = {}
    assets = {}
    t0 = 1_760_000_000.0
    for i, (key, m) in enumerate(world.markets.items()):
        ex, sym = key
        asset = sym.split("/")[0]
        states[key] = MarketState(asset, ex, sym, m.quote, lambda q: 1.0, cfg["engine"], cfg["feeds"], now=t0, stagger=i)
        assets.setdefault(asset, AssetState(asset, cfg, stagger=i))
    n_markets = len(states)
    by_asset = {}
    for key, st in states.items():
        by_asset.setdefault(st.asset, []).append(st)
    dt = 1.0 / a.book_hz
    steps = int(a.minutes * 60 * a.book_hz)
    gen = ingest = tick = 0.0
    n_book = n_trades = 0
    worst_tick = 0.0
    for i in range(steps):
        ts = t0 + i * dt
        g0 = time.perf_counter()
        data = [(key, m.step(ts, dt)) for key, m in world.markets.items()]
        g1 = time.perf_counter()
        for key, (b, s_, tr) in data:
            st = states[key]
            if b:
                st.on_book(b, s_, ts)
                n_book += 1
            if tr:
                st.on_trades(tr, ts)
                n_trades += len(tr)
        g2 = time.perf_counter()
        gen += g1 - g0
        ingest += g2 - g1
        if (i + 1) % int(a.book_hz) == 0:
            k0 = time.perf_counter()
            for asset, sts in by_asset.items():
                feats = [st.tick(ts) for st in sts]
                assets[asset].update(ts, feats, len(sts))
            k = time.perf_counter() - k0
            tick += k
            worst_tick = max(worst_tick, k)
    secs = a.minutes * 60
    # Fill 24 h of minute records to measure steady-state memory of the baseline rings.
    for st in states.values():
        rec = {f: 1.0 for f in st.minutes.fields}
        for mi in range(1440):
            st.minutes.append(int(t0) + 86400 + mi * 60, rec)
    snap1 = tracemalloc.take_snapshot()
    mem = sum(s.size_diff for s in snap1.compare_to(snap0, "filename"))
    print(f"markets: {n_markets} ({a.assets} assets x ~{n_markets / a.assets:.1f} venues), simulated {secs:.0f}s, "
          f"{n_book / secs:.0f} book updates/s, {n_trades / secs:.0f} trades/s")
    print(f"ingestion CPU : {ingest / secs * 100:.1f}% of one core")
    print(f"tick CPU      : {tick / secs * 1000:.0f} ms per 1 s tick (worst {worst_tick * 1000:.0f} ms)  "
          f"-> {tick / secs * 100:.1f}% of one core")
    print(f"engine memory : {mem / 2**20:.0f} MB incl. 24 h minute rings (~{mem / n_markets / 1024:.0f} KB per market)")
    print(f"(synthetic data generation, not part of the scanner: {gen / secs * 100:.0f}% of one core)")


if __name__ == "__main__":
    main()
