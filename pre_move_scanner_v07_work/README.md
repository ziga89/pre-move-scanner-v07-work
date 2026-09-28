# Pre-Move Scanner v0.7 — Top-100 pre-move universe scanner

Ranks the **Top 100 crypto assets** (plus pinned coins such as QNT / XDC / LINK) by **pre-move anomaly
score**: abnormal market structure that appears **while price is still relatively flat**. It is not a
momentum scanner — coins that have already moved are pushed down and labelled **LATE**.

> Research instrument, not trading advice. Thin books can reflect volatility controls, market-maker risk
> reduction, maintenance or delistings; wallet transfers can be custody or internal moves. Cancellations
> are *estimated* (removed liquidity minus aggressive fills), never proven.

## Quick start (Windows)

1. Install **Python 3.11+** from python.org (tick *Add python.exe to PATH*).
2. Double-click **`run_windows.bat`**, then open <http://127.0.0.1:8000>.
3. Before relying on it, run the live self-test once: `run_windows.bat selftest` → `data\selftest_report.md`.

Other actions: `run_windows.bat sim` (offline demo, synthetic markets, port 8001) ·
`run_windows.bat import "C:\path\to\v0.6\scanner.db"` (read-only import of v0.6 history) ·
`run_windows.bat test` (automated tests). Details: [docs/SETUP_AND_MIGRATION.md](docs/SETUP_AND_MIGRATION.md).

macOS / Linux:
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tools/selftest.py            # live checks (optional but recommended)
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

The first ~30 minutes after the very first start are a **warm-up** (baselines are built per coin, per
venue, per metric). A restart within 12 hours reuses the stored 1-minute baselines, so there is no second
warm-up (`engine.rehydrate_max_age_hours`).

## What it does

| Stage | What happens |
|---|---|
| Universe | CoinGecko market-cap ranking, **back-filled past rank 100** until there are 100 eligible, usable coins (no stablecoins, wrapped / staked / bridged tokens, tokenised gold). Pinned coins are added on top. Refreshed hourly with hysteresis. |
| Venues | For **each coin separately**, its biggest spot markets by *that coin's own* 24h volume (3–5 venues), verified to exist on the exchange and to actually stream. XDC never gets Binance unless Binance lists XDC. |
| Feeds | ccxt websockets, per-exchange partitions, capability matrix (multi-symbol vs per-symbol), backoff, circuit breaker, watchdog, health per exchange. |
| Engine | ±2 % band order book: depth ±0.5/1/2 %, spread, imbalance, slippage, adds/removes, refill-after-fills, cancellation **proxy** with confidence, aggressive flow, trade count, volume acceleration — all relative to each market's own lagged baseline (30 m / 2 h / 24 h). |
| Scoring | Order-book, liquidity, buy-pressure and cross-venue sub-scores → compression / wallet context → confidence → independent-signal / venue / liquidity-share caps → **late-move penalty last** (+5 %/15m, +8 %/30m, +12 %/1h hard limits **and** volatility-normalised displacement) → fast / slow persistence. |
| Status | `EMERGING` (30–60 s early warning) · `CONFIRMED PRE-MOVE` / `STRONG PRE-MOVE` (persistent 2–3 min) · `WATCH` · `MOVE IN PROGRESS` · `LATE` · `LOW CONFIDENCE` · `WARMING` · `STALE`. |
| Wallets (optional) | Labelled MM / CEX / custody / whale addresses via Etherscan V2; conservative classes (BUY, SELL, ACCUMULATION-SIDE, DISTRIBUTION-SIDE, SHIFT, UNKNOWN); MM / Whale / CEX-flow / Scarcity scores are **N/A** unless attribution is reliable. |
| History | SQLite (WAL): 5 s asset / 10 s venue rows (24–48 h), 1-minute rollups (30 / 14 days), 90-day event timeline, signal outcomes. v0.6 history can be imported read-only. |

## Dashboard

* **Top anomalies right now** — Rank, Coin, Pre-Move, Liquidity, Order-book, Buy pressure, Cross-venue,
  MM, Whale, CEX flow, Price 15m, Price 1h, Venues (confirmed / live / selected), Reason, Status.
* **Coin detail** — why-this-score reasons, the ordered score pipeline, sub-scores, history charts
  (1h / 6h / 24h / 7d: price & score, score breakdown, liquidity / buying pressure, volume & confirmations,
  spread & slippage, refill & cancel proxy, per-venue ask depth = *which exchange moved first*), venue
  table with depth histograms, lead / lag, wallet panel, event timeline with breakout precursors, and
  venue-selection reasons.
* **Health** — per-exchange state, subscription modes, partitions, reconnects, errors; data-source budgets;
  storage; FX.  **Universe** — the 100 members, pinned coins, and every excluded coin with its reason.

Phones: open `http://YOUR-PC-IP:8000` on the same Wi-Fi (PWA install needs HTTPS).

## Configuration

`config.json` is created from `config.example.json` only if it does not exist (it is never overwritten).
Only put what you change there; all defaults live in `server/config.py`. v0.6 keys (`watchlist`,
`coingecko_ids`, `onchain`, …) are still understood. Wallet labels: `labels/wallet_labels.csv`.

Optional environment variables: `COINGECKO_API_KEY` (free Demo key raises rate limits),
`ETHERSCAN_API_KEY` (wallet intelligence), `PMS_MODE=sim` (offline demo).

## Documentation

* [CHANGELOG.md](CHANGELOG.md)
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — implemented design and how each requirement is met
* [docs/SETUP_AND_MIGRATION.md](docs/SETUP_AND_MIGRATION.md) — install, Windows startup, v0.6 → v0.7
* [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) — limitations, unsupported exchanges / chains, data-source assumptions
* [docs/TEST_REPORT.md](docs/TEST_REPORT.md) — what was tested, how, and what still needs your machine
* [docs/V07_AUDIT_AND_ARCHITECTURE.md](docs/V07_AUDIT_AND_ARCHITECTURE.md) — the v0.6 audit and original proposal
