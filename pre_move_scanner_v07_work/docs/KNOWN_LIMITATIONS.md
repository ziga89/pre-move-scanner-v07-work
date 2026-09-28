# Known limitations, unsupported exchanges / chains, data-source assumptions

## Live verification so far
Since v0.7.1 the feeds are tested live from GitHub's Windows and Ubuntu runners (`live-feeds.yml`, see
`docs/TEST_REPORT.md`). All selected markets streamed on KuCoin, Coinbase, OKX, Kraken, Gate, MEXC, HTX,
Upbit, Bitfinex and Bitrue, including repeated start / stop / reconnect stress on KuCoin, Coinbase and MEXC.
Not covered there:
* **Binance (HTTP 451) and Bybit (HTTP 403) refuse US data centres**, so they were never live-tested.
  Run `run_windows.bat selftest --repeat 3` from your own network.
* Per-exchange connection limits are still conservative assumptions (Health page: "unverified"). The live
  runs used a few markets per exchange, not the full Top-100 load.
* Multi-hour behaviour: token expiry, exchange maintenance, memory and database growth.

## Exchange-specific behaviour handled in code
* **KuCoin**: the ccxt instance must be bound to the event loop before the first watch (fixed in 0.7.1).
* **Coinbase** serves `BASE/USDC` under `BASE-USD`. The updates are mapped back to the subscribed USDC
  market (0.7.1).
* **MEXC**: ccxt decodes the spot stream as protobuf, so the `protobuf` package is required (in
  `requirements.txt`). Without it, MEXC markets are marked UNAVAILABLE with that reason.
* **Bitrue** has no trade stream in ccxt (`NotSupported`). Books stream, but trade-based signals are missing
  for Bitrue markets.
* **HTX** occasionally fails to line up its order-book snapshot (`InvalidNonce`). The loop resyncs and it
  is reported as an incident (seen once in 8 live rounds).

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
  Hedera, and other non-EVM chains. For those coins MM / Whale / CEX / Scarcity show **UNSUPPORTED**, with
  the reason. Native EVM gas coins (ETH, BNB, AVAX) are UNSUPPORTED too, because only ERC-20 transfers
  are tracked.
* Only labelled addresses you provide are monitored (plus optional token-wide polling for tokens with
  manageable activity). Without labels the wallet columns stay N/A — by design. Discovery finds a token's
  contract, but never labels a wallet. Large unlabelled counterparties of exchange transfers are listed
  as UNKNOWN / WHALE CANDIDATE and are never counted.
* Contract discovery trusts CoinGecko's `asset_platform_id` (the chain a token is native to) and then
  re-checks the on-chain symbol. A token that is native to one EVM chain is monitored only there:
  bridged supply on other chains is not tracked. A coin whose CoinGecko entry has no platform is treated
  as a native coin (UNSUPPORTED).
* DEX swap detection (BUY / SELL) requires labelled DEX pool / router addresses; swaps are not decoded.
* A busy exchange hot wallet can have more transfers than one poll cycle can page through. Such addresses
  are flagged "lagging", and while they are, wallet scores are multiplied by 0.6 and marked "partial coverage".
* Token-wide polling is switched off automatically for a token above 400 transfers per hour
  (`intel.token_wide_max_transfers_per_hour`); after that only address-centric monitoring runs for it.

## Signal Radar / high-conviction alerts
* The thresholds (score levels, venue counts, 120 s persistence, 60 s hysteresis) are set by hand and are
  conservative. They are **not yet calibrated** on live outcomes. `signal_outcomes` records forward returns
  for each fired alert so that they can be calibrated later.
* The evidence score is a composite strength, not a probability, and never proof of a purchase.
* **No live HIGH-CONVICTION alert has been observed yet.** The alert path is tested with fixtures, and end
  to end in SIM with a forced setup. The strict checks were not relaxed for those tests.
* By design, one selected venue that is not live blocks WATCH and HIGH-CONVICTION for that coin
  (`alerts.min_selected_live_ratio` = 1.0). A flaky exchange can therefore hide a real setup; it cannot
  create one.
* Persistence means an alert fires at least 2 minutes after the structure appears. An open alert survives
  a scanner restart only as a closed record; it is not resumed.

## Data-source assumptions
* **CoinGecko**: market-cap ranking, prices and categories are taken as published. Free tiers are rate
  limited. The client stays under 8 calls/min by default. The ranking needs 2 calls per hour (2 pages of
  250), plus occasional category and cross-check lookups. If CoinGecko is down, the last cached universe is
  used and flagged.
* **Exchange 24h volume** from `fetch_tickers` is taken as reported; a wash-volume check against visible
  ±2 % depth demotes implausible markets but cannot detect all wash trading.
* **Identity**: a market is the same asset only if its USD price is within ±5 % of CoinGecko's (±12 % for
  KRW). Legitimate large premiums/discounts would be rejected (reason shown).
* **CoinGecko coin detail** (contract discovery): `asset_platform_id`, `platforms` and `detail_platforms`
  as published; lookups are paced to 2 per minute on top of the ranking calls.
* **Etherscan**: transfer lists and balances as published; free-tier limits (~5 calls/s, daily budget
  configurable, 90 000 by default).

## Other
* SQLite is single-writer; very large deployments (Top 500 with long retention) may prefer a server
  database later (the storage layer is isolated in `server/storage`).
* The **iOS SwiftUI starter** in `ios/` has not matched the server since v0.4 and was not updated or
  compiled; use the web app / PWA on iPhone. `/api/state` still provides a v0.6-shaped view.
* PWA installation requires HTTPS (reverse proxy or tunnel).
