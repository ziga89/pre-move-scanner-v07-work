# Changelog

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
