# Pre-Move Scanner v0.8.0 — test report

This report covers what was tested for v0.8.0 ("Multi-chain wallet intelligence + manual assets"), how, the
results, and what was **not** verified and still needs your machine. Nothing here claims more than was run.

## Summary

| Area | How | Result |
|---|---|---|
| Automated suite | 288 tests (220 in v0.7.4; 68 new), Python 3.11 / 3.12 / 3.13 on Ubuntu and Windows (GitHub Actions `CI`) | all pass; 1 skip by design (full-bootstrap test, run as its own CI step) |
| Same suite, cloud container | Python 3.12 without PyPI access (no fastapi / ccxt / aiohttp) | 288 run, all pass; 8 skipped because those packages are missing |
| Web UI | `node --check`, ESLint, Playwright smoke test in SIM mode (desktop and 390 px phone) | all pass; the manual-asset flow was clicked through: search → candidates → preview → add → remove |
| Release ZIP | `tools/make_release.py` build + `--verify` (CI Ubuntu and Windows, and the cloud container) | **CLEAN**; 148 files at the final commit; no `data/`, `config.json`, `.venv/`, database files, caches or secrets |
| Clean install | release ZIP unpacked into an empty folder, real venv + `pip install`, app started (CI, Python 3.12 on Ubuntu and on Windows) | pass |
| Windows launcher | `run_windows.bat` end-to-end on `windows-latest`: venv, pip, bootstrap, version, comma arguments, exit code, `release` | pass; an existing `config.json` was **not** overwritten |
| v0.6 history folder | `sha256sum -c v06_manifest.sha256` (CI on every push, and locally) | 20/20 files unchanged |
| Upgrade from v0.7.x | a v0.7.3-shaped schema-2 database with rows in every table (`tests/test_upgrade_v08.py`) | every row kept, backup written first, idempotent, atomic |
| Downgrade | v0.7.4 source (commit `89083a3`) started on a v0.8-migrated database | runs; history and alerts intact |
| Live wallet providers | `tools/wallet_check.py` on GitHub Ubuntu and Windows runners (real public APIs) | see [Live validation](#live-validation-real-apis) |
| Live universe / manual assets | `tools/live_onboarding_check.py` on GitHub runners (real CoinGecko and exchanges) | see [Live validation](#live-validation-real-apis) |
| **Not verified** | Etherscan V2 (all EVM chains **and XDC**) live; Binance / Bybit from US runners; multi-hour runs; your machine | needs your key and your network (commands at the end) |

## Automated tests

`python -m unittest discover -s tests -t .` (Windows: `run_windows.bat test`).

### New in v0.8.0

| File | Tests | What it proves |
|---|---|---|
| `test_providers.py` | 22 | Every provider against recorded API shapes:<br>• **Bitcoin (Esplora):** UTXO spend / change / receive; paging stops at the cursor; mempool.space → blockstream.info fallback; balance.<br>• **XRPL:** `delivered_amount` (partial payments never overstated); issued tokens; forward paging; `tooBusy` fail-over and back-off.<br>• **TRON:** TRC-20 and native TRX; hex → base58check; optional key header.<br>• **Solana:** native SOL and SPL from balance deltas; transaction-version retry; private RPC URL only from the environment; rate limit.<br>• **Hedera:** fee accounts, payer fee, HTS tokens.<br>• **Cardano:** stake identity, UTXO, native tokens.<br>• **EVM:** missing key → NO KEY; a chain outside the plan is reported; proxy module; XDC chain id 50.<br>• **Budget:** the daily cap is never exceeded; temporary failures and rate limits recover.<br>• **Monitor:** normalised events from a non-EVM chain; priority tiers space out quiet assets; legacy rows re-classified; an unsupported chain is never polled. |
| `test_manual_assets.py` | 8 | Add → duplicate refused → remove → persist across restart. Flipping the manual flag never changes the ranking. An ambiguous ticker is never resolved (HTTP 409); an asset with no venue is kept. 100 Top + manual assets outside the top make one de-duplicated universe. A ticker collision with a Top member is reported. Config seeding happens once, so a removed asset is not re-added. CoinGecko unreachable at first start → retried after 2, 4, 8 … minutes. |
| `test_upgrade_v08.py` | 5 | In-place migration 2 → 3 keeps every row in every table and writes a backup first. It is idempotent, and an interrupted migration leaves the database untouched. The service runs on the upgraded database with its history. A v0.7 `config.json` keeps working and is never rewritten. `app_meta` records the version and the upgrade history. |
| `test_radar_v08.py` | 8 | Wallet evidence from classified multi-chain events reaches the radar as supportive / neutral / unavailable / contradictory. Wallet data is never mandatory. Supportive evidence lowers the bar and contradictory vetoes. Internal, custody and MM routing never count as buying, and neither does unattributed CEX outflow. A degraded or unsupported wallet is *unavailable*, not zero. Manual and non-EVM assets are assessed identically. Labels, check counts, highlights and the note "not a probability" are present; "guaranteed" and "100% buy" never appear. |
| `test_release.py` | 4 | The real release is clean. The verifier rejects every prohibited file: `data/`, `config.json`, `.venv/`, `*.db` / `-wal` / `-shm`, SQLite content under another name, caches, `.env`, reports. API-key values present in the environment never ship. `.gitignore` keeps runtime state out of the repository. |
| `test_bootstrap.py` | 7 | Source-only unpack; bootstrap creates `config.json` (only if missing), `data/` and the venv. A fresh database appears only on a new install. The app starts from the unpacked folder. The full venv + pip install runs as its own CI step. The version is read without importing the app. An existing `config.json` is never overwritten. |
| `test_version.py` | 4 | One definition (`server/__init__.py` = 0.8.0). The UI, launchers, API, service worker and release name all report it. There are no hard-coded versions in the UI or the launchers. |
| `test_env_secrets.py` | +2 | `config.example.json` contains only environment-variable **names**. Keys are loaded but never exposed in payloads, Health, logs or errors (scrubbed). |
| `test_devserver.py`, `test_api_fastapi.py` | +1, extended | The v0.8 asset and wallet routes on both servers. |
| `test_intel.py` | +5 | Attribution and direction; distribution is an inflow; internal and custody moves never count; native ETH from `txlist`; taxonomy aliases (`MM`, `PROTOCOL_TREASURY`). |
| `test_wallet_status.py` | rewritten (27) | The eight states (OFF, NO KEY, DISCOVERING, WARMING, ACTIVE, UNSUPPORTED, DEGRADED, N/A) with reasons. A real zero is not N/A. The registry covers native assets, tokens, non-EVM tokens and bridged copies, NEEDS_VERIFICATION, NOT_FOUND and overrides. Lookups are paced and cached (TTL). Providers are built from environment-variable names only. |
| `tests/ui/smoke.mjs` | extended | Version in header / footer / title equals `/api/version`. The manual-only filter. Health provider rows for all eight chain groups, the global state counts and the "provider not implemented" reason. The manual-asset flow end to end. |

### v0.7.4 test assertions that changed, and why

Each change is required by the v0.8 specification. No assertion was relaxed to make code pass.

| Test | v0.7.4 expected | v0.8.0 expects | Why |
|---|---|---|---|
| `test_config` (v0.6 mapping) | `universe.pinned_assets` | `universe.manual_assets` | Spec 1: "rename pinned_assets → manual_assets"; the old key is still read. |
| `test_signal_radar` (labels) | one label `WATCH / CONFIRMING` | separate `WATCH` and `CONFIRMING` | Spec 11: the radar states are listed separately. |
| `test_storage` (migrations) | `[1, 2]` | `[1, 2, 3]` | Migration 3 (spec 3, 15). |
| `tests/ui/smoke.mjs` (radar labels) | 4 labels | 5 labels | Same as `test_signal_radar`. |
| `test_intel` (classifier) | v0.7 classes: `SHIFT`, `ACCUMULATION-SIDE`, `BUY` / `SELL`, `UNKNOWN` | v0.8 event types: `INTERNAL_SHIFT`, `MM_ROUTING`, `CUSTODY_SHIFT`, `ACCUMULATION`, `DISTRIBUTION`, `CEX_OUT`, `CEX_IN`, `DEX_FLOW`, `BRIDGE`, `UNKNOWN_TRANSFER` | Spec 9 defines the event types. **One real behaviour change:** exchange → institutional custody (Coinbase → Anchorage, Coinbase Hot → Prime) is now `CUSTODY_SHIFT`, because the spec says "custody … never buying". Holder ↔ holder is `UNKNOWN_TRANSFER`, and DEX swaps are `DEX_FLOW` (with direction), never a confirmed BUY. |
| `test_wallet_status` | native coins (BTC, ETH, XRP, …) and non-EVM chains → `UNSUPPORTED` | native coins and the non-EVM chains with a provider are supported; only chains without a provider are `UNSUPPORTED · provider not implemented` | Spec 5 and 7: support the chains and native assets. `OK` / `ON` became `ACTIVE`, and a pending lookup is `DISCOVERING` (spec 10). |

## CI (GitHub Actions)

Workflow `CI` (`.github/workflows/ci.yml`) runs on every push:
* 6 jobs: Python 3.11 / 3.12 / 3.13 × Ubuntu / Windows.
  * Each runs the syntax check, the full suite (with FastAPI, WebSocket, real ccxt and worker processes),
    the exchange capability matrix from the installed ccxt, `selftest --sim`, and the clean release ZIP
    build and verify.
  * The Python 3.12 jobs also run the clean install from that ZIP with a real venv and `pip install`.
  * The Windows 3.12 job also runs `run_windows.bat` end-to-end and the "never overwrites config.json"
    check.
* the web UI job: `node --check`, ESLint, Playwright;
* the v0.6 integrity job.

Results:
* **Run 15** (`48b17dc`): all 8 jobs green.
  * Ubuntu, Python 3.12: `Ran 286 tests … OK (skipped=1)`; the full-bootstrap step `Ran 7 tests … OK`.
  * The release step printed "release: dist/pre_move_scanner_v0.8.0.zip (147 files, 0.58 MB) — verified:
    no data/, config.json, .venv/, database files, caches or secrets".
  * The Windows `run_windows.bat release` printed `CLEAN`.
* **Run 16** (`ea4b9d3`): all 8 jobs green.
* **Runs 17–19** (`d5d5814`, `02e150a`, `c325388`; these include the universe-retry fix and `app_meta`):
  all 8 jobs green each time.
  * Run 19, Ubuntu, Python 3.12: `Ran 288 tests … OK (skipped=1)`; the full-bootstrap step `Ran 7 tests …
    OK`.
  * Release: 148 files, 0.61 MB, verified.
* The final commit of this report is checked by CI again.

The release ZIP built by CI is attached to each run as the artifact `release-zip`.

## Live validation (real APIs)

Workflow `v0.8 live validation (real APIs)` (`.github/workflows/live-wallet.yml`), on GitHub's Ubuntu and
Windows runners. No repository secrets were set, so **no Etherscan and no CoinGecko key** was used.

### Wallet providers and metadata discovery (`tools/wallet_check.py`)

Run 3 (`48b17dc`), with no keys:

| Check | Ubuntu | Windows |
|---|---|---|
| CoinGecko metadata decisions: QNT, LINK, UNI, AAVE → Ethereum token + contract; XDC → native XDC Network; BTC, ETH, BNB, XRP, TRX, SOL, HBAR, ADA → native coin of their chain; JUP → Solana SPL token; SUI → UNSUPPORTED "provider not implemented" | 15/15 PASS | 15/15 PASS |
| ERC-20 `symbol()` on chain (publicnode) for QNT, LINK, UNI, AAVE | 4/4 match | 4/4 match |
| Bitcoin (Esplora): probe, sample, first poll (51 transfers), cursor poll, balance | PASS | PASS |
| XRPL (rippled): probe, first poll (200 transfers), cursor poll, balance | PASS, after one `tooBusy` fail-over | PASS |
| TRON (TronGrid): TRX and TRC-20 (USDT), first poll, cursor poll, balance | PASS | PASS |
| Solana (public RPC): SPL token and SOL, first poll, cursor poll, balance | PASS, after one HTTP 429 back-off | SPL PASS; SOL **FAIL**: HTTP 429 persisted after one retry |
| Hedera (mirror node): probe, first poll (100 transfers), cursor poll, balance | PASS | PASS |
| Cardano (Koios): stake / payment address, first poll, cursor poll, balance | PASS | PASS |
| Etherscan V2 (EVM + XDC) | **skipped: no key** | **skipped: no key** |
| Totals | 61 checks, 0 FAIL, 5 WARN | 57 checks, 1 FAIL, 4 WARN |

The Windows Solana FAIL was the public endpoint refusing the shared runner IP, not a parsing or
normalisation error. The provider backed off as designed. `wallet_check.py` now retries twice with longer
waits and reports a check that stays refused as **NOT VERIFIED (WARN)**.

Runs 5 and 6 (`d5d5814`, `c325388`), with the retry handling, finished with **0 FAIL in all four jobs**:

| Run | Ubuntu | Windows |
|---|---|---|
| 5 | 58 checks, 0 FAIL, 8 WARN | 58 checks, 0 FAIL, 6 WARN |
| 6 | 59 checks, 0 FAIL, 8 WARN | 58 checks, 0 FAIL, 7 WARN |

* CoinGecko discovery was 15/15 again, and the ERC-20 symbols 4/4.
* Bitcoin, XRPL (one `tooBusy` retry in some runs), TRON, Hedera and Cardano passed.
* **Solana SOL** was NOT VERIFIED in all four jobs: the public RPC still answered HTTP 429 after two
  retries. SOL passed on Ubuntu in run 3.
* **Solana SPL** passed on Ubuntu in run 6 (28 transfers) and in run 3. In two jobs the tool had picked
  the wrapped-SOL mint as its sample. Those token accounts are short-lived, so the poll found 0 transfers
  (WARN). The sample picker now skips wrapped SOL.
* For real Solana coverage, set `SOLANA_RPC_URL` to a private RPC (see KNOWN_LIMITATIONS).

What each provider row means:
* **probe:** chain tip reached.
* **sample:** a real, active address picked from the chain's latest data. No wallet identity is
  hard-coded or invented.
* **first poll:** transfers normalised into `RawTransfer` rows.
* **second poll:** the cursor from the first poll works, and overlaps are de-duplicated.
* **balance:** the balance call works.

Seen live and handled:
* The public XRPL server answered `tooBusy`: the provider failed over to the next server and backed off.
* The public Solana RPC throttled with HTTP 429: the provider backed off. On one Windows run, the
  endpoint kept refusing after the tool's retry. That check is reported as **NOT VERIFIED** (v0.8.0
  final), never as a pass.
* Earlier runs found three bugs that are now fixed:
  * Solana transaction version 1 was not accepted;
  * Windows console encoding broke the report;
  * the Hedera sample picked system accounts.

### Universe, manual assets, registry, radar (`tools/live_onboarding_check.py`)

The check starts the real service against CoinGecko and 15 exchanges, using its own database. It covers:
* Top-20 plus the manual assets QNT, LINK and XDC;
* picking a coin ranked 251–500 with a usable market, then search → preview → add at runtime →
  streaming;
* registry discovery and the wallet states;
* ranking and radar;
* a restart on the same database;
* removal with history kept.

First run (`ea4b9d3`):
* **Windows:**
  * the real service started with 13/15 exchange catalogs (Binance and Bybit refuse US runners);
  * **20 Top assets + 2 manual assets**; QNT, LINK and XDC were resolved by CoinGecko id;
  * LINK is inside the Top 20, so it was marked manual and **not duplicated**;
  * universe = 20 + 2 = 22 monitored — PASS.

  The script then made an extra CoinGecko call of its own, which CoinGecko refused with HTTP 403, and the
  script crashed.
* **Ubuntu:**
  * CoinGecko refused the runner (HTTP 403) from the first call;
  * the service still monitored the three manual assets, and every universe check passed except the
    ranking;
  * this exposed a real gap: without a cache, the ranking was retried only at the next hourly refresh.

  Fixed: it is now retried after 2, 4, 8, … minutes (covered by a unit test). The script now takes its
  candidate from the ranking the service already fetched, and reports refusals as NOT VERIFIED.

Runs 5 and 6 (`d5d5814`, `c325388`). In run 6 the onboarding jobs ran alone, after the wallet checks, one
OS at a time. CoinGecko refused `/coins/markets` (HTTP 403) for the **whole** run on both runners, even
after 2 + 4 + 8 minutes of retries. CoinGecko's `/coins/{id}` answered normally from the same runners
minutes earlier.
* **Verified live (22 checks, 0 FAIL):**
  * the manual assets QNT, LINK and XDC are monitored with their CoinGecko ids, even with no ranking;
  * metadata: QNT and LINK are Ethereum tokens with their contracts (config overrides), and XDC is the
    native coin of XDC Network;
  * wallet states: NO KEY for Ethereum and XDC, each with a reason; the other providers are OK;
  * ranking by score: manual assets are not pinned;
  * the radar state is NO HIGH-CONVICTION SETUP, with the "not a probability" note;
  * a restart on the same database: schema 3, not a new install, `app_meta` previous version 0.8.0,
    registry cached.
* **NOT VERIFIED live:**
  * the Top-N composition in these runs (it was verified in run 4 on Windows, see above);
  * adding a rank 251–500 coin at runtime against real exchanges;
  * removal of it with its history kept.

  These paths are covered by `tests/test_manual_assets.py` (real service in SIM mode, CoinGecko calls
  faked) and by the Playwright flow. Run the live check once on your PC (command below); your home
  connection is not a shared cloud IP.

## Upgrade, downgrade, data safety

* **Migration 3** (`tests/test_upgrade_v08.py`, `tests/test_storage.py`):
  * row counts are identical before and after for every table;
  * the 1-minute rows needed for baseline rehydration are intact;
  * v0.7 cursors and budget counters are still read;
  * the v0.7.3 `token_contracts` cache moves into `asset_registry`;
  * the backup file holds the pre-migration schema (2) and all rows;
  * a second start neither re-migrates nor re-backs up;
  * a failure injected after migration 3's statements, before the commit, rolls everything back: the
    schema stays at 2 and no new table exists. The next start then completes the migration.
* **Downgrade.** The v0.7.4 source was started on a copy migrated by v0.8.0. It reported version 0.7.4,
  applied no migrations, and read 120 history rows and 1 alert. v0.7 ignores the new tables and columns.
* **`config.json`:**
  * never rewritten (bootstrap test, Windows CI step, upgrade test);
  * a v0.7 config with `pinned_assets` and `intel.tokens` works unchanged.
* **Secrets:**
  * `config.example.json` holds only variable names;
  * provider errors are scrubbed of keys and private RPC URLs;
  * the release builder refuses files that contain a set key's value or a key-shaped string;
  * tested with fake keys that are generated at runtime, so no literal key exists in the source.

## Not verified (needs your key or your machine)

* **Etherscan V2: all EVM chains and XDC, live.** Not run, because the runners had no `ETHERSCAN_API_KEY`.
  The code paths are covered by the recorded-response tests (`test_providers.py`). XDC through
  Etherscan V2 (chain id 50) depends on your plan; if it is not included, Health shows DEGRADED with
  Etherscan's message.
* **CoinGecko with a key.** Only the keyless public API was used.
* **Binance and Bybit** refuse US data centres (HTTP 451 / 403), as in v0.7. Test them from your network
  with `selftest`.
* **Scores on real labelled wallets.** The repository ships only one example label. Wallet scores need
  your trusted labels per chain; without them the state is N/A. The live checks verify data collection
  and normalisation, not the scores on real exchange wallets.
* **A live HIGH-CONVICTION alert** has still not been observed. It is tested with fixtures and in SIM.
* **Multi-hour runs:** full Top-100 feed load, memory and database growth over days, provider budgets
  over a whole UTC day.

## Run it on your Windows PC

```bat
cd C:\path\to\pre_move_scanner_v07_work
set PMS_NO_PAUSE=1
run_windows.bat test
run_windows.bat selftest --repeat 3
setx ETHERSCAN_API_KEY "your-key"
rem  open a NEW command prompt so the key is visible, then:
run_windows.bat walletcheck
run_windows.bat walletcheck --only discovery,evm
run_windows.bat release
.venv\Scripts\python.exe tools\make_release.py --verify dist\pre_move_scanner_v0.8.0.zip
.venv\Scripts\python.exe tools\live_onboarding_check.py --top 20 --seconds 90
run_windows.bat
```

Send back:
* `data\selftest_report.md`;
* `data\wallet_check_report.md`;
* `data\live_onboarding_report.md`;
* screenshots of **Health**: Wallet providers and Storage (it must say "upgraded from schema 2" and name
  the backup).
