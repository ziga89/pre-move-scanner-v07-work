# Architecture (v0.8.0, as implemented)

```
CoinGecko ──► universe/        Top-N (back-fill past 100) − exclusions + manual assets (by CoinGecko id), hysteresis, cache fallback
Exchanges ──► universe/        catalogs (load_markets + fetch_tickers) → per-coin venue selection, FX, checks
   (ccxt)     feeds/           capability matrix → partitions → supervised book/trade loops → health
              engine/          MarketState per coin×venue (band book, rings, baselines, features)
              engine/          AssetState per coin (flags, aggregation, sub-scores, pipeline, status, events)
CoinGecko ──► intel/registry   asset registry: native coin / token, chain, contract, provider, overrides (SQLite)
Chains    ──► intel/providers/ Etherscan V2 (EVM + XDC) · Esplora · rippled · TronGrid · Solana RPC · Hedera · Koios
                               → RawTransfer → monitor (budget / priority) → labels → classifier → WalletEvent
              intel/           scores MM / Whale / CEX flow / Scarcity → status OFF / NO KEY / DISCOVERING / WARMING /
                               ACTIVE / UNSUPPORTED / DEGRADED / N/A
              engine/alerts.py Signal Radar: WATCH / CONFIRMING / HIGH-CONVICTION / INVALIDATED → alerts table
              storage/         SQLite WAL, writer thread, rollups, retention, history, migrations + backup, v0.6 import
              service.py       orchestration loops + payload builders
              app.py           FastAPI (REST + WebSocket topics)      devserver.py  stdlib fallback
web/                           Top table, coin detail, health, universe + manual assets (vanilla JS modules, canvas charts)
tools/                         bootstrap.py (venv / config / data), make_release.py (clean ZIP + verifier), wallet_check.py,
                               live_onboarding_check.py, selftest.py, feed_stress.py, import_v06.py
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

## Feed client lifecycle (v0.7.1)

* One partition = one ccxt.pro instance = one client **generation**. The instance is created inside the
  running event loop (`asyncio_loop` option) and opened before its first watch. Every loop is bound to its
  generation, and an instance is closed exactly once, before the next one is created.
* Error classes: **permanent** (bad symbol / not supported → that market UNAVAILABLE, multi-symbol chunks
  fall back to per-symbol loops); **client** (the instance is unusable: AttributeError, TypeError, closed or
  wrong-loop client → one rebuild per generation, with backoff; markets RECONNECTING); **transient**
  (network → per-loop exponential backoff).
* The circuit breaker opens only when the partition delivers no data at all, so a failing market never
  takes down healthy ones. The watchdog replaces a partition whose order books stay silent.
* Exchange quirks are handled in `CcxtStreamClient`: Coinbase USD↔USDC aliases are mapped back to the
  subscribed symbol. Updates for symbols that weren't requested are counted, never dropped silently. A
  missing stream dependency (MEXC: `protobuf`) is reported as UNAVAILABLE with the reason.

## Signal Radar and wallet status (v0.7.3)

* **Per tick**, after an asset is scored, `service._intel_ctx` computes the wallet scores. `intel/status.asset_status`
  then gives the asset and each score an explicit state, and `gate_scores` passes on only the values in
  state OK (everything else is `None`). The state goes into `res["wallet_status"]`.
* `HighConvictionAlerts.update(res, now)` assesses the result:
  * 15 strict checks, and the WATCH gates (see `engine/alerts.py`);
  * the wallet: only *attributed* accumulation is supportive, hostile flow vetoes, and reshuffling is
    listed but never counted.

  It then advances the per-asset state machine: WATCH (with a soft-dip linger) → CONFIRMING (120 s,
  reset on any break) → HIGH_CONVICTION → INVALIDATED. Moves and hostile wallets end an alert immediately;
  faded confirmation or incomplete feeds end it after 60 s of hysteresis.
* After the asset loop, `sweep` ends the alerts of assets that stopped updating or left the universe.
  `pop_changes()` hands fired / refreshed (every 30 s) / ended rows to the writer (`alerts` table,
  `critical=True`). `radar(now, wallet_summary, feeds)` builds the headline state and the entries that
  `/api/top` (every second), `/api/radar` and `/api/alerts` carry to the always-visible bar.
* **Charts** draw each row of `/api/history/{asset}` → `alerts` as a band from `fired_ts` to `ended_ts`.
  At start-up, `close_open_alerts` ends any alert the previous run left open.
* **v0.8 changes.** The decision logic above is unchanged. The display and the wallet input change:
  * WATCH and CONFIRMING have their own labels.
  * Each entry carries `checks_passed / checks_total` (15) and `gates_passed / gates_total`, plus
    highlights built from the measured values ("ask supply draining", "bid support building", "sustained
    buy pressure", "volume accelerating (Nx normal)", "price still flat", wallet line).
  * The wallet domain reads scores built from the v0.8 event types, gated by the eight-state status
    (see below).
  * The note "Evidence score is a composite strength, not a probability" stays on every payload.

## v0.8: universe, manual assets and the asset registry

* **One version.** `server/__init__.py` defines `__version__`. `/api/version`, the UI title, the HTTP
  user agent, the release ZIP name and `run_windows.bat` (through `tools/bootstrap.py --version`) read it
  from there. `sw.js` is served with the version substituted, so every release invalidates the PWA cache.
* **Universe.** `universe.build(rows, …, manual_rows, manual=…)` returns the Top-N members plus the manual
  assets:
  * Manual assets are resolved **by CoinGecko id**. Legacy config tickers fall back to a *unique* ticker
    match. An ambiguous ticker yields candidates, never a guess.
  * A manual asset that is also a Top-N member is listed once (`manual=True, in_top=True`).
  * A manual asset whose ticker is taken by a different member is reported, not monitored.
  * `AssetInfo.manual` is display-only: scoring, sorting and the radar never read it.
* **Manual-asset lifecycle** (`service.search_assets / resolve_asset / add_manual_asset /
  remove_manual_asset`, table `manual_assets`, helper `universe/manual.py`):
  * *Search* calls CoinGecko `/search`.
  * *Resolve* fetches the coin row and its registry decision, and previews venues from the current exchange
    catalogs.
  * *Add* stores the row. `_integrate_manual` then selects venues, creates the `AssetState` and subscribes
    feeds for that one asset, without a restart and without touching the other assets.
  * *Remove* unsubscribes a manual-only asset and keeps every stored row. For a Top-N member it only clears
    the mark.
  * `config.universe.manual_assets` (`pinned_assets` in v0.7) seeds the table once.
* **Asset registry** (`intel/registry.py`, table `asset_registry`). `decide(coin)` is a pure function of
  CoinGecko's coin detail:
  * a native coin (`BY_NATIVE_COIN`) → READY with the chain's provider;
  * a token → READY only on its home platform (`asset_platform_id`), with a validated contract (per-family
    address codec in `intel/addr.py`);
  * a multi-platform coin without a home platform → NEEDS_VERIFICATION;
  * a known chain without a provider → UNSUPPORTED "provider not implemented";
  * a coin CoinGecko does not have → NOT_FOUND.

  Overrides (`assets.overrides`, `intel.tokens`, `PUT /api/assets/{symbol}/override`) always win.
  `_registry_loop` looks up `discovery_calls_per_minute` coins per minute in priority order, persists the
  decisions and calls `_sync_wallet_assets`, which tracks the READY entries in the monitor. Entries are
  re-checked after `discovery_ttl_days`.

## v0.8: wallet providers

* **Interface** (`intel/providers/base.py`). A `WalletProvider` has:
  * `family`, `chains`, `supports(chain)`, `state(chain)` (ok / off / no_key / degraded + reason) and
    `normalize_address(chain, address)`;
  * `fetch_address(chain, address, assets, cursor) → FetchResult(transfers, cursor, complete)`;
  * `fetch_balance(chain, address, asset)`, optional `fetch_token` (token-wide, EVM only), `stats()` and
    `secret_values()`.

  Every HTTP call goes through `_call`, which:
  * paces the call on a `ProviderBudget` (calls per second, daily cap counted per UTC day and persisted with the cursors, back-off
    after HTTP 429, consecutive-failure tracking);
  * classifies errors (auth, rate limit, chain unavailable, budget);
  * scrubs every secret value (API key, private RPC URL) from error texts before they reach logs, Health
    or the database.
* **Adapters** (`intel/providers/*.py`) normalise their API into `RawTransfer` rows: chain, asset/token,
  from, to, amount, tx hash, stable index, timestamp and block. Chain-specific semantics stay inside the
  adapter:
  * **Esplora and Koios:** UTXO; change excluded, receipts attributed to the dominant input
    (`normalize.utxo_transfers`).
  * **Solana and Hedera:** balance deltas allocated to the counterparties (`normalize.allocate_deltas`).
  * **XRPL:** `delivered_amount` only.
  * **TronGrid:** hex addresses → base58check.
  * **Etherscan V2:** `tokentx` / `txlist` per chain id (XDC = 50) with a newest-first seed, then ascending
    pages.

  Cursors are opaque JSON per (chain, address), stored in `intel_cursors`.
* **Registry** (`ProviderRegistry`): `build_providers(icfg, http, budget_store)` creates one provider per
  enabled family from `intel.providers.<family>`, keeping v0.7's Etherscan keys. `for_chain(chain)`
  routes; `chain_state(chain)` gives the Health / status view.
* **Monitor** (`intel/monitor.WalletMonitor`):
  * One address poll per (chain, labelled address) covers every tracked asset on that chain.
  * Discovered token contracts are verified on the first transfer's symbol.
  * Events are de-duplicated by (chain, tx, index), classified (`classify.classify_transfer` → `CEX_IN`,
    `CEX_OUT`, `ACCUMULATION`, `DISTRIBUTION`, `INTERNAL_SHIFT`, `MM_ROUTING`, `CUSTODY_SHIFT`, `BRIDGE`,
    `DEX_FLOW`, `UNKNOWN_TRANSFER` with attribution confidence and entity type) and persisted in
    `onchain_transfers` (the v0.8 columns are added by migration 3).
  * **Scheduling:** per provider, the base interval comes from the remaining daily budget (70 % for address
    polls, 20 % for balances, 10 % for token-wide polls). Each asset's tier from `service._tiers` sets the
    multiplier: 1 anomaly and 2 radar = ×1, 3 manual = ×2, 4 top-50 = ×4, 5 quiet = ×8.
* **Scores** (`intel/scores.py`): only attributed, reliable events count. `INTERNAL_SHIFT`, `MM_ROUTING`,
  `CUSTODY_SHIFT` and `BRIDGE` are routing and never count as buying. Unattributed CEX outflow gets half
  weight in CEX flow and never counts as accumulation.
* **Status** (`intel/status.asset_status`), checked in this order:
  1. OFF;
  2. UNSUPPORTED (registry or chain without a provider);
  3. DISCOVERING (no registry decision yet);
  4. N/A (NEEDS_VERIFICATION, rejected contract, no trusted labels, no USD reference);
  5. NO KEY;
  6. DEGRADED (provider failing, rate-limited, out of budget, chain not on the plan);
  7. WARMING (first polls, history window);
  8. ACTIVE (possibly "partial" while lagging).

  `gate_scores` passes values to the engine only when the state is ACTIVE.

## v0.8: storage, upgrade and release

* **Migration 3** (`storage/schema.migration_3`) is additive and idempotent: each statement checks for the
  table or column first. `Database.migrate` runs it inside one `BEGIN … COMMIT`, and rolls back on
  error. Before any pending migration on an existing database, `backup_before_migration` writes an
  online SQLite backup to `data/backups/`. It skips the backup when free space is below 2× the database
  size. `app_meta` records the running app version, the first installed version and an upgrade history
  (version and schema changes). Health → Storage shows the upgrade and the backup file.
* **Bootstrap** (`tools/bootstrap.py`) is shared by `run_windows.bat` and `run.sh`:
  * creates the venv and installs the requirements;
  * copies `config.example.json` → `config.json` **only when missing**;
  * creates `data/`;
  * reports the database state.

  It never deletes or replaces anything.
* **Release** (`tools/make_release.py`) builds from an allow-list (server/, web/, tools/, tests/, docs/,
  labels/, launchers, examples), then refuses and verifies the archive:
  * refused: `data/`, `config.json`, `.venv/`, `*.db*`, caches, `.env`, `dist/`;
  * refused: any file with an SQLite header;
  * refused: any file containing the value of a set secret environment variable or a key-shaped string;
  * `--verify` re-checks any ZIP.

  `tests/test_release.py` fails if a prohibited file would be packaged.

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
| Signal Radar states, persistence / hysteresis, late rejection (v0.7.3) | `engine/alerts.py`, `web/js/radar.js` |
| Mandatory market structure, cross-venue, no single venue / single print (v0.7.3) | `alerts.assess` (`gate`, `checks`) |
| Reshuffling never BUY (v0.7.3) | `alerts._wallet`, `intel/scores.cex_outflow_attributed_share` |
| Alerts in SQLite + chart marks (v0.7.3) | `storage/schema.py` migration 2, `storage/history.alerts_query`, `web/js/charts.bands` |
| Explicit wallet states + coverage (v0.7.3) | `intel/status.py`, `web/js/radar.walletCell`, Health panel |
| EVM contract auto-discovery, provider abstraction (v0.7.3) | superseded in v0.8 by `intel/registry.py`, `intel/providers/` |
| Top-100 + manual assets, deduplicated, manual never affects score (v0.8) | `universe/universe.py`, `universe/manual.py`, `service.add_manual_asset / remove_manual_asset` |
| Manual assets UI: search, candidates, preview, confirm, remove (v0.8) | `web/js/universe.js`, `/api/assets/*` |
| Persistent asset registry, no guessing, overrides (v0.8) | `intel/registry.py`, table `asset_registry` |
| Provider adapters, one WalletEvent model (v0.8) | `intel/providers/`, `intel/model.py`, `intel/classify.py` |
| Taxonomy and event types; custody / MM / internal never buying (v0.8) | `intel/labels.py`, `intel/classify.py`, `intel/scores.py` |
| Eight wallet states, zero ≠ N/A (v0.8) | `intel/status.py`, `web/js/radar.walletCell`, Health |
| Budget-aware polling with priorities, provider stats (v0.8) | `intel/providers/base.ProviderBudget`, `intel/monitor.schedule`, `service._tiers` |
| Secrets only as env-var names, scrubbed everywhere (v0.8) | `server/env.py`, `WalletProvider.scrub`, `tools/make_release.py` |
| Idempotent migration + backup, never overwrite config / DB (v0.8) | `storage/schema.migration_3`, `storage/db.py`, `tools/bootstrap.py` |
| One canonical version (v0.8) | `server/__init__.py`, `tools/bootstrap.py --version`, `app.py` (`sw.js`) |
| Clean release ZIP + regression test (v0.8) | `tools/make_release.py`, `tests/test_release.py` |

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
