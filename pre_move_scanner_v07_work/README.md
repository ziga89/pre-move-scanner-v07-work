# Pre-Move Scanner v0.8 — Top-100 + manual assets, multi-chain wallet intelligence

Ranks the **CoinGecko Top 100** plus your **manual assets** (QNT, LINK and XDC by default; add any coin from
the Universe page) by **pre-move anomaly score**: abnormal market structure that appears **while price is
still relatively flat**. It is not a momentum scanner. Coins that have already moved are pushed down and
labelled **LATE**.

> This is a research instrument, not trading advice. Thin books can reflect volatility controls, market-maker
> risk reduction, maintenance or delistings. Wallet transfers can be custody, market-maker or internal
> exchange moves. Cancellations are *estimated* (removed liquidity minus aggressive fills), never proven. The
> evidence score is a composite strength, not a probability.

The version is defined once, in `server/__init__.py` (0.8.0). The launcher, the API, the UI, the service
worker and the release ZIP all read it from there.

## Quick start (Windows)

1. Install **Python 3.11+** from python.org and tick *Add python.exe to PATH*.
2. Double-click **`run_windows.bat`**, then open <http://127.0.0.1:8000>.
   * The first start creates `.venv\`, `config.json` (from `config.example.json`, **only if it does not
     exist**) and `data\`.
   * The launcher never overwrites `config.json` or the database.
3. Before relying on the scanner, run the live checks:
   * `run_windows.bat selftest --repeat 3` checks the exchanges and feeds and writes
     `data\selftest_report.md`;
   * `run_windows.bat walletcheck` checks CoinGecko discovery and every wallet provider and writes
     `data\wallet_check_report.md`.

**Upgrading from v0.7.x:** unzip the release over your folder and start as usual. Your database is backed
up and migrated in place, and your `config.json` keeps working. See
[docs/MIGRATION_V080.md](docs/MIGRATION_V080.md).

| Command | What it does |
|---|---|
| `run_windows.bat` | Starts the live scanner on port 8000. |
| `run_windows.bat sim` | Offline demo with synthetic markets on port 8001. |
| `run_windows.bat test` | Runs the automated test suite. |
| `run_windows.bat selftest --repeat 3` | Live exchange and feed check. |
| `run_windows.bat walletcheck [--only discovery,evm,bitcoin,...]` | Live wallet-provider check. |
| `run_windows.bat stress` | Live start / stop / reconnect stress test (KuCoin by default). |
| `run_windows.bat import "C:\path\to\v0.6\scanner.db"` | Read-only import of v0.6 history. |
| `run_windows.bat release` | Builds a clean source ZIP in `dist\`. It never contains data, config, the venv, databases or keys. |

Set `PMS_NO_PAUSE=1` to skip the "press any key" prompts.

macOS / Linux: `./run.sh` accepts the same actions. To run it by hand:
```bash
python3 tools/bootstrap.py --venv     # .venv + pip install + config.json only if missing + data/
.venv/bin/python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
```

The first ~30 minutes after the very first start are a **warm-up**: baselines are built per coin, per
venue and per metric. A restart within 12 hours reuses the stored 1-minute baselines, so there is no second
warm-up.

## What it does

| Stage | What happens |
|---|---|
| Universe | One de-duplicated list: the CoinGecko market-cap ranking, **back-filled past rank 100** until there are 100 eligible, usable coins, **plus your manual assets**. Stablecoins, wrapped / staked / bridged tokens and tokenised gold are excluded. The ranking is refreshed hourly with hysteresis. A manual asset that is already in the Top 100 is listed once. Manual status never changes a score or the sort order. |
| Manual assets | Universe → *Manual assets*: search by ticker, name or CoinGecko id, then pick the exact coin (an ambiguous ticker is never picked for you). The preview shows the chain, contract, wallet support and venues. **Add** starts monitoring at once, without a restart. **Remove** stops it and keeps its history. The list is stored in SQLite; `config.json` only seeds it. |
| Venues | For **each coin separately**: its biggest spot markets by *that coin's own* 24h volume (3–5 venues), verified to exist on the exchange and to actually stream. |
| Feeds | ccxt websockets, per-exchange partitions, capability matrix, back-off, circuit breaker, watchdog and health per exchange. |
| Engine and scoring | ±2 % band order book, liquidity, buy pressure and cross-venue sub-scores against each market's own lagged baseline. Confidence and independent-signal caps are applied, and the **late-move penalty comes last**. |
| Status | `EMERGING` · `CONFIRMED PRE-MOVE` / `STRONG PRE-MOVE` · `WATCH` · `MOVE IN PROGRESS` · `LATE` · `LOW CONFIDENCE` · `WARMING` · `STALE`. |
| Signal Radar | An always-visible bar with the states `NO HIGH-CONVICTION SETUP` · `WATCH` · `CONFIRMING` · `HIGH-CONVICTION BUY SETUP` · `INVALIDATED`. It shows the evidence score (not a probability), the checks and gates passed, the confirmed venues, the persistence time, highlights (e.g. "ask supply draining", "volume accelerating (3.4x normal)", "price still flat") and the wallet evidence. A HIGH-CONVICTION setup needs multi-venue structure, buy flow, complete feeds and a flat price, all held for 120 s. It is never raised from one venue, one print or wallet reshuffling. |
| Asset registry | Chain, platform and contract metadata for every asset, discovered from CoinGecko and cached in SQLite for 30 days. A native coin is recognised as native. A token is used only on the chain CoinGecko names as its home platform, and ERC-20 contracts are re-checked on chain. When the chain is ambiguous, the result is `NEEDS_VERIFICATION`: nothing is guessed. Overrides are available from the API and `assets.overrides`. |
| Wallet intelligence (optional) | Provider adapters normalise every chain into one wallet-event model: Etherscan V2 (EVM + XDC), Esplora (Bitcoin), rippled (XRPL), TronGrid (TRON), Solana RPC, Hedera mirror and Koios (Cardano). Events: `CEX_IN`, `CEX_OUT`, `ACCUMULATION`, `DISTRIBUTION`, `INTERNAL_SHIFT`, `MM_ROUTING`, `CUSTODY_SHIFT`, `BRIDGE`, `DEX_FLOW`, `UNKNOWN_TRANSFER`. Custody, market-maker and internal moves are never counted as buying. Each asset has an explicit state: **OFF · NO KEY · DISCOVERING · WARMING · ACTIVE · UNSUPPORTED · DEGRADED · N/A**, always with a reason. Zero is a real value; N/A means no value. |
| History | SQLite (WAL): 5 s asset and 10 s venue rows (24–48 h), 1-minute rollups (30 / 14 days), a 90-day event timeline, alerts and signal outcomes. The database is backed up before every schema migration. |

## Dashboard

* **Signal Radar** (top of every view): see above. Click an entry to open the coin.
* **Top anomalies right now**: Rank, Coin (manual assets carry an `M` badge), Pre-Move, Liquidity,
  Order-book, Buy pressure, Cross-venue, MM, Whale, CEX flow, Price 15m / 1h, Venues, Reason and Status.
* **Coin detail**: why-this-score reasons, the score pipeline, history charts, the venue table, lead / lag,
  and a wallet panel with the chain, contract, provider, state, events by type and whale candidates.
* **Universe**: Top-100 members, manual assets (search / preview / add / remove) and excluded coins with
  their reasons.
* **Health**: exchanges, data-source budgets, **wallet providers** (per chain: state, calls today / budget,
  errors, rate-limit hits, last error with keys scrubbed), the asset registry, and storage (schema version,
  upgrade and backup).

Phones: open `http://YOUR-PC-IP:8000` on the same Wi-Fi. Installing it as a PWA needs HTTPS.

## Configuration and secrets

* Only put what you change in `config.json`. All defaults live in `server/config.py`.
* v0.6 and v0.7 keys are still understood: `pinned_assets` and `watchlist` are read as `manual_assets`.
* **API keys never go in `config.json`**, the source, the database, logs or release ZIPs. The config holds
  only the *names* of environment variables:
  * `ETHERSCAN_API_KEY`: EVM chains and XDC;
  * `COINGECKO_API_KEY`: optional, raises rate limits;
  * `TRONGRID_API_KEY`, `KOIOS_API_TOKEN`, `SOLANA_RPC_URL`: all optional.

  On Windows, set them with `setx ETHERSCAN_API_KEY your-key` (then open a new window). The scanner also
  reads the user environment from the registry.
* Wallet labels: `labels/wallet_labels.csv`, for all supported chains, using the v0.8 taxonomy (`CEX_HOT`,
  `CEX_COLD`, `CEX_DEPOSIT`, `CEX_CUSTODY`, `CUSTODY_INSTITUTIONAL`, `MARKET_MAKER`, `DEX_POOL`,
  `DEX_ROUTER`, `BRIDGE`, `TREASURY`, `WHALE`, `WHALE_CANDIDATE`, `UNKNOWN`).

## API

`/api/top` · `/api/coin/{asset}` · `/api/history/{asset}` · `/api/radar` · `/api/universe` · `/api/health` ·
`/api/version` and, new in v0.8:

| Endpoint | Purpose |
|---|---|
| `GET /api/assets` | The whole universe with its registry metadata. |
| `GET /api/assets/search?q=` | CoinGecko candidates for a ticker, name or id. |
| `GET /api/assets/resolve/{coingecko_id}` | Preview: metadata, wallet support and venues. |
| `GET /api/assets/{symbol}` | One asset. |
| `POST /api/assets/manual` | Add a manual asset. Body: `{"coingecko_id": "..."}`. |
| `DELETE /api/assets/manual/{symbol}` | Remove a manual asset; its history is kept. |
| `PUT /api/assets/{symbol}/override` | Set chain / contract metadata by hand. |
| `GET /api/wallet/providers` | Per-provider and per-chain diagnostics. |
| `GET /api/wallet/status` | Wallet state for every asset. |
| `GET /api/wallet/{symbol}` | Wallet detail for one asset. |

## Documentation

* [CHANGELOG.md](CHANGELOG.md)
* [docs/MIGRATION_V080.md](docs/MIGRATION_V080.md): upgrading from v0.7.x
* [docs/TEST_REPORT_V080.md](docs/TEST_REPORT_V080.md): what was tested for v0.8.0, how, and what still
  needs your machine
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the implemented design
* [docs/SETUP_AND_MIGRATION.md](docs/SETUP_AND_MIGRATION.md): install, Windows startup, clean releases,
  v0.6 import
* [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md): limitations, unsupported exchanges and chains,
  data-source assumptions
* [docs/TEST_REPORT.md](docs/TEST_REPORT.md) (v0.7.x) and
  [docs/V07_AUDIT_AND_ARCHITECTURE.md](docs/V07_AUDIT_AND_ARCHITECTURE.md): history
