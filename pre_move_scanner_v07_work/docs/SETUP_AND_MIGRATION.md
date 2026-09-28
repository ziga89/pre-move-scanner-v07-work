# Setup, Windows startup and migration from v0.6

## Requirements
* Windows 10/11, macOS or Linux; **Python 3.11 – 3.13** (tested in CI on Windows and Ubuntu).
* Internet access to CoinGecko and the exchanges' public REST + WebSocket endpoints; optionally Etherscan.
* Dependencies (`requirements.txt`): fastapi, uvicorn[standard], httpx, ccxt — all pure-Python wheels on
  Windows (no compiler needed; v0.6's cryptofeed could need one).

## Windows — first start
1. Install Python from <https://www.python.org/downloads/> and tick **Add python.exe to PATH**.
2. Unzip `pre_move_scanner_v07_work` anywhere (keep your v0.6 folder where it is — it is not needed and is
   never modified).
3. Double-click **`run_windows.bat`**. It creates `.venv`, installs dependencies, creates `config.json`
   *only if missing*, and starts the scanner on <http://127.0.0.1:8000>.
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
| `run_windows.bat test` | automated test-suite |

Stop with **Ctrl+C**. Windows Firewall may ask to allow Python on private networks (needed only for phones).

## macOS / Linux
```bash
cd pre_move_scanner_v07_work
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tools/selftest.py
uvicorn server.app:app --host 0.0.0.0 --port 8000
# offline demo:  PMS_MODE=sim uvicorn server.app:app --port 8001
# no FastAPI available: python -m server.devserver --sim   (stdlib server, polling UI)
```

## API keys (optional)
* `COINGECKO_API_KEY` — free *Demo* key from CoinGecko; raises rate limits (set `coingecko_api_key_type`
  to `pro` for a paid key).
* `ETHERSCAN_API_KEY` — enables wallet intelligence together with `"intel": {"enabled": true}`.

Windows: `setx ETHERSCAN_API_KEY "your-key"` (then open a new command prompt).

Until both are set, the wallet cells say **OFF** (intel disabled) or **NO KEY**. They never show a bare
N/A. With a key, EVM token contracts of the universe coins are discovered automatically through CoinGecko
(`intel.auto_discover_contracts`, 2 lookups per minute, cached for 30 days). Scores still need reliable
labelled addresses (see *Wallet labels*).

## Upgrading from v0.7.1 / v0.7.2
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
| Coins | `watchlist` in config | automatic Top-100 universe; old `watchlist` becomes **pinned** coins |
| Config | overwritten on each start by `run_windows.bat` | created only if missing; old keys mapped (see below) |
| Feed library | cryptofeed | ccxt (`pip install -r requirements.txt` in the new folder) |
| Database | `scanner.db` in the v0.6 folder | `data/scanner_v07.db` (new schema); v0.6 DB importable read-only |
| Score | one composite score | Pre-Move score + sub-scores + status; v0.6 scores shown as "v0.6" in charts |

**Config keys still understood**: `watchlist` → `universe.pinned_assets`; `coingecko_ids` →
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
`intel_cursors`, `signal_outcomes`, `legacy_import`, and since v0.7.3 `alerts` and `token_contracts`; plus the import targets `composite_history_v2`,
`venue_history_v2`, `scanner_events_v2` (same columns as v0.6, with uniqueness indexes). No v0.6 table is
altered or dropped.

**Default retention** (`storage` section): 5 s asset rows 48 h, 10 s venue rows 24 h, 1-minute asset
rollups 30 days, 1-minute venue rollups 14 days, events 90 days, high-conviction alerts 365 days. Estimated steady-state size at Top 100 is
about 1–1.5 GB (a calculation, not yet measured on a live run).

## Wallet labels
Edit `labels/wallet_labels.csv` (`chain,address,entity,entity_type,confidence,source,notes`). Only add
labels you trust. LOW-confidence and WATCH labels are shown but never drive classification or scores.
Tokens to monitor: `intel.tokens` (the contract symbol is verified on the first transfer). Contracts
discovered automatically are added on top. A configured token always wins over a discovered one.
Existing reliable exchange / market-maker / custody labels are reused for every token on the same chain.

## Scaling beyond Top 100
Set `"feeds": {"workers": 2}` (or more) to shard exchanges across processes, and `"universe":
{"target_size": 250}`. Exchange limits can be overridden per exchange in `feeds.exchange_overrides`
(e.g. `{"mexc": {"max_symbols_per_connection": 10}}`).
