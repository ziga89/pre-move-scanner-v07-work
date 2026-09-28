# v0.7 architecture (as implemented)

```
CoinGecko ──► universe/        Top-N (back-fill past 100) − exclusions + pinned, hysteresis, cache fallback
Exchanges ──► universe/        catalogs (load_markets + fetch_tickers) → per-coin venue selection, FX, checks
   (ccxt)     feeds/           capability matrix → partitions → supervised book/trade loops → health
              engine/          MarketState per coin×venue (band book, rings, baselines, features)
              engine/          AssetState per coin (flags, aggregation, sub-scores, pipeline, status, events)
Etherscan ──► intel/           labels → address-centric monitor → classifier → MM/Whale/CEX/Scarcity (null-aware)
              storage/         SQLite WAL, writer thread, rollups, retention, history, v0.6 import
              service.py       orchestration loops + payload builders
              app.py           FastAPI (REST + WebSocket topics)      devserver.py  stdlib fallback
web/                           Top table, coin detail, health, universe (vanilla JS modules, canvas charts)
```

## Data flow per second

1. **Ingestion** (event loop, per message): `MarketState.on_book` applies the ccxt book view to a ±2 % band
   (processing throttled to 4 Hz per market; the netted diff is kept) and accumulates added / removed
   notional inside the ±1 % flow band; `on_trades` dedupes, drops historical replays and accumulates
   aggressive buy / sell notional into 1-second rings. Receipt time is used everywhere.
2. **Tick** (1 Hz): every `MarketState.tick` produces a feature row (depths, spread, imbalance, slippage,
   flows, refill, cancel proxy + confidence, returns, ratios / z-scores vs its own lagged baseline, health,
   confidences). Minute records are finalised and baselines recomputed on a per-market staggered second.
3. **Asset scoring**: `AssetState.update` → venue flags → cross-venue aggregation → sub-scores →
   ordered pipeline → fast / slow persistence → status (with hysteresis) → reasons; onsets feed lead / lag
   and the event detector.
4. **Persistence** via the writer thread (5 s asset rows, 10 s venue rows, 1-minute rollups, events,
   outcomes); **broadcast** of the compact top table (1 Hz) and the open coin (2 s).

## Requirement → implementation map

| Requirement | Where |
|---|---|
| Top 100, stable / wrapped exclusion, back-fill, pinned, refresh | `universe/universe.py`, `universe/filters.py`, `service.refresh_universe` |
| Per-coin venues by own volume, verify pair + realtime | `universe/venues.py`, `service.tick_once` (verification / wash / unavailable feedback) |
| L2, spread, depth ±0.5/1/2 %, flows, imbalance, slippage | `engine/book.py`, `engine/market_state.py` |
| Cancellation as a proxy with confidence (amendment 1) | `market_state.cancel_proxy_confidence`, UI "est." labels |
| Capability matrix, multi vs per-symbol, limits, health (amendment 3) | `feeds/capabilities.py`, `feeds/manager.py` |
| Late-move penalty, hard thresholds + vol-normalised (amendment 4) | `engine/scoring.late_assessment`, `sigma_estimates` |
| EMERGING vs CONFIRMED two-speed (amendment 5) | `engine/asset_state.py` |
| Explicit scoring order (amendment 6) | `engine/scoring.run_pipeline` + persistence re-cap in `asset_state` |
| Universe back-fill definition (amendment 7) | `universe/universe.py` |
| Null semantics (amendment 8) | `intel/scores.py`, `scoring.context_points`, UI `na()` |
| Address-centric wallet intelligence (amendment 2) | `intel/monitor.py`, `intel/classify.py`, `intel/labels.py` |
| False-positive controls | activity / book / concentration confidence, family / venue / share caps, persistence, relative baselines |
| Baselines per coin / exchange / metric, warm-up | `engine/baselines.py` (lagged median / MAD 30 m / 2 h / 24 h, rehydration) |
| Stale data never scores | `MarketState.state` (STALE / DISCONNECTED / RESYNCING), exclusion in `aggregate` |
| History ≥ 7 days, timeline, precursors | `storage/*`, `engine/events.py` |
| Scale path Top 250 / 500 | `engine/host.ProcessEngineHost` (`feeds.workers`) |
| CI (amendment 9) | `.github/workflows/ci.yml` (repository root) |

## Scoring details

**Venue families** (strength 0–1, only for LIVE + warmed venues): `thinning` (ask depth ratio **and**
z-score vs own baseline; slippage), `no_replenish` (refill after fills, net ask withdrawal z-score, cancel
proxy × its confidence), `buy_flow` (taker-buy share above the venue's own normal × activity confidence),
`volume` (vs own normal; damped when sell-driven), `bid_support` (imbalance rise with bids not falling).
A venue *confirms* with ≥ 2 active families and confidence ≥ 0.35.

**Pipeline** (`run_pipeline`): structural = 0.35·order-book + 0.25·cross-venue + 0.22·buy + 0.18·liquidity →
× (1 + compression bonus ≤ 10 %) + wallet context (±8, 0 when N/A) → × (0.35 + 0.65·confidence) and
low-activity / tiny-book caps → caps: families (0→20, 1→35, 2→55, 3→72), live venues (1→42, 2→72),
confirmed venues (0→39, 1→59, 2→82), confirming liquidity < 30 % → 60, live liquidity < 50 % → 60,
warm-up → 44 → late multiplier and LATE cap 25 (**last**) → fast / slow medians, current caps re-applied.

**Late index** `L = max(r15/5 %, r30/8 %, r60/12 %, |z|/3 for moves ≥ 1 %)` (down moves × 0.7);
`L ≥ 0.35` penalty starts, `≥ 0.6` MOVE IN PROGRESS, `≥ 1` LATE.

**Status**: EMERGING = fast ≥ 55, ≥ 2 families, ≥ 2 confirming venues or ≥ 50 % liquidity;
CONFIRMED = slow ≥ 60, ≥ 3 families, ≥ 2 confirming venues, ≥ 70 % of the last 120 s above 60;
STRONG = slow ≥ 72 and ≥ 50 % of liquidity confirming. 5-point hysteresis on downgrades.

All numbers are defaults in `server/config.py` (`scoring` section) and can be tuned; signal outcomes
(`/api/outcomes`) record forward returns to support calibration.

## Performance (measured, `tools/bench_engine.py`, one CPU core, synthetic data)

Top 100 × 4 venues = 400 markets, 1 600 book updates/s, ~580 trades/s:
ingestion ≈ 10 % of a core, tick ≈ 48 ms per second (worst 80 ms), engine memory ≈ 213 MB including full
24 h minute rings. (v0.6 measured: ~1.9 s per 1 s tick, ~4.2 GB.) ccxt's own websocket parsing comes on
top and can only be measured live; for Top 250 / 500 enable `feeds.workers` (exchanges are sharded across
processes; only 1 Hz feature rows cross process boundaries).
