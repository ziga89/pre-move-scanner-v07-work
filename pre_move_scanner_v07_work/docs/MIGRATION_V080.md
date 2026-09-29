# Upgrading to v0.8.0

v0.8.0 upgrades any v0.7.x installation (including v0.7.3 and v0.7.4) **in place**. You do not delete
anything, you do not copy data, and nothing you collected is lost.

## TL;DR (Windows)

1. Stop the scanner (Ctrl+C).
2. Unzip the v0.8.0 release ZIP **over your existing folder**, the one that contains `data\` and
   `config.json`. Confirm "replace files" when asked.
   * The release ZIP is source-only. It contains no `data\`, no `config.json`, no `.venv\`, no database
     and no keys, so it cannot overwrite `data\scanner_v07.db` (your baselines, history, alerts and
     outcomes) or your settings.
   * You can check any ZIP before unpacking it:
     `.venv\Scripts\python.exe tools\make_release.py --verify path\to\the.zip`
3. Double-click `run_windows.bat`, as before. On the first start it does four things:
   * installs the updated dependencies into your existing `.venv`;
   * keeps your `config.json` (it is never rewritten);
   * **backs up your database** to `data\backups\scanner_v07.before-v0.8.0-<date>.db`;
   * migrates the database in place (schema 2 → 3) and starts.
4. Open <http://127.0.0.1:8000> → **Health**. The *Storage* row must say `upgraded from schema 2` and
   name the backup file.

## What changes on disk

| Item | What happens |
|---|---|
| `data\scanner_v07.db` | **Kept, same name.** Migration 3 adds the tables `asset_registry`, `manual_assets` and `app_meta`, and 5 columns on `onchain_transfers` (`event_type`, `attribution_confidence`, `entity_type`, `direction`, `provider`). No table is dropped or rewritten, and no row is deleted. The migration runs in one transaction: an interrupted upgrade leaves the database exactly as it was and runs again on the next start. |
| `data\backups\` | New. A consistent SQLite copy of the database, taken before the migration and only once per upgrade. It is skipped when free disk space is below 2× the database size; Health then says why. You can delete old backups once you are happy with v0.8. |
| `config.json` | **Never rewritten.** v0.7 keys keep working (see below). |
| `.venv\` | Reused. `pip install -r requirements.txt` runs on every start, as before. |
| `server\intel\providers.py` (v0.7 file) | Replaced by the package `server\intel\providers\`. If you unzip over the old folder, the stale `providers.py` stays behind but is ignored: Python loads the package first. You may delete it. |
| `server\intel\discovery.py` (v0.7 file) | Replaced by the asset registry (`server\intel\registry.py`). A stale copy left behind by unzipping is never imported. You may delete it. |

What is **kept** by the migration (tested in `tests/test_upgrade_v08.py` with a real v0.7.3-shaped
database, row by row):
* market history: 5 s asset rows, 10 s venue rows, 1-minute rollups;
* baselines: rebuilt from the kept 1-minute rows, so a restart within 12 h has no new warm-up;
* high-conviction alerts and their chart bands;
* signal outcomes;
* events;
* wallet transfers and balances;
* intel cursors and the Etherscan call counter;
* universe snapshots and venue selections;
* the v0.6 import tables.

## Configuration

Your `config.json` keeps working unchanged:

| v0.7 setting | v0.8 behaviour |
|---|---|
| `universe.pinned_assets` | Read as `universe.manual_assets`: the **initial** manual-asset list. Health → Config shows a note. |
| `universe.coingecko_ids` | Used to resolve the seeded manual assets by CoinGecko id. The defaults now include `XDC → xdce-crowd-sale`. |
| `intel.tokens` (QNT, LINK contracts) | Verified **overrides** in the asset registry. |
| `intel.discovery_calls_per_minute`, `intel.discovery_ttl_days` | Carried over to the new `assets` section. |
| `intel.etherscan_api_key_env`, `universe.coingecko_api_key_env` | Unchanged: they hold the **name** of an environment variable, never the key. |

New optional settings (defaults in `server/config.py`, examples in `config.example.json`):
* `assets.discovery_calls_per_minute` (2), `assets.discovery_ttl_days` (30) and `assets.overrides`.
* `intel.providers.<evm|bitcoin|xrpl|tron|solana|hedera|cardano>`: `enabled`, `daily_call_budget` and
  `calls_per_second`, plus `api_key_env` / `rpc_url_env`. These hold **names** of environment variables:
  `TRONGRID_API_KEY`, `KOIOS_API_TOKEN` and `SOLANA_RPC_URL` are all optional.
* `storage.backup_before_migration` (true).

## Manual assets (formerly "pinned")

* Your pinned QNT, LINK and XDC become **manual assets** on the first v0.8 start. They are stored in the
  database; the config list is only an initial seed.
* Manage them on **Universe → Manual assets**:
  * search a ticker, name or CoinGecko id;
  * choose the right coin (an ambiguous ticker is never picked for you);
  * check the preview (chain, contract, wallet support, venues);
  * **Add**.

  The asset joins immediately, with no restart.
* **Remove** stops monitoring a manual-only asset and keeps its history. Removing a manual asset that is
  also a Top-100 member only clears the manual mark.
* Manual assets are ranked exactly like every other asset.

## Wallet intelligence after the upgrade

* **OFF** everywhere until `"intel": {"enabled": true}`, as before.
* **Assets:**
  * EVM tokens and native ETH / BNB / AVAX / XDC use your `ETHERSCAN_API_KEY`.
  * Bitcoin, XRPL, TRON, Solana, Hedera and Cardano need no key.
* **Metadata:** each asset shows `DISCOVERING` until its chain / contract metadata has been looked up
  (2 CoinGecko calls per minute; the whole Top-100 takes about an hour once, then it is cached for 30
  days).
* **Scores** still need **trusted labelled addresses** in `labels/wallet_labels.csv` for the chain. The
  file accepts all v0.8 chains and the new taxonomy. Without labels the state is `N/A` with the reason;
  nothing is guessed.
* **Etherscan free tier:** it does not cover every chain. Such chains show `DEGRADED` with Etherscan's
  own message; other chains are unaffected.

## Going back to v0.7.x

* The v0.7.4 source runs on the migrated database: it ignores the new tables and columns. This was
  verified by starting the v0.7.4 code on a v0.8-migrated copy; history and alerts were intact.
  * v0.7 cannot read the wallet-poll cursors that v0.8 writes, so its wallet monitor starts polling
    afresh and its daily Etherscan counter restarts.
  * v0.7 does not interpret wallet events recorded by v0.8.
* For a strict rollback, restore the backup:

  ```bat
  copy data\backups\scanner_v07.before-v0.8.0-<date>.db data\scanner_v07.db
  ```

  Stop the scanner first, and delete `data\scanner_v07.db-wal` / `-shm` if they are present.

## Checklist on your machine

```bat
cd C:\path\to\pre_move_scanner_v07_work
set PMS_NO_PAUSE=1
run_windows.bat test
run_windows.bat selftest --repeat 2
run_windows.bat walletcheck
run_windows.bat
```

* `run_windows.bat test`: the automated tests, 290+.
* `run_windows.bat selftest --repeat 2`: exchanges and feeds, as in v0.7.
* `run_windows.bat walletcheck`: CoinGecko discovery and every wallet provider, EVM included, when your
  key is set. It writes `data\wallet_check_report.md`.
* `run_windows.bat`: the scanner. Then Health → Storage (upgraded from schema 2) and Universe → Manual
  assets.
