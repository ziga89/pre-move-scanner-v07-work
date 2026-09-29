# Changelog

## v0.8.0 — Multi-chain wallet intelligence and manual assets

### Universe and manual assets
* **Monitored universe = CoinGecko Top-100 ∪ manual assets**, deduplicated by CoinGecko id. With 100
  members and 5 manual assets outside them, 105 unique assets are monitored. A manual asset that is
  already a Top-100 member is marked manual but not duplicated. A manual asset whose ticker belongs to a
  *different* Top-100 coin is reported, not merged.
* **Manual assets are persistent** (SQLite `manual_assets`) and replace v0.7 "pinned" coins.
  * The config list (`universe.manual_assets`, or the v0.7 `universe.pinned_assets`) only seeds symbols
    never seen before, so removing QNT in the UI is not undone at the next start. QNT, LINK and XDC carry
    over.
  * Removing an asset keeps its row and all its history.
* **Manual status never affects score or sorting.** Ranking is by pre-move score (late / quiet groups
  as before), with market-cap rank as the tie-break. A manual asset can rank #1 or last.
* **Never guessed:**
  * Assets are identified by CoinGecko id. A ticker alone returns candidates and is never auto-resolved.
  * A seeded v0.7 ticker without a valid id is resolved only if exactly one fetched coin uses it.
  * An ambiguous ticker waits for your selection. A missing coin is reported.
* **Universe page → Manual assets.**
  1. Search by ticker, name or CoinGecko id.
  2. Candidates show symbol, name, id, rank, price, the venues found in the exchange catalogs, and
     metadata when it is known.
  3. The preview shows chain / token platform, contract, wallet-intelligence support and venues.
  4. Add or remove the asset.
* **An added asset joins at runtime, with no restart:** venue selection, feed subscription, baseline
  warm-up, the ranking, Signal Radar / high-conviction eligibility, and metadata discovery.
* New API endpoints:
  * `GET /api/assets`, `GET /api/assets/{symbol}`, `GET /api/assets/search?q=`,
    `GET /api/assets/resolve/{coingecko_id}`;
  * `POST /api/assets/manual` (`coingecko_id`; `query` only returns candidates with HTTP 409),
    `DELETE /api/assets/manual/{symbol}`, `PUT /api/assets/{symbol}/override`.

### Asset registry (`server/intel/registry.py`, SQLite `asset_registry`)
* Every monitored asset gets persistent metadata: symbol, name, CoinGecko id, market-cap rank, manual,
  native chain, token platform, contract, native asset, wallet provider, wallet supported, discovery
  confidence, verified, last metadata check, created / updated.
* It is discovered automatically from the CoinGecko coin detail, paced to 2 calls per minute, cached
  for 30 days, and failed lookups are retried after 1 h. Nothing is rediscovered on restart.
* **Decisions (never guessed):**
  * **Native coins** come from a curated map: BTC, ETH, BNB, AVAX, XRP, TRX, SOL, XDC, HBAR, ADA.
  * **Tokens** are tracked only on the platform CoinGecko names as native (`asset_platform_id`), with a
    contract that is well-formed for that chain family. Bridged copies elsewhere are ignored.
  * **Multi-platform coins without a native platform** get `NEEDS_VERIFICATION`: nothing is tracked
    until you set an override.
  * **Chains without a provider** are `UNSUPPORTED · provider not implemented`.
* **Overrides:** `intel.tokens` (v0.7) or `assets.overrides` / `PUT /api/assets/{symbol}/override`:
  `{chain, contract, decimals}`, `{chain, native: true}` or `{unsupported: reason}`.
* A discovered EVM contract is verified on-chain: the first transfer's token symbol must match, or the
  contract is rejected.
* The v0.7.3 `token_contracts` cache is migrated (supported rows only).

### Multi-chain wallet providers (`server/intel/providers/`)
* One interface (`base.WalletProvider`). Each provider normalises raw chain data into the same
  `RawTransfer` → `WalletEvent` model. Attribution, classification and scoring are shared and never
  chain-specific.

| Provider | Chains | Assets | Key |
|---|---|---|---|
| **Etherscan V2** | Ethereum, BSC, Base, Arbitrum, Optimism, Polygon, Avalanche, Mantle, Linea, Scroll, Blast, XDC (chain 50) | ERC-20 tokens (`tokentx`) and native coins (`txlist`: ETH, BNB, AVAX, XDC) | `ETHERSCAN_API_KEY` |
| **Esplora** (mempool.space, blockstream.info fallback) | Bitcoin | BTC (UTXO: change and consolidations never counted; receipts attributed to the dominant input) | none |
| **rippled JSON-RPC** (xrplcluster, s1/s2.ripple.com fail-over) | XRP Ledger | XRP and issued tokens, from `delivered_amount` (partial payments never overstated) | none |
| **TronGrid** | TRON | TRX (`TransferContract`) and TRC-20 | optional `TRONGRID_API_KEY` |
| **Solana JSON-RPC** | Solana | SOL and SPL tokens (owner token accounts; balance deltas; supports transaction version 1) | optional private RPC URL in `SOLANA_RPC_URL` |
| **Hedera mirror node** | Hedera | HBAR and HTS tokens (fee accounts and node fees removed) | none |
| **Koios** | Cardano | ADA and native tokens (UTXO; stake-key identity) | optional `KOIOS_API_TOKEN` |

* Not implemented, and shown as `UNSUPPORTED · provider not implemented`: Sui, Aptos, TON, NEAR,
  Polkadot, Stellar, Cosmos, Algorand, Litecoin, Dogecoin, Bitcoin Cash, Ethereum Classic, Internet
  Computer, Monero, Tezos, and any other chain.
* **Budget-aware polling** per provider:
  * calls today are persisted, and each provider has a daily cap and a per-second pace;
  * HTTP 429 / "too busy" back-off honours Retry-After;
  * consecutive failures are tracked, along with the last success and last error;
  * the cap is never exceeded: the call is refused.
* **Polling priority per asset:** 1 current anomaly, 2 radar WATCH / CONFIRMING, 3 manual, 4 top-50,
  5 quiet.
  * Quiet addresses are polled up to 8× less often.
  * Anomalies are polled twice as often while the budget is healthy.
* A busy address that cannot be paged through in one poll is marked `lagging`. Its scores are damped
  and shown as `ACTIVE (partial)`.
* Etherscan chains that the API plan does not cover are reported per chain with Etherscan's own text,
  as DEGRADED.
* Provider error texts, stats and logs never contain an API key or a private RPC URL: they are scrubbed.

### Wallet taxonomy, event types and states
* **Entity taxonomy:** CEX_HOT, CEX_COLD, CEX_DEPOSIT, CEX_CUSTODY, CUSTODY_INSTITUTIONAL,
  MARKET_MAKER, DEX_POOL, BRIDGE, TREASURY, WHALE, WHALE_CANDIDATE, UNKNOWN (plus DEX_ROUTER, WATCH and
  the built-in BURN).
  * `MM` and `PROTOCOL_TREASURY` are still accepted.
  * Labels are validated per chain family (base58check, bech32, …). Invalid addresses are rejected.
* **Event types:** CEX_IN, CEX_OUT, ACCUMULATION, DISTRIBUTION, INTERNAL_SHIFT, MM_ROUTING,
  CUSTODY_SHIFT, BRIDGE, DEX_FLOW, UNKNOWN_TRANSFER, each with attribution and classification
  confidence.
  * Internal exchange moves, exchange ↔ exchange routing, market-maker routing, custody shifts and
    bridges are excluded from reserve flows and never count as buying.
  * **Changed from v0.7:** exchange → institutional custody (e.g. Coinbase → Anchorage,
    Coinbase Hot → Prime) is now a CUSTODY_SHIFT; v0.7 counted it as accumulation-side.
  * Holder ↔ holder is UNKNOWN_TRANSFER.
* Stored v0.7 transfers are re-attributed with the current labels when loaded.
* **Explicit wallet states:** OFF, NO KEY, DISCOVERING, WARMING, ACTIVE, UNSUPPORTED (with the reason),
  DEGRADED, N/A.
  * `OK / ON` is now `ACTIVE`, and a pending lookup is `DISCOVERING`.
  * A real 0 stays 0. Only ACTIVE values reach scoring and the radar.

### Signal Radar
* WATCH and CONFIRMING are separate headline states and labels.
* Entries carry the checks passed (e.g. `8/15 checks`) and the gates passed.
* A HIGH-CONVICTION BUY SETUP lists plain-language evidence: "ask supply draining", "sustained buy
  pressure", "volume accelerating (2.1x normal)", "price still flat", "wallet / CEX flow supportive" and
  "active 4m 12s".
* The wallet chip reads supportive / neutral / unavailable / **contradictory**.
* Wallet evidence stays an independent, optional domain:
  * unavailable data raises the pre-move threshold (90) instead of blocking;
  * supportive lowers it (82);
  * contradictory vetoes.
* Manual and non-EVM assets are assessed identically.

### Health
* A wallet-intelligence table has one row per chain group: Ethereum / EVM, Bitcoin, Solana, XRPL,
  TRON, XDC, Hedera, Cardano. Each row shows state (with reason), key status, assets covered (and
  their states), calls / budget, rate-limit state, errors, last success and last error.
* Global counts of ACTIVE / DISCOVERING / WARMING / UNSUPPORTED / DEGRADED (+ OFF / NO KEY / N/A).
* Registry discovery progress and the list of chains without a provider.
* Storage shows the schema version, "new database" / "upgraded from schema N", the previous app version
  and the pre-migration backup.
* New API endpoints: `GET /api/wallet/providers`, `GET /api/wallet/status`, `GET /api/wallet/{symbol}`.

### Database, bootstrap, release safety, version
* **Migration 3** (automatic, additive, idempotent, one transaction):
  * adds `asset_registry`, `manual_assets` and `app_meta`, and the new event columns on
    `onchain_transfers`;
  * `data/scanner_v07.db` keeps its name, and every row is preserved;
  * **before migrating an existing database, a consistent SQLite backup** is written to `data/backups/`.
    It is skipped, and reported, when disk space is short.
  * an interrupted upgrade leaves the database untouched and simply runs again;
  * `app_meta` records the running app version, the first installed version and an upgrade history.
* **`tools/bootstrap.py`** (standard library only): creates `config.json` from the example only if it
  is missing (never overwritten) and `data/` if missing, and reports whether the database is new or
  existing. `--venv` creates `.venv` and installs `requirements.txt`. `run_windows.bat` and the new
  `run.sh` call it.
* **`tools/make_release.py`** builds a source-only ZIP from an allow-list and refuses to build or
  verify an archive that contains any of the following:
  * `data/`, `config.json` or `.venv/`;
  * `*.db`, `*.db-wal` or `*.db-shm`, or any file with a SQLite header;
  * `__pycache__`, caches or reports;
  * the value of any API-key environment variable, or a CoinGecko-style key.

  Build it with `run_windows.bat release`. The verifier found that the v0.7.4 source ZIP contained 48
  stale `__pycache__/*.pyc` files. It had no data files and no secrets.
* **One canonical version** (`server/__init__.py`):
  * the UI header, title and footer read it from `/api/version`;
  * the service-worker cache name is filled in by the server;
  * the HTTP user-agent, the dev-server header, the launcher banner and title (via
    `bootstrap.py --version`) and the release name all use it.

  A test fails on any hard-coded version in the UI or the launchers.
* `run_windows.bat walletcheck` / `tools/wallet_check.py` is a live check of CoinGecko discovery and
  every provider. It picks sample addresses from each chain's latest data and never uses a hard-coded
  wallet identity. A check that a public endpoint keeps rate-limiting is reported as NOT VERIFIED.
* `tools/live_onboarding_check.py` is a live end-to-end check of the real service, using its own
  database:
  * Top-N plus manual assets;
  * adding a coin ranked 251–500 at runtime, then streaming it;
  * metadata discovery and wallet states;
  * ranking and radar;
  * restart persistence;
  * removal with history kept.

### Fixed
* When CoinGecko is unreachable and no cached ranking exists, the manual assets are still monitored.
  Previously the ranking was retried only at the next hourly refresh. It is now retried after 2, 4, 8, …
  minutes (`universe.retry_minutes`), up to the normal interval. Found by the live onboarding check:
  CoinGecko refused a GitHub runner with HTTP 403.
* Removed the unused v0.7 module `server/intel/discovery.py`, superseded by the asset registry.

### Preserved
* The ARGS forwarding logic of `run_windows.bat` is unchanged, with CRLF line endings.
* `server/feeds/`, the scoring engine and the baselines are unchanged: the KuCoin lifecycle / rebuild,
  MEXC protobuf and Coinbase USD/USDC fixes, the honest self-test and the pre-move scoring.
* The Signal Radar gates, persistence and hysteresis are unchanged, as are alert persistence, chart
  markers and outcomes.
* Key loading through `server/env.py` is unchanged.
* The v0.6 history folder is unchanged (manifest check).

## v0.7.4 — API keys from the Windows user environment (provided build)
* `server/env.py`: API keys are read by environment-variable name. On Windows the persisted user or
  machine value (from `setx`) is used when the running shell predates it. The configured name is
  stripped, so a trailing space cannot turn a present key into NO KEY.

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
