# Known limitations, unsupported exchanges / chains, data-source assumptions

## Not yet verified against live markets
The build environment had no access to CoinGecko, the exchanges or Etherscan. Everything was verified
with fixtures and the synthetic SIM market, plus GitHub Actions (which installs the real ccxt / FastAPI but
makes no exchange connections). **Live behaviour must be confirmed with `run_windows.bat selftest`** —
especially per-exchange websocket limits (the capability matrix values are documented assumptions,
marked "unverified" in the Health page) and real message rates.

## Market microstructure
* **Cancellations are a proxy**: removed ask liquidity minus aggressive buy notional. Hidden / iceberg
  fills, exchange-side aggregation of book updates, feed throttling (250 ms), timestamp misalignment and
  truncated depth all blur it. It is shown with a confidence label (max "moderate") and weighted by that
  confidence in scoring — never as confirmed cancellations.
* **Visible depth**: some feeds deliver limited levels (e.g. Bybit spot 200). For very liquid coins the
  book may not reach ±2 % ("book coverage" in the venue table); ratios vs the market's own baseline remain
  meaningful, absolute ±2 % depth is then a lower bound.
* **Trade side** relies on the exchange's taker-side field as normalised by ccxt.
* **Aggregation** across venues uses liquidity weights; quote→USD conversion for KRW uses BTC/KRW ÷ BTC/USD,
  which embeds the Korean premium (a few % on Upbit / Bithumb notional).
* **Volatility normalisation** needs ≥ 4 h of 1-minute history per coin; before that only the hard
  +5 / +8 / +12 % thresholds apply.
* **Warm-up**: ~30 minutes on the very first run (lagged 2 h baseline needs ≥ 20 valid minutes).
* **Scores are heuristics**, not probabilities; weights and thresholds are initial values. Use
  `/api/outcomes` (forward returns after each signal) to calibrate.

## Exchanges
Supported through ccxt (default list): Binance, OKX, Bybit, Coinbase (Advanced Trade), Kraken, KuCoin,
Gate, MEXC, Bitget, HTX, Crypto.com, Upbit, Bitfinex, Bitstamp, Bitrue.
Installed ccxt 4.5.84 (CI): all 15 ids valid with websocket order books; multi-symbol subscriptions on
Binance, OKX, Bybit, Coinbase, Kraken, KuCoin, Bitget, Crypto.com; per-symbol order books on Gate, Upbit,
MEXC, HTX, Bitfinex, Bitstamp, Bitrue.

* Other ccxt exchanges can be added to `discovery.exchanges`. They get a conservative default policy
  (per-symbol subscriptions, slow pacing) because their limits have not been reviewed.
* **Not supported**: DEXs / AMMs (Uniswap, PancakeSwap, Raydium, …) and derivatives (perpetuals and futures
  are excluded on purpose; this is spot only). Any exchange that is not in `discovery.exchanges`, or has no
  websocket order book in the installed ccxt, is also unsupported. CoinGecko's top markets on unsupported
  exchanges are listed per coin under "Top markets on unsupported exchanges" rather than being replaced
  silently.
* Bithumb, WhiteBIT, LBank and other exchanges are not in the default list. If ccxt supports them, add them
  and run the self-test.

## Chains (wallet intelligence)
* Supported: EVM chains through Etherscan V2 (Ethereum by default; BSC, Polygon, Arbitrum, Optimism,
  Base, Avalanche, Linea, Scroll, Mantle, Blast are wired but Etherscan's free-tier chain coverage has
  changed over time — per-chain errors are shown on the Health page).
* **Not supported**: Bitcoin, XRP Ledger, Solana, Cardano, TRON, TON, **XDC native chain**, Cosmos chains,
  Hedera, and other non-EVM chains. For those coins MM / Whale / CEX / Scarcity are always **N/A**.
* Only labelled addresses you provide are monitored (plus optional token-wide polling for tokens with
  manageable activity). Without labels the wallet columns stay N/A — by design.
* DEX swap detection (BUY / SELL) requires labelled DEX pool / router addresses; swaps are not decoded.
* A busy exchange hot wallet can have more transfers than one poll cycle can page through. Such addresses
  are flagged "lagging", and while they are, wallet scores are multiplied by 0.6 and marked "partial coverage".
* Token-wide polling is switched off automatically for a token above 400 transfers per hour
  (`intel.token_wide_max_transfers_per_hour`); after that only address-centric monitoring runs for it.

## Data-source assumptions
* **CoinGecko**: market-cap ranking, prices and categories are taken as published. Free tiers are rate
  limited. The client stays under 8 calls/min by default. The ranking needs 2 calls per hour (2 pages of
  250), plus occasional category and cross-check lookups. If CoinGecko is down, the last cached universe is
  used and flagged.
* **Exchange 24h volume** from `fetch_tickers` is taken as reported; a wash-volume check against visible
  ±2 % depth demotes implausible markets but cannot detect all wash trading.
* **Identity**: a market is the same asset only if its USD price is within ±5 % of CoinGecko's (±12 % for
  KRW). Legitimate large premiums/discounts would be rejected (reason shown).
* **Etherscan**: transfer lists and balances as published; free-tier limits (~5 calls/s, daily budget
  configurable, 90 000 by default).

## Other
* SQLite is single-writer; very large deployments (Top 500 with long retention) may prefer a server
  database later (the storage layer is isolated in `server/storage`).
* The **iOS SwiftUI starter** in `ios/` has not matched the server since v0.4 and was not updated or
  compiled; use the web app / PWA on iPhone. `/api/state` still provides a v0.6-shaped view.
* PWA installation requires HTTPS (reverse proxy or tunnel).
