# v0.7 test report

## v0.7.3: Signal Radar and wallet-intelligence states

**Summary: 217 automated tests pass. 67 of them are new in v0.7.3, and 150 are the existing v0.7.1 /
v0.7.2 tests.** The suite was run twice in the cloud container:
* plain Python 3.11: 217 run, 7 skipped (the real-ccxt and FastAPI tests);
* with ccxt 4.5.84 installed: 217 run, only the FastAPI test skipped. PyPI is not reachable from the
  container; CI installs FastAPI.

Also run:
* the Playwright UI smoke test (SIM, all 40 checks);
* `node --check` on every web module;
* ESLint on `web/`;
* `compileall`.

GitHub Actions results are listed further down.

### New tests

| Area | Test file | What is proven |
|---|---|---|
| **Radar states** | `test_signal_radar.py` | NONE → WATCH → CONFIRMING → HIGH-CONVICTION → INVALIDATED. The headline priority. The invalidated entry is the headline for 10 min and listed until 15 min. The bar entry carries the asset, evidence, venues, persistence, reasons and wallet status. Disabled alerts never fire. |
| **Persistence / hysteresis** | `test_signal_radar.py` | One failed check restarts the 120 s timer. An every-other-tick flicker never fires in 10 minutes. A dip under 60 s keeps the alert (shown as "dipping"); a longer one ends it as "composite confirmation faded". A re-fire needs a fresh 120 s. WATCH lingers on a soft dip only, and an ended setup must re-earn WATCH. |
| **Stale feeds** | `test_signal_radar.py`, `test_service.py` | 3/4 feeds live blocks WATCH and the fire for 5 minutes. No data scores evidence 0. An active alert ends as "feeds stale / incomplete (2/4 selected venues live)". The sweep ends alerts of assets that stop updating ("no fresh data for 70s") or leave the universe. Stale WATCH / CONFIRMING entries drop off the bar. |
| **Late rejection** | `test_signal_radar.py` | MOVING / IN_PROGRESS / LATE never reach WATCH and never fire, even at a pre-move score of 99. A missing late state is not "flat". A LATE move ends an alert immediately, even with a 600 s hysteresis, and records "+8.0% since fire". |
| **Single-venue spike** | `test_signal_radar.py` | A pre-move score of 99 on one venue never reaches WATCH in 10 minutes. Neither does structure on one venue with confirmations elsewhere. |
| **Single large trade** | `test_signal_radar.py` | A print that is 72 % of the tape never reaches WATCH and never fires. Spikes next to a real setup do not disturb it: only the real setup fires. |
| **Market structure mandatory** | `test_signal_radar.py` | Buy flow on every venue without a book-side family, or with an order-book sub-score under 50, is not even WATCH. Supportive wallets do not replace structure. |
| **CEX / MM reshuffling** | `test_signal_radar.py` | `RESHUFFLING` and `ROUTING` are neutral and listed as "not counted". MM `OFF_EXCHANGE` is never supportive, and the threshold stays at 86. CEX outflow to mostly **unlabelled** wallets (20 % attributed) is not supportive. Loud reshuffling with a weak market never fires. Only labelled-holder accumulation plus a drain or an attributed outflow is supportive (threshold 82). Hostile flow blocks WATCH. |
| **Wallet states** | `test_wallet_status.py` | Each state and its reason: OFF, NO KEY, WARMING (lookup pending, lookup error being retried, first polls pending, "collecting transfer history (10/60 min)"), UNSUPPORTED (a native coin, a chain without a provider), N/A (no contract, a rejected contract whose on-chain symbol differs, no labels even with token-wide polling, no USD reference). Also: a real value with per-score N/A (no labelled market maker); "ON (partial)" while lagging; `gate_scores` never lets a non-OK value through; the coverage summary text and counts. |
| **Contract discovery** | `test_wallet_status.py` | A native ERC-20 is accepted (lower-cased, with decimals). BTC, ETH and BNB are UNSUPPORTED with the reason. A Solana token is UNSUPPORTED, and so is a TRON token even though it has an ERC-20 bridged copy. A chain without a provider is UNSUPPORTED. An invalid address or a symbol mismatch is `not_found`. Lookups are paced (1 per run), configured tokens are never looked up, results are cached and honoured after a restart, re-checked after the 30-day TTL or a coin-id change, and errors are retried after 1 h, not before. |
| **Providers / monitor** | `test_wallet_status.py` | Registry routing, keys, budget and `LookupError` for an unsupported chain. The Etherscan interface. `add_token`: a configured token wins, a duplicate contract is refused, a discovered token has token-wide polling off. |
| **Whale candidates** | `test_wallet_status.py` | A $900k CEX → unlabelled transfer is listed as UNKNOWN / WHALE CANDIDATE, naming only the labelled exchange. The whale score stays 0 and the attributed share is 0. With CEX → CEX shifts excluded, the share is 0.5. |
| **Storage** | `test_storage.py` | The alerts round trip (same id replaced, JSON columns). Open alerts are closed on restart. Retention uses `fired_ts`. `token_contracts` round trip. A **v0.7.2 database** (migration 1 only) is upgraded to migration 2 with its data kept, and re-opening applies nothing. |
| **Service end-to-end (SIM)** | `test_service.py` | The radar and explicit OFF states in `/api/top`, the coin view and Health. A (forced) setup fires, is written to SQLite, is the radar headline, appears in `history().alerts` and in the ALERT timeline events, is invalidated through the normal path, and an alert left open is closed at the next start. |
| **API** | `test_devserver.py`, `test_api_fastapi.py` | `/api/radar`, `/api/alerts` and `/api/alerts/history` (with `days` and `limit`) on both servers. |
| **UI** | `tests/ui/smoke.mjs` | The radar is visible with a valid state and label on the overview, coin and Health views. Wallet cells show OFF (36 of 36). The coin wallet panel shows per-score OFF states and how to enable intel. Health shows the wallet-intelligence panel and the radar state. No JS errors, and no horizontal scroll at 390 px. |

### Bugs found during this work (fixed, with tests)
* The provided v0.7.2 counted MM `OFF_EXCHANGE` and unattributed CEX outflow as supportive wallet evidence.
* It invalidated alerts on a missing late state.
* Its coin charts filtered out the ALERT markers, and its dev server lacked `/api/alerts`.
* During development: WATCH lingered for 20 s after an invalidation or a hard gate failure, and with no
  labelled addresses and token-wide polling on, the wallet status stayed WARMING forever.

### Preserved v0.7.1 fixes
* `server/feeds/`, `server/selftest.py`, `tools/`, `run_windows.bat`, the requirements files and the CI
  workflows have **zero diff** against v0.7.1.
* The KuCoin lifecycle and stress tests, the real-ccxt local KuCoin-server tests, the Coinbase alias
  tests, the MEXC protobuf check and the Windows launcher test all still pass.

### Preserved v0.6 folder
`sha256sum -c v06_manifest.sha256` passes for all 20 files, and `git diff e136c53 HEAD --
pre_move_scanner_v06_history` is empty.

### Not verifiable here
* No live HIGH-CONVICTION alert has been observed; the thresholds are uncalibrated (see
  `KNOWN_LIMITATIONS.md`).
* Contract discovery against the live CoinGecko API and wallet states with a real `ETHERSCAN_API_KEY` need
  your machine: open Health → *Wallet intelligence* after about 10 minutes.

## v0.7.1: KuCoin stream fix, verified against the real exchanges

**Your report:** some live self-test runs lost every KuCoin stream with
`AttributeError: 'NoneType' object has no attribute 'create_task'`, while the KuCoin catalog loaded fine.

**Root cause** (reproduced with the real ccxt 4.5.84, locally and live): the scanner shares the catalog's
markets with each stream client (`set_markets`), so ccxt never makes a REST request and never runs `open()`,
the call that binds an instance to the event loop. KuCoin's watchers call `negotiate()` → `spawn()` →
`self.asyncio_loop.create_task()` before anything opens the instance. It failed **every time KuCoin was
streamed**. It looked intermittent because whether KuCoin is among a coin's selected venues depends on
live volumes. The scanner then made it worse: the error counted as transient, the broken instance was
retried until the circuit breaker opened for every KuCoin market, and the summary still said PASS at
≥ 80 %. Fix and details: `CHANGELOG.md` (v0.7.1).

### Live runs: GitHub Actions, real exchanges, Windows and Ubuntu runners (2 each)

Workflow `.github/workflows/live-feeds.yml`, commit 905227e. The runners are in US data centres.

| Check | Result |
|---|---|
| Raw ccxt KuCoin, shared markets, no `open()` | `AttributeError: 'NoneType' object has no attribute 'create_task'` on **all 4 runners** (root cause reproduced live) |
| `tools/feed_stress.py`, KuCoin QNT/XDC/LINK, 12 cycles per runner: start, resubscribe, stop/start, socket drop, client rebuild, new manager | **48 / 48 cycles PASS**. 0 client errors, 0 error rebuilds, 0 breaker trips; every market delivers new books after every operation |
| Live self-test, KuCoin markets, `--repeat 3` | **12 / 12 rounds clean**: 3/3 markets, 0 feed errors, 0 incidents |
| Full live self-test, all exchanges, `--repeat 2` | **8 / 8 rounds with every selected market streaming** (34–35 markets on 10 exchanges). 7 rounds fully clean; 1 WARN (HTX `InvalidNonce` resync on XDC/USDT, recovered, reported as WARN) |
| Coinbase `LINK/USDC`, `SOL/USDC`, `ETH/USD` stress, 6 cycles per runner | **24 / 24 PASS** (USDC alias fix) |
| MEXC XDC/BTC/ETH stress, 6 cycles per runner | **24 / 24 PASS** (with `protobuf`) |

The first live run (commit fd150d4, before the Coinbase / MEXC fixes) had the same KuCoin result: 48/48
cycles and 12/12 rounds. Its full self-test FAILed MEXC on every runner and Coinbase USDC pairs on Windows,
which the old ≥ 80 % summary would have hidden. Both were real bugs, fixed in the same release (see the
changelog).

Not the scanner's fault, and they need your machine: Binance (HTTP 451) and Bybit (HTTP 403) refuse US
runners, so those two were not live-tested. Bitrue has no trade stream in ccxt (`NotSupported`); its
books stream.

### Offline regression tests added in v0.7.1 (run in CI on Windows + Ubuntu × Python 3.11–3.13)

| File | What it proves |
|---|---|
| `tests/test_kucoin_lifecycle.py` (10, no ccxt) | a model of ccxt's KuCoin lifecycle reproduces the error; the stream client opens before the first watch, refuses reuse after close or from another loop, closes once; a client error rebuilds once (RECONNECTING, never UNAVAILABLE); a persistent client error backs off and is reported; **one failing market cannot take down the others**; **60-cycle start / stop stress** with injected faults: no leaked tasks, no use of a closed client, one open instance at most |
| `tests/test_kucoin_ccxt_local.py` (4, real ccxt) | the real ccxt KuCoin code against a local fake KuCoin server (token REST, snapshot REST, websocket): reproduction, books + trades, bad-symbol isolation, **18-cycle start / stop / drop / token-expiry stress**. It also passes with 0.5 s of added REST latency |
| `tests/test_exchange_quirks.py` (4) | Coinbase USDC-alias updates reach the USDC market; unrequested symbols are counted; missing `protobuf` is reported for MEXC without connecting |
| `tests/test_selftest_tool.py` (1) | injected feed incidents make the market, exchange and summary lines WARN, never PASS |

On the unfixed code, 9 of the 10 lifecycle tests and 3 of the 4 real-ccxt tests fail. The remaining one in
each file only checks that the harness reproduces the bug.

Totals: **143 tests**. Here: 136 run + 7 skipped without FastAPI / ccxt, and 142 + 1 skipped with a
local real-ccxt harness. In CI all 143 run, on 6 OS / Python combinations.

### Still for your Windows machine

`run_windows.bat selftest --repeat 3` (and `run_windows.bat stress` for KuCoin) from your own network
and IP location, including Binance and Bybit, plus a longer live run.

---

## v0.7.0 report

The build environment had **no network access to market data**: PyPI, CoinGecko, every exchange and
Etherscan returned HTTP 403. To make up for that, the core is pure-stdlib (it runs without PyPI). A
synthetic multi-exchange market (`server/sim.py`) and recorded-format fixtures stand in for live data.
GitHub Actions installs the real dependencies and runs everything on **Windows and Ubuntu**.

The tests fall into three groups:

* **A** — executed and passed against the real code and real libraries. The inputs are hand-built, but
  nothing about the market is simulated.
* **B** — executed and passed, but the market, CoinGecko, exchange or Etherscan data they use is synthetic.
* **C** — not run yet. These need live data and your Windows machine.

## Where the tests ran

| Environment | What ran | Result |
|---|---|---|
| Build sandbox (Linux, Python 3.11; FastAPI and ccxt could not be installed) | 124 unit / fixture / scenario tests | **121 passed, 3 skipped** (1 FastAPI, 2 real-ccxt) |
| Build sandbox | ESLint and `node --check` on all JS files | passed, 0 findings |
| Build sandbox | Playwright headless Chromium UI smoke test (desktop + 390 px mobile) | all checks passed |
| Build sandbox | `tools/selftest.py --sim`, `tools/bench_engine.py`, 30-minute SIM demo server | passed / see numbers below |
| GitHub Actions: ubuntu-latest and windows-latest × Python 3.11 / 3.12 / 3.13 | `compileall`, all 124 tests including FastAPI + WebSocket and real ccxt, `print_capabilities.py`, `selftest.py --sim` | **all 6 jobs green** |
| GitHub Actions: web | JS syntax, ESLint 9, Playwright UI smoke test | green |
| GitHub Actions: v0.6 integrity | `sha256sum -c v06_manifest.sha256` on the original folder | green |

CI installed fastapi 0.141.1, uvicorn 0.54.0, httpx 0.28.1, ccxt 4.5.84 and websockets 17.1. The first
Windows run found a real bug: SQLite reader connections were left open, so Windows could not delete the
database file. It was fixed in `Database.close()`, and every run since has been green.

## A — executed successfully, real code and libraries (72 tests + tooling)

| Area | Tests | What is proven |
|---|---|---|
| Band order book | `test_book` (9) | first update is a snapshot (no flows); adds / removes in USD; levels outside ±2 % ignored; band re-centring and truncated depth never counted as removals; crossed books rejected; depth bands, slippage, histogram |
| Market state | `test_market_state` (16) | cancel proxy = removed − fills, never above 0.8 confidence, "very low" without a tape; duplicate and replayed trades dropped; clock skew tolerated; windows use receipt time; STALE after threshold; a disconnect resets the book and the next book is a snapshot; resync grace blocks flows; memory bounded; warm-up → live; rehydration skips warm-up; ring gap zeroing |
| Baselines | `test_baselines` (5) | 10-minute lag keeps an unfolding anomaly out of its own baseline; invalid minutes excluded; warm threshold; derived fields; robust floor |
| Scoring pipeline | `test_scoring` (20) | order of operations (bonus / context cannot bypass family caps; confidence before caps; **late cap applied last**); venue and liquidity-share caps; warm-up caps; missing intel = 0 context, never negative, never a family; hard +5 / +8 / +12 % thresholds; volatility-normalised late detection with a minimum absolute move; down-moves weighted; buy share relative to the venue's own normal; thinning needs ratio **and** z-score; warming venues contribute nothing; low activity blocks flow flags |
| Events | `test_events` (6) | score-crossing hysteresis; debounced confirmation counts; breakout precursors; cross-venue count; status escalation; leader detection |
| Config | `test_config` (4) | v0.6 config understood; an existing `config.json` is never overwritten; created only when missing; bad JSON falls back |
| Storage | `test_storage` (7) | migrations and WAL; writer batching and drop counter; 7-day history keeps spikes (max-preserving); pruning; v0.6 import (see B); minute roundtrip and rehydration; outcome filling |
| HTTP servers | `test_devserver` (2); `test_api_fastapi` (1, CI) | every REST route; static files and error codes; FastAPI routes plus the **WebSocket** topics `top` / `coin:X` / `health` with a real FastAPI TestClient |
| Real ccxt | `test_ccxt_real` (2, CI) | all 15 configured exchange ids exist in ccxt 4.5.84 and resolve; websocket clients construct without network |
| Tooling | `print_capabilities.py` (CI, real ccxt) | capability matrix from the installed ccxt: multi-symbol book + trades on Binance, OKX, Bybit, Coinbase, Kraken, KuCoin, Bitget, Crypto.com; per-symbol book with multi-symbol trades on Gate and Upbit; per-symbol on MEXC, HTX, Bitfinex, Bitstamp, Bitrue |
| Web UI | ESLint, `node --check`, Playwright smoke test | table rows and all required columns; wallet columns render **N/A**; search filter; coin view (venue table, score pipeline, wallet panel, charts, 1h / 24h / 7d ranges); Health and Universe views; **no horizontal scroll at 390 px**; no JS errors or HTTP errors |
| v0.6 integrity | `sha256sum -c` (local + CI) | every file of the original v0.6 folder byte-identical to the uploaded ZIP |

## B — executed successfully with simulations / fixtures (52 tests + tools)

| Area | Tests | Synthetic input | What is proven |
|---|---|---|---|
| Pre-move scenarios | `test_scenarios` (10) | scripted 4-venue markets fed through the full engine | a calm market stays quiet; **Case A** ($70 in 2 trades) → LOW CONFIDENCE, score < 15; **Case B** (asks thinning on 4 venues, flat price) → EMERGING at ~4 min, then CONFIRMED / STRONG at 6–7 min, score ~72; EMERGING does not wait for the full slow median; single-venue anomaly capped; stale venues cannot score; one large print does not rank; **Case C** (pump) → MOVE IN PROGRESS then LATE, capped at 25; vol-normalised LATE below the 5 % hard line on a calm asset; warm-up suppresses scores |
| Universe and venues | `test_universe` (14) | CoinGecko `/coins/markets` and exchange catalogs in the real response format (`tests/fixtures/`) | **XDC never gets Binance**; ranking by the coin's own volume; ticker collision rejected; wide spread rejected; KRW + FX; unavailable market promotes the next; replacement hysteresis; unsupported top markets reported; stable / wrapped exclusions; **back-fill to 100 usable coins plus pinned extras**; membership hysteresis; 429 back-off; token bucket |
| Feed manager | `test_feeds` (10) | SIM stream client; a fake `ccxt.pro` module | multi-symbol only when policy and ccxt agree; error classification; partitioning and diffing; reconnect with resync; a bad symbol is isolated; multi-symbol chunk falls back to per-symbol; circuit breaker and per-exchange isolation; **watchdog restarts a silent book stream**; SIM world through the manager; the ccxt adapter code path |
| Wallet intelligence | `test_intel` (14) | fake Etherscan responses; labelled transfers | classification matrix (Coinbase Hot → Prime = SHIFT; CEX withdrawal ≠ buy); CSV labels; **null semantics** (N/A vs covered-but-quiet 0); QNT / Wintermute routing; simultaneous accumulation and distribution; real supply drain vs returned tokens; partial-coverage damping; address-centric polling; symbol mismatch invalidates a token; lagging addresses; token-wide auto-disable; Etherscan errors and budget |
| Engine hosts | `test_host` (2) | SIM markets | in-process host streams and rewires; **worker processes** (spawn) deliver feature rows |
| Service end-to-end | `test_service` (2) | SIM world, fixtures | universe → venues → feeds → engine → storage → payloads; LATE ranks below early signals |
| v0.6 import | inside `test_storage` | a DB built with the exact v0.6 schema | source opened read-only, hash unchanged, idempotent re-import, merged history |
| Self-test tool | `selftest.py --sim` (local + all CI jobs) | SIM exchanges | the live self-test's streaming path works end to end |
| Performance | `tools/bench_engine.py` | 400 synthetic markets, 1 600 book updates/s, ~580 trades/s | ingestion ≈ 10 % of one core; tick ≈ 48 ms/s (worst 80 ms); ≈ 213 MB including 24 h rings. v0.6 on the same load: ~1.9 s per 1 s tick, ~4.2 GB. GC tuning cut GC time from 11.5 % to 0.3 % |
| SIM demo | 30-minute run of the stdlib dev server with the web UI | 12 synthetic coins × 4 SIM venues; warm-up shortened to 2 minutes and the scenario cycle to 5 minutes so it fits the run | ALPHA went NORMAL → WATCH → EMERGING (ask depth −47 % on 4 / 4 venues, leader and followers shown) while its price stayed flat |

## C — live tests still requiring your Windows machine

Run `run_windows.bat selftest` once (2–4 minutes) and send `data\selftest_report.md` back if anything is
WARN / FAIL. It performs:

1. Python and package check **on your install** (CI proves the packages install on Windows, not on your PC).
2. Your `config.json` loads, including what was mapped from v0.6.
3. SQLite WAL on your disk.
4. **CoinGecko live**: top 500 and the exclusion categories (rate limits without / with a Demo key).
5. **Exchange catalogs live**: `load_markets` + `fetch_tickers` on all 15 exchanges from your network
   (some exchanges geo-block certain countries).
6. Capability matrix with your installed ccxt version.
7. **Real Top-100 universe** build (back-fill, exclusions, pinned).
8. **Real per-coin venue selection** for QNT, XDC, LINK, BTC, ETH, SOL, HBAR (XDC must not get Binance).
9. **Real websockets**: streams the selected markets through the real feed manager and engine for 90 s.
   Reports books / trades per market, first-book latency and errors. This is the check of the
   per-exchange limits that could not be verified offline.
10. Etherscan (only with `ETHERSCAN_API_KEY` and `intel.enabled`).

Not covered by the self-test, to observe in normal use:

* A **multi-hour live run** of the full Top 100: warm-up (~30 min), then real message rates, CPU (ccxt's
  own parsing was not measurable offline), memory, database growth and reconnect behaviour.
* **Import of your real v0.6 `scanner.db`**: `run_windows.bat import "C:\...\scanner.db"`. Tested only
  on a synthetic v0.6-schema database.
* Phone access over the LAN (`http://YOUR-PC-IP:8000`) and the Windows Firewall prompt.
* Calibration: score thresholds are initial values. After a few days, `/api/outcomes` shows the forward
  returns after each signal.
