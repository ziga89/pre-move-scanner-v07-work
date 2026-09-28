# v0.6 — Historical dashboard

The scanner now stores the complete composite microstructure history for 7 days and adds synchronized charts:

- **Price**
- **Pre-Move score**
- **Ask-depth ratio / aggressive-buy ratio / relative volume**
- **60-second volume + confirmed-venue count**
- **Per-venue ask-depth history** so you can see *which exchange moved first*
- automatic event markers when score crosses 55 / 70 / 80 or the number of confirmed venues changes

Click any coin card, then choose **1h / 6h / 24h / 7d**.

Important: full order-book history starts when this scanner version is running. Historical exchange L2 books cannot be reconstructed from candles after the fact.


# Pre-Move Scanner — v0.5 Dynamic Per-Coin Venues

This version fixes an important design error: **the biggest exchanges overall are not necessarily the biggest exchanges for a specific coin.**

At startup, for every asset separately, the scanner:

1. resolves the coin on CoinGecko;
2. downloads that coin's market tickers sorted by **24h converted USD volume**;
3. keeps one highest-volume spot pair per exchange;
4. walks down that ranking and selects up to **5 markets that have a realtime order-book/trade adapter**;
5. normalizes non-USD quote pairs into USD notional using CoinGecko's converted price;
6. builds one liquidity-weighted composite score for that coin.

So XDC does **not** get Binance just because Binance is the world's largest exchange. If XDC's volume is concentrated on KuCoin/Kraken/Bybit/Gate/etc., those are the venues the scanner follows.

The dashboard also shows the discovered 24h volume next to each selected venue.

If a true top market is unsupported by the realtime feed library, it is skipped rather than replaced silently; the scanner keeps walking down the volume ranking to the next streamable market.


# Pre-Move Scanner — v0.4 Top-5 Composite

## The important v0.4 change

The scanner no longer treats exchange cards as separate signals.

For each coin it merges the five largest spot venues used in this build:

**Binance + OKX + Bybit + Coinbase + Gate**

and returns **one composite card / one composite score per coin**.

The combined signal:
- sums real USD/USDT/USDC order-book depth across venues;
- combines aggressive trade flow across venues;
- compares each venue with its own baseline before aggregation;
- requires cross-exchange confirmation for a strong score;
- prevents one tiny/thin exchange from creating a false high signal;
- shows the individual venue breakdown under each coin.

If a coin is not listed on all five, the card shows its live coverage, e.g. `3/5 venues`.

Quotes USD, USDT and USDC are treated as near-dollar quotes for microstructure aggregation. This is intended for anomaly detection, not accounting or execution pricing.


# Pre-Move Scanner — MVP


## v0.3 changes

- adds a **trade-confidence filter**, so 100% aggressive buys on tiny volume cannot create a high score
- uses **trade count + 60s notional** to judge whether buy pressure is statistically meaningful
- adds **absolute book-depth confidence**, reducing false highs on nearly empty books
- fixes ask replenishment: shows **n/a** until enough ask liquidity has actually been removed
- displays the **exchange/venue** on every card
- adds `run_windows.bat`: double-click it on Windows; no PowerShell venv activation is required


A real-time research dashboard for spotting unusual crypto market structure before a price expansion.

It is **not** a news predictor and does **not** infer that a market maker has non-public information.
The score is an explainable anomaly heuristic.

## What v0.1 monitors

### Order book
- bid/ask depth within ±0.5%, ±1%, ±2%
- spread
- bid/ask imbalance
- ask depletion vs recent baseline
- ask replenishment vs removal

### Trades
- aggressive buy/sell quote volume from Binance aggregate trades
- 60s volume
- volume acceleration
- 5-minute price move
- penalty when the coin has already run hard (the goal is *pre*-move)

### Optional known-wallet monitoring
- Ethereum ERC-20 transfers involving manually configured wallets
- Etherscan V2 API
- example includes QNT + a known Wintermute wallet
- **transfer direction is not automatically a buy or sell**

### Storage
- one metric snapshot every 5 seconds to SQLite (`scanner.db`)
- keeps 7 days

## Quick start

Requires Python 3.11+.

```bash
cd pre_move_scanner
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp config.example.json config.json
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Open:

`http://127.0.0.1:8000`

The market-data side uses public Binance Spot market data; no Binance account/API key is required.

## Edit the watchlist

Edit `config.json` before starting:

```json
"watchlist": ["QNTUSDT", "LINKUSDT", "AUDIOUSDT"]
```

Use symbols that actually exist on Binance Spot.

## Enable QNT / MM wallet monitoring

Create an Etherscan API key, then set:

macOS/Linux:
```bash
export ETHERSCAN_API_KEY="..."
```

Windows PowerShell:
```powershell
$env:ETHERSCAN_API_KEY="..."
```

Then in `config.json` set:

```json
"onchain": { "enabled": true, ... }
```

You can add any ERC-20 contract and any known wallet you want to monitor.

## iPhone / iPad

### Fastest method: browser
If your iPhone and computer are on the same Wi-Fi:

1. Start the server with `--host 0.0.0.0`.
2. Find your computer's LAN IP, e.g. `192.168.1.50`.
3. Open `http://192.168.1.50:8000` in Safari.

The page is responsive and works as a mobile dashboard while Safari is open.

### PWA / Home Screen
For a proper installable Home Screen web app and remote use, expose the server over **HTTPS** (a VPS,
reverse proxy, or secure tunnel). Then Safari can install it via **Share > Add to Home Screen**.

### Native iOS
A SwiftUI client starter is included in `/ios`. It reads the exact same WebSocket feed as the web UI.

Important architecture point:
**do not make the iPhone the 24/7 collector.** iOS suspends long-running background sockets.
Run the scanner continuously on a Mac/VPS and let the iOS app be the live viewer + alert receiver.

## Score interpretation

- 0–39: normal
- 40–54: interesting
- 55–69: watch
- 70–79: strong anomaly
- 80+: extreme / rare

v0.2 requires multiple independent confirmations. A single thin ask book is not enough for a high score.
The first 30 minutes are treated as warm-up, while a 2-hour rolling baseline is built.

The score currently weights:
- ask-depth thinning
- aggressive buy pressure
- volume acceleration
- bid/ask imbalance
- weak ask replenishment
- spread widening

It then penalizes coins that are already sharply up over the last 5 minutes.

## What is NOT in v0.1 yet

These are the next useful additions:
1. Kraken L3 order-level analysis
2. Coinbase order-book feed
3. cross-exchange depth comparison
4. spoof/cancel persistence analysis
5. MM net-inventory estimates across labelled wallets
6. CEX vs Prime/custody reclassification detector
7. Telegram / iOS push alerts
8. watchlist auto-discovery: scan hundreds of coins and rank the top anomalies
9. proper historical backtesting against future 1h/4h returns

Those matter before treating the score as statistically useful.

## Research warning

A thin ask book can be caused by volatility controls, normal market-maker risk reduction,
exchange-specific inventory, delistings, maintenance, or genuine demand. Wallet transfers may be
custody/internal movements rather than purchases. Treat the scanner as a research instrument, not proof of manipulation.
