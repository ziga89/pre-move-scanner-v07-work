# Changelog

## v0.7.3 — Signal Radar, explicit wallet-intelligence states, contract discovery

### Added
* **Signal Radar**, an always-visible bar at the top of every view (overview, coin, health, universe).
  One headline state at a time:
  `NO HIGH-CONVICTION SETUP` · `WATCH / CONFIRMING` · `HIGH-CONVICTION BUY SETUP` · `INVALIDATED`.
  The bar shows the asset, the evidence score (a composite strength, not a probability), the confirmed
  venues (count and names), the persistence time (a progress bar while confirming), the strongest
  reasons and the wallet-intelligence status. It also shows what is still missing, or why a setup ended.
  Up to four other entries are listed as chips, with the wallet coverage text and the feed state. If the
  bar is not refreshed for 15 s it says so. New endpoint: `/api/radar`.
* **Radar logic** (`server/engine/alerts.py`, same `HighConvictionAlerts` API as v0.7.2):
  * **WATCH** needs every mandatory gate:
    * price still flat (only late state `FLAT` counts; an unknown state is not flat);
    * every selected feed live;
    * **market structure**: a book-side family (ask thinning / no replenishment / bid support) on at
      least 2 venues, and an order-book sub-score of at least 50;
    * at least 2 confirming venues;
    * no single print dominating the tape;
    * no hostile wallet flow;
    * a pre-move score of at least 70.
  * **CONFIRMING**: all 15 strict checks hold (unchanged from v0.7.2, including at least 3 confirming
    venues and book-side and buy-flow families on at least 2 venues each). The 120 s persistence timer is
    running, and any break resets it.
  * **HIGH-CONVICTION BUY SETUP** fires only after 120 s of continuous strict confirmation.
  * **INVALIDATED**:
    * immediately when the price moves (`MOVING` / `IN_PROGRESS` / `LATE`, with the % move since the
      fire) or the wallet flow turns hostile;
    * after a 60 s hysteresis when the confirmation fades or the feeds stay incomplete;
    * when an asset stops updating or leaves the universe.
  * An invalidated setup is the headline for 10 min and stays listed for 15 min.
  * WATCH lingers 20 s on a soft dip (score or venue count) so the bar does not flicker. A hard gate
    failure clears it at once.
* **Alert persistence**: a new `alerts` table (schema migration 2, applied automatically). Each alert is
  written when it fires, every 30 s while open, and when it ends. The row keeps peak evidence, the price
  at fire and at end, the confirmed venues, reasons, checks, wallet state and the end reason. Alerts left
  open by a previous run are closed at start-up ("scanner restarted - setup not re-verified"). Retention
  is `storage.alerts_days` (365).
* **Alerts on the historical charts**: a green `HC` band on the price and score-breakdown charts from
  the fire to the end, with a red dashed `✕` where the alert was invalidated. The data comes from the
  alerts table (`/api/history/{asset}` returns `alerts`), so the band survives the 500-event cap. ALERT
  events also appear as timeline markers.
* `/api/alerts` and `/api/alerts/history` are kept. `/api/alerts/history` also returns the `alerts` rows
  (`days` 1–365, `limit` 1–5000). The stdlib dev server now serves `/api/alerts`,
  `/api/alerts/history` and `/api/radar`.
* **Explicit wallet-intelligence states** replace the generic N/A, per asset and per score
  (MM / whale / CEX flow / scarcity):
  * `OFF`: disabled in the config;
  * `NO KEY`: no `ETHERSCAN_API_KEY`;
  * `WARMING`: contract lookup, first polls, or the history window (`intel.warmup_minutes`, 60) still
    filling;
  * `UNSUPPORTED`: the chain has no provider, or the asset is a native coin;
  * `N/A`: no reliable attribution (no contract, no labelled addresses of the needed kind, no USD
    reference);
  * otherwise the real value.

  The states are shown in the table cells, the coin view (per score, with the reason), the radar and a
  new Health panel: assets per state, labelled addresses per chain, tokens (configured / discovered),
  discovery progress, provider budgets and the last poll. Only values in state OK reach the scoring
  engine and the radar; everything else is `None`, never 0.
* **Automatic EVM contract discovery** (`server/intel/discovery.py`), which only runs where it is safe:
  * It reads CoinGecko's coin detail for each universe coin.
  * It accepts a token only if the token is *native* to a supported EVM chain (`asset_platform_id`), the
    contract is a valid address and the symbol matches.
  * Bridged copies of tokens that live on another chain are not tracked.
  * Native coins (BTC, XRP, SOL, XDC, ...) and native EVM gas coins (ETH, BNB, AVAX) are UNSUPPORTED,
    with the reason stated.
  * The monitor re-checks the on-chain token symbol on the first transfer and rejects the contract if it
    differs.
  * Configured tokens (`intel.tokens`) always win.
  * Results are cached in the new `token_contracts` table and re-checked after `discovery_ttl_days` (30).
    Failed lookups are retried after 1 h.
  * Lookups are paced to `discovery_calls_per_minute` (2) and run only while a provider key is set.
    Pinned coins go first, then current anomalies, then by rank.
* **Provider registry** (`server/intel/providers.py`). The monitor, the status resolver and discovery ask
  the registry which provider serves a chain. Etherscan V2 serves the EVM chains today, and further chains
  or providers plug in behind the same small interface.
* **UNKNOWN / WHALE CANDIDATE**: large transfers (at least `intel.whale_candidate_usd`, 250,000) between
  a labelled exchange and an unlabelled address are listed in the coin view. No identity is inferred,
  and they are not counted in any score.

### Fixed / changed relative to the provided v0.7.2
* **Reshuffling counted as buying.** v0.7.2 treated market-maker `OFF_EXCHANGE` and *any* CEX outflow as
  supportive, including outflow to unlabelled addresses, which may be the exchange's own wallets.
  Now only attributed accumulation supports a setup:
  * whale ACCUMULATION (labelled holders only) of at least 35;
  * and either a real supply drain, or a CEX outflow of which at least 50 % reached labelled holders
    (`cex_outflow_attributed_share`).

  Market-maker direction, `ROUTING` and `RESHUFFLING` are never supportive; they are shown as
  "reshuffling seen, not counted".
* **Missing late data invalidated alerts.** A missing late-move state (no data) invalidated an active alert
  immediately as "price moved". Only real MOVING / IN_PROGRESS / LATE states do that now. Missing data
  goes through the hysteresis and ends as "feeds stale / incomplete".
* **ALERT markers never reached the charts.** The coin view filtered ALERT markers out.
* **The dev server returned 404 for `/api/alerts`.**
* **Wallet status with no labelled addresses.** With token-wide polling on, an asset with no labelled
  addresses showed WARMING forever. It is now N/A with the reason.
* Health: the `etherscan` block reads the provider registry. Version 0.7.3, service-worker cache
  `premove-v073`.

### Preserved
* The v0.7.1 KuCoin client lifecycle, MEXC protobuf, Coinbase USD↔USDC mapping and Windows launcher
  fixes. `server/feeds/`, `server/selftest.py`, `tools/`, `run_windows.bat`, `requirements*.txt` and the
  CI workflows are byte-identical to v0.7.1.
* The preserved v0.6 history folder is unchanged (manifest check in `docs/TEST_REPORT.md`).

### Tests
* New `tests/test_signal_radar.py`: states and headline priority, persistence and hysteresis, stale
  feeds and sweep, late rejection, single-venue and single-print spikes, mandatory market structure,
  CEX / MM reshuffling, alert row persistence.
* New `tests/test_wallet_status.py`: every wallet state, per-score N/A, value gating, coverage summary,
  discovery decisions and pacing / TTL / retry, provider registry, `add_token`, whale candidates and
  attributed share.
* Storage: alerts round trip, restart close-out, retention by `fired_ts`, `token_contracts`, and an
  upgrade from a v0.7.2 database.
* Service (SIM): radar and OFF states end to end; an alert fires, is persisted, appears in the chart
  history, is invalidated, and is closed on restart.
* Dev-server and FastAPI routes; the Playwright UI smoke test checks the radar on every view and the
  wallet states.

## v0.7.2 — High-conviction alert rail (provided build)

- Added a strict HIGH-CONVICTION BUY SETUP engine above the ordinary Pre-Move score.
- Requires sustained cross-venue market structure + execution confirmation while price is still flat.
- Wallet/CEX intelligence can strengthen or veto an alert; internal/reshuffling flows are never treated as proof of buying.
- Added 120 s persistence, clearing hysteresis, single-large-trade rejection, stale/coverage gates and hostile-wallet veto.
- Added a sticky green global alert rail on every web view, click-through to coin detail, and green ALERT chart/timeline markers.
- Added `/api/alerts` and `/api/alerts/history`; fired alerts are also stored in `signal_outcomes` for forward-return calibration.
- Evidence score is explicitly a composite evidence strength, not a probability or guarantee.

## v0.7.1 — KuCoin stream fix, client lifecycle, honest self-test

### Fixed
* **KuCoin streams failed with `AttributeError: 'NoneType' object has no attribute 'create_task'`.**
  Cause (reproduced with the real ccxt 4.5.84): stream clients receive the catalog's markets through
  `set_markets()`, so ccxt never makes a REST request and never calls `open()`, which binds an instance
  to the event loop. Most exchanges' watchers open the instance themselves. KuCoin's watchers first call
  `negotiate()` → `spawn()` → `self.asyncio_loop.create_task(...)` while `asyncio_loop` is still `None`.
  This failed every time KuCoin was actually streamed. It looked intermittent because whether KuCoin is
  among a coin's selected venues depends on live volumes.
* The error was treated as a transient network error. Every KuCoin loop retried the broken instance, the
  watchdog rebuilt an identical broken one, and the failures tripped the partition's circuit breaker for
  all KuCoin markets.
* The self-test's realtime summary said PASS at ≥ 80 % of markets. It also missed mid-run status changes,
  because the feed manager binds its status callback at construction.
* **Coinbase USDC pairs never streamed.** Coinbase serves `BASE/USDC` under `BASE-USD`, and ccxt resolves
  the USDC request with a book and trades labelled `BASE/USD`. The multi-symbol loop discarded them
  because that symbol wasn't subscribed. The USD↔USDC alias is now mapped back to the subscribed symbol,
  and updates for unsubscribed symbols are counted (`unmatched_msgs`), never dropped silently.
* **MEXC streams died as "ping-pong keepalive missing".** ccxt 4.5.84 decodes MEXC's spot stream as
  protobuf but does not install it. The `NotSupported` error escapes ccxt's receive callback and the socket
  is never read again. `requirements.txt` now includes `protobuf==5.29.5`. If the package is missing, MEXC
  markets are `UNAVAILABLE` with that reason instead of timing out, and the self-test checks for it.
* **`run_windows.bat` mangled options.** It passed only four arguments, and cmd splits on commas, so
  `selftest --assets QNT,XDC,LINK` reached the self-test as `--assets QNT XDC LINK`. Everything after the
  action is now passed on unchanged. The exit code is returned, there is a new `stress` action, and
  `PMS_NO_PAUSE=1` skips the prompts. CI runs the launcher on a Windows runner.

### Changed
* `CcxtStreamClient` binds its ccxt instance to the running loop (`asyncio_loop` option plus an explicit
  `open()`) before the first watch. It refuses use after `close()` or from another loop, and `close()` is
  idempotent. Market sharing (no REST reload per connection) is kept.
* New error class **client** (AttributeError, TypeError, ExchangeClosedByUser, closed / wrong-loop client).
  The partition rebuilds its client once per generation, with backoff. Markets go `RECONNECTING` with the
  real error text, never `UNAVAILABLE`.
* Each stream loop is bound to its client generation, so it never touches a newer, closed or missing
  client. Clients are closed exactly once before a replacement is created, and removing an exchange waits
  for its loops to stop.
* The circuit breaker trips only when a partition delivers **no data at all**, so one failing market cannot
  take down the healthy ones.
* Self-test: every feed incident is recorded. Each market and each exchange PASSes only without incidents,
  with feed errors, client rebuilds, breaker trips and the last error shown. New options: `--repeat N` and
  `--exchanges kucoin`.
* New `tools/feed_stress.py`: a live start / resubscribe / stop-start / socket-drop / rebuild / new-manager
  stress test with a Markdown / JSON report.

### Tests
* ccxt-free lifecycle model (reproduces the error) plus a 60-cycle start/stop stress test with faults.
* The real ccxt KuCoin code against a local fake KuCoin server (token REST, snapshot REST, websocket), with
  an 18-cycle stress test.
* Self-test incident reporting.
* A GitHub workflow runs the live tests against the real KuCoin, repeatedly, on Windows and Ubuntu runners.

## v0.7.0 — Top-100 pre-move universe scanner

### Scope
* Scans a **Top-100 universe** instead of a hand-typed watchlist: CoinGecko market-cap ranking,
  back-filled beyond rank 100 until 100 eligible **and** usable coins exist; stablecoins, wrapped /
  staked / bridged tokens, tokenised gold and duplicate tickers excluded (with reasons); pinned coins
  (default QNT, LINK, XDC) monitored in addition; hourly refresh with membership hysteresis;
  cached-universe fallback if CoinGecko is unavailable.
* Main screen is **Top anomalies right now**, ranked by Pre-Move score — never by market cap.

### Venue discovery (per coin)
* One `load_markets` + `fetch_tickers` per exchange (≈15 calls instead of ≈200 CoinGecko calls).
* Ranking by the coin's **own** 24h USD volume; one best pair per exchange; up to 5 venues.
* New safeguards: price identity check (rejects a different token sharing the ticker), live quote→USD FX
  (USDT/USDC/EUR/KRW/TRY/BTC…), spread / stale / minimum-volume filters, wash-volume check vs visible
  depth, realtime verification (no book within 90 s → next venue promoted), replacement hysteresis,
  periodic re-discovery (v0.6 discovered once at startup), CoinGecko cross-check of unsupported top markets.

### Feeds
* **cryptofeed replaced by ccxt / ccxt.pro** (v0.6 called `load_symbols()` / `run_async()`, which could
  not be confirmed against cryptofeed's documented API — see the audit; ccxt installs as pure-Python
  wheels on Windows, is actively maintained, and uses the same symbols for discovery and streaming).
* **Exchange capability matrix**: static per-exchange limits combined with the installed ccxt's `has`
  flags; multi-symbol subscriptions only where supported, per-symbol elsewhere; automatic per-symbol
  fallback when a multi-symbol call fails on one symbol.
* Per-exchange partitions, exponential backoff with jitter, circuit breaker, order-book watchdog,
  subscription diffing (only changed partitions restart), per-exchange / per-partition health.
* Optional worker processes (`feeds.workers`) for Top 250 / 500.

### Engine
* ±2 % band-limited order book; diffs ignore far levels, band re-centring and truncated depth;
  reconnect snapshots are never diffed (no phantom removal spikes); 60 s resync grace.
* Bounded memory (fixed-size rings) — v0.6's raw deques silently truncated on busy markets.
* New metrics: slippage for a coin-scaled order, **refill after fills** (churn-independent
  replenishment), net ask flow, **cancellation/removal proxy with explicit confidence** (never presented
  as confirmed cancellations), largest-trade share, 15/30/60 min returns per venue.
* Lagged robust baselines (median / MAD; 30 min / 2 h / 24 h) per coin × venue × metric, rehydrated from
  SQLite after a restart (no repeated warm-up). Receipt-time windows (clock-skew safe), duplicate and
  historical-replay trade filtering.

### Scoring
* Sub-scores: order-book, liquidity (movability), buy pressure (absorption guard), cross-venue (with
  leader → follower propagation); MM / Whale / CEX-flow / Scarcity from wallet intelligence.
* Explicit order: structural → compression / context → confidence → independent-signal / venue /
  liquidity-share / coverage / warm-up caps → **late-move penalty and hard cap last** → persistence.
* Late-move logic: hard +5 %/15m, +8 %/30m, +12 %/1h **plus volatility-normalised displacement**
  (robust σ per asset); `MOVE IN PROGRESS` and `LATE`.
* Two-speed signal: `EMERGING` (30 s median, within ~30–60 s) and `CONFIRMED PRE-MOVE` /
  `STRONG PRE-MOVE` (150 s median + 120 s hold), with hysteresis.
* All thresholds relative to each venue's own normal (buy share, imbalance, depth, spread).
* Null semantics: missing wallet data is **N/A** — never 0, never negative, never a signal family.
* Human-readable reasons, and the cap that limited a score.

### Wallet intelligence
* Address-centric monitoring of labelled MM / CEX / custody / whale addresses (Etherscan V2, budgeted,
  priority for top anomalies); token-wide polling only where manageable (auto-disabled above a transfer
  rate); contract-symbol verification; balance snapshots; Δ1h / 6h / 24h / 7d per entity.
* Conservative classifier (BUY, SELL, ACCUMULATION-SIDE, DISTRIBUTION-SIDE, SHIFT, UNKNOWN); Coinbase
  Hot → Prime is SHIFT; CEX withdrawal ≠ purchase; MM routing detected via net vs gross flow; real supply
  drain vs reshuffling.

### History and events
* SQLite WAL with a dedicated writer thread — no SQL on the event loop (v0.6 ran DB work on the feed loop).
* 5 s / 10 s / 1-minute tables, 7-day charts from 1-minute rollups with **max-preserving** bucketing
  (v0.6 stride sampling could hide spikes), 30-day asset history, 90-day events, retention by index.
* Event timeline (status, score, book, flow, venue, price, wallet, system) with debounce / hysteresis
  and automatic **breakout precursors** ("ask depth −38 % on Gate, 27 min before").
* Signal outcome logging (forward 15m / 1h / 4h / 24h returns) for calibration.
* **v0.6 history import** (read-only, hash-verified, idempotent); v0.6 series shown dashed in charts.

### UI
* New Top-anomalies table (keyed in-place updates — v0.6 collapsed panels every second), filters,
  sorting, explicit N/A; coin detail, health and universe views; all v0.6 charts kept and extended.
* WebSocket topics (`top`, `coin:X`, `health`) with latest-wins queues; HTTP polling fallback; no timer
  leak on reconnect; PWA kept.

### Tooling
* `run_windows.bat` never overwrites `config.json` (v0.6 did on every start) and gains `selftest`, `sim`,
  `import`, `test`.
* `tools/selftest.py` (live checks + report), `tools/import_v06.py`, `tools/print_capabilities.py`,
  `tools/bench_engine.py`, stdlib dev server (`python -m server.devserver --sim`).
* 124 automated tests; GitHub Actions on Ubuntu + Windows × Python 3.11–3.13, JS lint, headless-browser UI
  test, v0.6 integrity check.

### Removed / replaced (functionality preserved)
* `server/multivenue.py` → `server/engine/*`, `server/feeds/*`, `server/service.py`
* `server/discover.py` → `server/universe/*`
* `server/onchain.py` → `server/intel/*`
* `server/scanner.py` (unused v0.1–v0.3 Binance scanner) — kept only in the v0.6 reference folder
* `web/app.js` → `web/js/*` (charts ported into `web/js/charts.js`)
* `cryptofeed` dependency → `ccxt`
* iOS starter client: unchanged (it has not matched the server since v0.4 — see known limitations)
