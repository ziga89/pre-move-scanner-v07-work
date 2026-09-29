# Setup, Windows startup, clean releases and migration (v0.8.0)

## Requirements
* Windows 10/11, macOS or Linux; **Python 3.11 – 3.13** (tested in CI on Windows and Ubuntu).
* Internet access to CoinGecko and the exchanges' public REST + WebSocket endpoints. Wallet intelligence
  (optional) also uses Etherscan and the public chain APIs listed under *API keys*.
* Dependencies (`requirements.txt`): fastapi, uvicorn[standard], httpx, ccxt and protobuf. All are
  pure-Python wheels on Windows, so no compiler is needed (v0.6's cryptofeed could need one).

## Windows — first start
1. Install Python from <https://www.python.org/downloads/> and tick **Add python.exe to PATH**.
2. Unzip the release ZIP (`pre_move_scanner_v0.8.0.zip`) anywhere. Keep your v0.6 folder where it is: it
   is not needed and is never modified. **Upgrading from v0.7.x?** Unzip over your existing folder instead
   and read [MIGRATION_V080.md](MIGRATION_V080.md).
3. Double-click **`run_windows.bat`**. It:
   * creates `.venv` and installs the dependencies;
   * runs `tools\bootstrap.py`, which creates `config.json` from `config.example.json` **only if missing**
     and creates `data\` (an existing database is never replaced);
   * shows the version from `server\__init__.py`;
   * starts the scanner on <http://127.0.0.1:8000>.
4. Recommended once: open a command prompt in the folder and run `run_windows.bat selftest`.
   It checks the universe, per-coin venues (incl. that XDC does not get Binance), streams the selected
   markets for 90 s and writes **`data\selftest_report.md`**. Please send that file back if anything fails.

| Command | Purpose |
|---|---|
| `run_windows.bat` | live scanner, port 8000 (LAN devices: `http://YOUR-PC-IP:8000`) |
| `run_windows.bat selftest [--assets QNT,XDC,LINK] [--seconds 120]` | live self-test + report |
| `run_windows.bat selftest --repeat 3` | three streaming rounds (fresh feed manager each). A market PASSes only with zero feed incidents |
| `run_windows.bat selftest --assets QNT,XDC,LINK --exchanges kucoin --repeat 3` | stream only one exchange's markets (adds each asset's best pair there) |
| `run_windows.bat stress [--exchange kucoin] [--assets QNT,XDC,LINK] [--cycles 20]` | live start / resubscribe / stop-start / socket-drop / rebuild stress → `data\feed_stress_<exchange>.md` |
| `run_windows.bat sim` | offline demo with synthetic markets (port 8001, separate `data\scanner_sim.db`) |
| `run_windows.bat import "C:\...\scanner.db"` | read-only import of v0.6 history |
| `run_windows.bat walletcheck [--only discovery,evm,bitcoin,xrpl,tron,solana,hedera,cardano]` | live wallet-provider check: CoinGecko discovery, ERC-20 `symbol()` on chain, and each provider (probe, two polls with cursor, balance) → `data\wallet_check_report.md` |
| `run_windows.bat test` | automated test-suite |
| `run_windows.bat release [--out DIR]` | clean source ZIP → `dist\pre_move_scanner_v<version>.zip` (see *Clean release ZIPs*) |

Set `PMS_NO_PAUSE=1` to skip the "press any key" prompts. Stop with **Ctrl+C**. Windows Firewall may ask to allow Python on private networks (needed only for phones).

## macOS / Linux
`./run.sh [selftest|walletcheck|stress|sim|import|test|release]` does the same steps as `run_windows.bat`. By
hand:
```bash
cd pre_move_scanner_v07_work
python3 tools/bootstrap.py --venv          # .venv + requirements + config.json only if missing + data/
. .venv/bin/activate
python tools/selftest.py
uvicorn server.app:app --host 0.0.0.0 --port 8000
# offline demo:  PMS_MODE=sim uvicorn server.app:app --port 8001
# no FastAPI available: python -m server.devserver --sim   (stdlib server, polling UI)
```

## API keys (optional): environment variables only
`config.json` holds only the **names** of environment variables (`etherscan_api_key_env`,
`coingecko_api_key_env`, `intel.providers.*.api_key_env` / `rpc_url_env`), never a key. Keys are never
written to the config, the database, logs, Health or release ZIPs; error texts are scrubbed before they are
shown.

| Variable | Used for | Needed? |
|---|---|---|
| `COINGECKO_API_KEY` | CoinGecko ranking, search, coin details (set `coingecko_api_key_type` to `pro` for a paid key) | optional, raises rate limits |
| `ETHERSCAN_API_KEY` | wallet intelligence on EVM chains and XDC (Etherscan V2) | required for EVM / XDC |
| `TRONGRID_API_KEY` | TRON (TronGrid) | optional, raises rate limits |
| `SOLANA_RPC_URL` | a private Solana RPC URL (Helius, QuickNode, ...) instead of the public endpoint | optional, recommended |
| `KOIOS_API_TOKEN` | Cardano (Koios) | optional |

Windows: `setx ETHERSCAN_API_KEY "your-key"`, then open a new command prompt. The scanner also reads keys
set in the user environment of the registry, so a scanner started from Explorer finds them.

* Wallet intelligence stays **OFF** until `"intel": {"enabled": true}`.
* After that, Bitcoin, XRPL, TRON, Solana, Hedera and Cardano work without any key. EVM / XDC assets show
  **NO KEY** until `ETHERSCAN_API_KEY` is set.
* Chain and contract metadata is discovered automatically through CoinGecko: 2 lookups per minute, cached
  for 30 days (`assets.*`). Assets show **DISCOVERING** until their lookup is done.
* Scores still need reliable labelled addresses for the chain (see *Wallet labels*). Otherwise the state
  is **N/A** with the reason.

## Upgrading from v0.7.x to v0.8.0
See [MIGRATION_V080.md](MIGRATION_V080.md): unzip over the folder and start as usual. The database is backed
up to `data\backups\` and migrated in place (migration 3, additive). `config.json` keeps working
(`pinned_assets` → `manual_assets`).

## Clean release ZIPs
`run_windows.bat release` (or `python tools/make_release.py --out dist`) builds a source-only ZIP from an
allow-list:
* included: `server/`, `web/`, `tools/`, `tests/`, `labels/`, `docs/`, `ios/`, the launchers, the
  requirements, `config.example.json`, README and CHANGELOG, plus a `RELEASE_MANIFEST.txt`;
* never included:
  * `data/`, `config.json`, `.venv/`, `dist/`, `backups/`;
  * `*.db`, `*.db-wal`, `*.db-shm`, `*.sqlite`;
  * caches, `__pycache__`, `.env`, `*.pem` / `*.key`;
  * the self-test / wallet-check reports.

The builder also refuses:
* any file with an SQLite header;
* any file that contains the value of a secret environment variable that is set, or a key-shaped string.

Check any ZIP with `python tools/make_release.py --verify path\to\file.zip`, which prints `CLEAN` or the
problems and exits non-zero. `tests/test_release.py` runs the same checks in CI and fails if a prohibited
file would be packaged.

## Upgrading from v0.7.1 / v0.7.2 to v0.7.3 (history)
* Copy nothing: v0.7.3 uses the same folder layout, `config.json` and `data/scanner_v07.db`.
* The database is upgraded automatically on the first start. Migration 2 adds the `alerts` and
  `token_contracts` tables; no existing table is altered and no data is removed. There is no downgrade
  step: v0.7.2 ignores the two new tables.
* New optional config keys (defaults in `server/config.py`):
  * `alerts.watch_min_premove`, `watch_min_confirmed_venues`, `watch_min_orderbook`,
    `watch_linger_seconds`, `invalidated_display_seconds`, `invalidated_headline_seconds`,
    `persist_update_seconds`;
  * `storage.alerts_days`;
  * `intel.warmup_minutes`, `auto_discover_contracts`, `discovery_calls_per_minute`,
    `discovery_ttl_days`, `discovered_token_wide`, `whale_candidate_usd`.
* A high-conviction alert that was open when the scanner stopped is closed at the next start ("scanner
  restarted - setup not re-verified after restart"). It is never resumed unverified.

## Migration from v0.6

| Topic | v0.6 | v0.7 |
|---|---|---|
| Folder | `pre_move_scanner/` (v0.6) | `pre_move_scanner_v07_work/` — separate; v0.6 untouched |
| Coins | `watchlist` in config | automatic Top-100 universe; old `watchlist` becomes **manual assets** (v0.7: pinned) |
| Config | overwritten on each start by `run_windows.bat` | created only if missing; old keys mapped (see below) |
| Feed library | cryptofeed | ccxt (`pip install -r requirements.txt` in the new folder) |
| Database | `scanner.db` in the v0.6 folder | `data/scanner_v07.db` (new schema); v0.6 DB importable read-only |
| Score | one composite score | Pre-Move score + sub-scores + status; v0.6 scores shown as "v0.6" in charts |

**Config keys still understood**: `watchlist` → `universe.manual_assets`; `coingecko_ids` →
`universe.coingecko_ids`; `venue_discovery.max_venues_per_asset` → `discovery.max_venues_per_asset`;
`baseline_minutes` → `engine.baseline_minutes`; `warmup_minutes` → `engine.min_baseline_minutes` (minus the
10-minute lag); `sample_interval_seconds` / `persist_interval_seconds` → broadcast / persistence cadence;
`onchain` → `intel` (watch wallets become labels: Wintermute → MM/MEDIUM, others → WATCH/LOW).
You can copy your v0.6 `config.json` into the v0.7 folder. The Health page (Config rows) lists what was mapped.

**History**: `run_windows.bat import "C:\path\to\pre_move_scanner\scanner.db"`. The v0.6 file is opened with
SQLite `mode=ro`, hashed before and after (the import aborts if it changed) and copied into same-named
legacy tables in the v0.7 database. Re-running later only adds new rows. The coin charts then show the v0.6
period as dashed series with the label "v0.6", because the score formula changed.

**Database schema (new tables, versioned in `schema_migrations`)**: `asset_metrics_5s`,
`market_metrics_10s`, `asset_metrics_1m`, `market_metrics_1m`, `events`, `universe_snapshots`,
`venue_selection`, `feed_health_1m`, `wallet_labels`, `onchain_transfers`, `wallet_balances`,
`intel_cursors`, `signal_outcomes`, `legacy_import`, since v0.7.3 `alerts` and `token_contracts`, and since
v0.8.0 `asset_registry`, `manual_assets` and `app_meta`; plus the import targets `composite_history_v2`,
`venue_history_v2`, `scanner_events_v2` (same columns as v0.6, with uniqueness indexes). No v0.6 table is
altered or dropped.

**Default retention** (`storage` section): 5 s asset rows 48 h, 10 s venue rows 24 h, 1-minute asset
rollups 30 days, 1-minute venue rollups 14 days, events 90 days, high-conviction alerts 365 days. Estimated steady-state size at Top 100 is
about 1–1.5 GB (a calculation, not yet measured on a live run).

## Wallet labels
Edit `labels/wallet_labels.csv` (`chain,address,entity,entity_type,confidence,source,notes`). Only add
labels you trust.
* `chain`: `ethereum`, `bsc`, `base`, `arbitrum`, `optimism`, `polygon`, `avalanche`, `mantle`, `linea`,
  `scroll`, `blast`, `xdc`, `bitcoin`, `solana`, `xrpl`, `tron`, `hedera` or `cardano`.
* `address`: in the chain's own format. Invalid addresses are rejected and counted as label errors on the Health page. For
  exchange wallets on Cardano, use stake addresses (`stake1…`).
* `entity_type`: `CEX_HOT`, `CEX_COLD`, `CEX_DEPOSIT`, `CEX_CUSTODY`, `CUSTODY_INSTITUTIONAL`,
  `MARKET_MAKER`, `DEX_POOL`, `DEX_ROUTER`, `BRIDGE`, `TREASURY`, `WHALE`, `WHALE_CANDIDATE`, `UNKNOWN` or
  `WATCH`. The v0.7 names `MM` and `PROTOCOL_TREASURY` are still accepted.
* LOW-confidence rows and the types `WATCH`, `WHALE_CANDIDATE` and `UNKNOWN` are shown but never drive
  classification or scores.
Assets to monitor come from the asset registry: every universe asset whose chain has a provider and whose
metadata is READY. Contract symbols are verified on the first transfer. Overrides (`assets.overrides`,
`intel.tokens` or `PUT /api/assets/{symbol}/override`) always win over
discovered metadata.
Existing reliable exchange / market-maker / custody labels are reused for every token on the same chain.

## Scaling beyond Top 100
Set `"feeds": {"workers": 2}` (or more) to shard exchanges across processes, and `"universe":
{"target_size": 250}`. Exchange limits can be overridden per exchange in `feeds.exchange_overrides`
(e.g. `{"mexc": {"max_symbols_per_connection": 10}}`).
