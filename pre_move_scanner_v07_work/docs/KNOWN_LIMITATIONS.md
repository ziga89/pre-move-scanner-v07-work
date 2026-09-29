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

## Manual assets and the asset registry (v0.8)
* A manual asset needs a **CoinGecko id**. Coins that CoinGecko does not list cannot be added. An
  ambiguous ticker (several coins share it) is never resolved automatically: the Universe page lists the
  candidates and you choose.
* Two coins in one universe cannot share a ticker. The universe is keyed by symbol, as in v0.7. A manual
  coin whose ticker is already used by a *different* Top-100 coin is refused, and the reason is shown.
* A manual asset is monitored only if at least one supported exchange lists a usable spot market for it.
  Otherwise it stays on the manual list as "no usable market" and is retried at every universe refresh.
* Metadata discovery is paced at 2 CoinGecko coin lookups per minute (`assets.discovery_calls_per_minute`).
  A fresh install needs about an hour to fill the registry for 100+ assets. Until an asset's lookup is
  done, its wallet state is **DISCOVERING**. The results are cached for 30 days.
* Discovery never guesses:
  * A token is used only on the chain CoinGecko publishes as its home platform (`asset_platform_id`).
  * A discovered token contract is marked "on-chain symbol check pending" until the monitor has read a
    transfer of it and the token symbol there matches. It produces no scores before that. For EVM
    tokens this needs the Etherscan key. (`walletcheck` additionally checks ERC-20 `symbol()` through a
    public RPC.)
  * A multi-chain coin without a home platform is **NEEDS_VERIFICATION** until you set an override
    (`PUT /api/assets/{symbol}/override` or `assets.overrides`).
  * Bridged supply on other chains is not tracked.

## Chains (wallet intelligence)
Implemented providers (v0.8.0). All of them normalise into the same wallet-event model:

| Chain(s) | Provider | Key |
|---|---|---|
| Ethereum, BNB Smart Chain, Base, Arbitrum, OP Mainnet, Polygon, Avalanche C-Chain, Mantle, Linea, Scroll, Blast, **XDC Network** | Etherscan V2 (`etherscan`) | `ETHERSCAN_API_KEY`, required |
| Bitcoin | Esplora: mempool.space, then blockstream.info (`esplora`) | none |
| XRP Ledger | public rippled servers: xrplcluster.com, s1/s2.ripple.com (`xrpl`) | none |
| TRON (TRX + TRC-20) | TronGrid (`trongrid`) | `TRONGRID_API_KEY`, optional |
| Solana (SOL + SPL) | Solana JSON-RPC (`solana_rpc`) | `SOLANA_RPC_URL`, optional private RPC |
| Hedera (HBAR + HTS) | Hedera public mirror node (`hedera_mirror`) | none |
| Cardano (ADA) | Koios (`koios`) | `KOIOS_API_TOKEN`, optional |

* **Not supported** (shown as `UNSUPPORTED · provider not implemented`, never faked):
  * Sui, Aptos, TON, NEAR, Polkadot, Stellar, Cosmos chains, Algorand, Litecoin, Dogecoin, Bitcoin Cash,
    Ethereum Classic, Internet Computer, Monero, Tezos;
  * every other chain not in the table above.
* **Live verification.** The keyless providers were verified live from GitHub's Ubuntu and Windows runners
  (`live-wallet.yml`; see `docs/TEST_REPORT_V080.md`). **Etherscan (EVM and XDC) was not live-verified**,
  because no key was available to the test runners. Verify it with
  `run_windows.bat walletcheck --only discovery,evm` on your machine.
* **Etherscan free tier.** It does not cover every chain (e.g. BNB Smart Chain, Base and others may need a
  paid plan). Such a chain shows **DEGRADED** with Etherscan's own message on the Health page; the other
  chains are unaffected. XDC is served through Etherscan V2 (chain id 50) and has the same caveat.
* **Public endpoints.** They are rate-limited, and the limits change without notice. Here is how the
  scanner copes:
  * **XRPL.** Busy servers answer `tooBusy`: the provider fails over to the next server and backs off.
  * **Solana.** The public mainnet RPC throttles hard (HTTP 429). Reading a busy wallet costs one
    `getTransaction` call per transaction, so such an address will lag. For serious Solana coverage, set
    `SOLANA_RPC_URL` to a private RPC.
  * **Cardano.** The public Koios tier is slow (0.5 calls/s by default). Use a stake address in the labels
    file for exchange wallets.
  * **TRON.** Without a key, TronGrid is more strictly limited.
  * **Budgets.** Every provider has a daily call budget and a pace (Health → Wallet providers). When a
    budget is used up, that provider's chains become DEGRADED until the next UTC day.
* **Native coins.** Native coins (ETH, BNB, AVAX, XDC, BTC, XRP, TRX, SOL, HBAR, ADA) are tracked through
  address-centric polling of labelled wallets only. There is no token-wide polling for a native coin.
  Contract-internal native moves are not included:
  * EVM internal transactions;
  * TRON contract-internal TRX.
* **Cardano.** Native-token balances are not read (ADA only). Native tokens are tracked from transactions
  where they appear.
* **Labels.** Only labelled addresses you provide are monitored, plus optional token-wide polling for EVM
  tokens with manageable activity.
  * Without trusted labels for a chain, the wallet columns stay **N/A** with that reason. This is by
    design: discovery finds a token's contract and never labels a wallet.
  * Large unlabelled counterparties are listed as **WHALE_CANDIDATE** and never counted.
  * Labels of type `WATCH`, `WHALE_CANDIDATE` or `UNKNOWN`, or with LOW confidence, never feed a score.
* **UTXO chains (Bitcoin, Cardano).** A receipt is attributed to the dominant input address, and change
  outputs are excluded. CoinJoin-style transactions and batched exchange withdrawals can therefore
  attribute a receipt to one of several real senders. Unlabelled counterparties remain UNKNOWN.
* **DEX flows.** DEX swaps are detected only through labelled DEX pool / router addresses (`DEX_FLOW`).
  Swaps are not decoded, and DEX flow is never treated as a confirmed purchase.
* **Lagging.** A busy exchange hot wallet can have more transfers than one poll cycle can page through.
  Such addresses are flagged "lagging". While they are, wallet scores are multiplied by 0.6 and the state
  reads "ACTIVE (partial)".
* **Token-wide polling.** It is switched off automatically for a token above 400 transfers per hour
  (`intel.token_wide_max_transfers_per_hour`).

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
* **CoinGecko coin detail** (asset registry): `asset_platform_id`, `platforms` and `detail_platforms` as
  published. Lookups are paced to 2 per minute on top of the ranking calls. `/search` (manual assets) is
  called only when you search on the Universe page.
* **Etherscan**: transfer lists and balances as published; free-tier limits (~5 calls/s; 4 calls/s and a
  90 000-call daily budget by default).
* **Other wallet providers**: taken as published by the public endpoint.
  * Default paces: Esplora 1/s, XRPL 2/s, TronGrid 1/s, Solana 2/s, Hedera 2/s, Koios 0.5/s.
  * Default daily budgets: 20 000 each, except TronGrid at 10 000 and Koios at 4 000.
  * An endpoint that returns inconsistent data is not cross-checked against a second source.

## Other
* SQLite is single-writer; very large deployments (Top 500 with long retention) may prefer a server
  database later (the storage layer is isolated in `server/storage`).
* The **iOS SwiftUI starter** in `ios/` has not matched the server since v0.4 and was not updated or
  compiled; use the web app / PWA on iPhone. `/api/state` still provides a v0.6-shaped view.
* PWA installation requires HTTPS (reverse proxy or tunnel).
