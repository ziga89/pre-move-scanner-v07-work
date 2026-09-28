# Pre-Move Scanner — v0.6 Audit & v0.7 Architecture Proposal

**Status:** Phase 1 (audit only). No application code has been changed. Implementation waits for approval.
**Date:** 2026-09-28

> **Later note:** This proposal was approved with ten amendments: cancellation proxy with confidence,
> address-centric wallet intelligence, the exchange capability matrix, volatility-normalised late-move
> logic, the two-speed EMERGING / CONFIRMED signal, an explicit scoring order, universe back-fill, null
> semantics, CI, and v0.6 untouched. It was then implemented. The design as built, including where it
> differs from this proposal, is in [ARCHITECTURE.md](ARCHITECTURE.md). The document below is the original
> audit, kept unchanged.

---

## 0. Workspace and integrity

| Folder | Purpose | State |
|---|---|---|
| `pre_move_scanner_v06_history/` | Untouched reference copy of the uploaded v0.6 ZIP | Byte-identical to the ZIP, set read-only on disk |
| `pre_move_scanner_v07_work/` | The only folder that will be changed | Currently identical to v0.6 plus this `docs/` file |
| `v06_manifest.sha256` | Per-file SHA-256 of the v0.6 files | Used to prove at delivery that v0.6 is unchanged |

- Uploaded ZIP SHA-256: `724630106cb8326d7eb20a1bab406ae1e351075e725d8630bbaf58f7e3fb5b66`
- Verify at any time: `cd pre_move_scanner_v06_history && sha256sum -c ../v06_manifest.sha256`
- The ZIP contains no `scanner.db`, `config.json` or discovery cache, so your real v0.6 history lives only on your machine. v0.7 will import it **read-only** (see §11).

### Test environment limitation (important)

This cloud sandbox's network policy denies **PyPI** and **every market-data host**: CoinGecko, all exchanges and Etherscan (all HTTP 403 at the proxy). As a result:

- `fastapi`, `uvicorn`, `httpx`, `cryptofeed` and `ccxt` cannot be installed here, so the v0.6 backend could not be started and the cryptofeed calls could not be checked against the real library.
- Loading the live Top 100 and discovering venues cannot be tested here.

What I *could* do: syntax-check everything (all Python, JS and JSON pass), read all of the code, and benchmark the v0.6 hot paths with the standard library only (cryptofeed stubbed out). The figures in §4 come from those benchmarks.

The acceptance tests in requirement 25 need network access. See §16 for the options.

---

## 1. Current architecture (v0.6)

```
                       startup (once)
CoinGecko /search + /coins/{id}/tickers ──► discover.py ──► venue_discovery_cache.json
                                               │ (≤5 markets/coin with a cryptofeed adapter)
                                               ▼
cryptofeed FeedHandler (ONE handler, ONE feed per exchange, all symbols)
   │  L2_BOOK callback: copies up to 500 levels/side into dicts, diffs against previous full book
   │  TRADES callback: appends to deque
   ▼
VenueState (per coin×venue, in memory)          ◄── metric() on demand (cached 0.8 s)
   trades / prices / add-remove deques, metrics_history (≤12 000 dicts)
   ▼
MultiVenueScanner._composite()  → one composite score per coin
   ▼
app.py (FastAPI, same event loop as the feeds)
   ├─ broadcaster(): full snapshot of everything → every WS client, every 1 s
   ├─ persister(): every 5 s → SQLite (composite_history_v2, venue_history_v2, scanner_events_v2)
   │               + 7-day DELETE, synchronously on the event loop
   ├─ /api/state, /api/history/{asset} (sync SQLite on the event loop)
   └─ EtherscanWatcher: polls tokentx per configured wallet, in-memory only
web/: vanilla JS cards + canvas charts (price, score, flow, volume, per-venue ask depth, events)
ios/: SwiftUI client (targets the v0.2 payload format)
server/scanner.py: v0.1–v0.3 Binance-only scanner — dead code, not imported
```

---

## 2. What currently works (or is sound)

Verified by reading and static checks; live streaming could not be checked here (§3, item C1).

1. **Per-coin venue selection concept.** For each coin, CoinGecko tickers are ranked by converted USD volume, one pair per exchange is kept, and the scanner walks down the list to the first 5 markets that have an adapter. Unsupported top markets are recorded as `skipped_markets` rather than silently replaced. This is the right idea and is kept.
2. **The liquidity-weighted composite** reconstructs each venue's baseline ask depth (`ask/ratio`) and sums them. The result is a true cross-venue "depth vs own baseline" ratio that a thin venue cannot dominate. Kept.
3. **False-positive guards exist:**
   - trade confidence = √(notional × count), with hard caps below 0.2 and 0.4;
   - coverage caps (1 venue ≤ 42, 2 venues ≤ 72);
   - confirmed-venue caps (0 → 39, 1 → 59, 2 → 82);
   - a warm-up cap (44);
   - a replenishment ratio that shows `n/a` until enough ask liquidity has actually been removed.
4. **History layer.** Three tables with `(asset, ts)` indexes, 7-day retention, a history API with range selection, per-venue history ("which exchange moved first"), threshold and confirmation event markers, and dependency-free canvas charts with 1h / 6h / 24h / 7d buttons.
5. **Web UI.** Live WebSocket with auto-reconnect, PWA manifest and service worker, responsive layout, and conservative wording ("transfers are not automatically buys/sells").
6. **Etherscan watcher** with honest semantics: IN/OUT only, with no buy/sell claims.
7. **Syntax.** All 6 Python files, `app.js`, `sw.js` and both JSON files parse cleanly.

---

## 3. Broken or fragile

Severity: **C** = critical (wrong or no data), **H** = high, **M** = medium, **L** = low.

| # | Sev | Where | Problem |
|---|---|---|---|
| C1 | C | `multivenue.py:419,474,485` | Calls `cls.load_symbols(cache_ttl=…)` and `fh.run_async()`. I believe neither method exists in cryptofeed 2.4.x, where the documented integration is `FeedHandler.run(start_loop=False)` and `Feed.symbols()`, a synchronous blocking HTTP call. I could not install cryptofeed here to confirm. If they are missing, **every market is skipped or the handler never starts**. The exception is swallowed into `last_error`, and the UI just shows "waiting for market data". **Please check:** does your v0.6 dashboard show non-zero depth per venue? What does `/api/state` → `last_error` say? |
| C2 | C | `run_windows.bat` step 3 | **Overwrites `config.json` with `config.example.json` on every start.** The backup file is overwritten on the next start too, so any edit you make is lost after two launches. |
| C3 | C | `app.py` persister + `/api/history` | SQLite writes, a full-table-scan `DELETE` every 5 s, and history queries all run **synchronously on the same event loop as the exchange feeds**. At scale, this stalls websocket processing and triggers exchange disconnects (numbers in §4). |
| C4 | H | `multivenue.py` metric / `_price_at` | The price deque holds 30 000 trades. For liquid pairs that covers only minutes, so 5-minute changes can be silently truncated, and 15m / 1h changes are impossible. The late-move penalty checks only **5m > 3 %**. A coin that is **+12 % in 1 h but flat for the last 5 min gets no penalty**, which is exactly the post-pump false positive you want to avoid. |
| C5 | H | `VenueState.update_book` | Diffs the **entire 500-level book** on every update. Far-from-mid levels, book re-windowing and **reconnect snapshots** all show up as "adds/removes". A reconnect produces a phantom ask-removal or replenishment spike. Fills and cancels are not separated, so there is no cancellation-intensity metric. |
| C6 | H | discovery | Runs **once at startup** and is never refreshed while running. `quote_to_usd` is frozen at discovery time, so BTC-, EUR-, KRW- and TRY-quoted pairs drift in USD terms. |
| C7 | H | baselines | Baselines are 1 s samples kept in memory and **lost on every restart**, so every restart means another 30-minute warm-up. They also include stale-feed periods and the anomaly itself; a slow 2-hour bleed gets absorbed into its own baseline. |
| C8 | H | composite scoring | Buy-ratio (60 %) and imbalance thresholds are **absolute**, not relative to each venue's normal. Structurally imbalanced books score permanently. `book_confidence` is computed but **not used** in the composite, so tiny books can still earn the full 28 ask-thinning points. The spread component from v0.3 was lost. There is no persistence requirement, so a single sample can cross 55/70/80. |
| C9 | M | `_book_cb` / `_trade_cb` | Trade windows use the exchange timestamp: local clock skew on Windows distorts the 60 s windows. When the direct lookup misses, there is an O(N) fallback scan per message. |
| C10 | M | `VENUE_ALIASES` | Crypto.com aliases (`CRYPTOCOM`) appear never to match cryptofeed's `CRYPTODOTCOM`, so the venue is silently unused. There is no startup report of which adapters actually resolved. |
| C11 | M | `app.js render()` | Rebuilds every card's `innerHTML` every second. The **"Exchange breakdown" `<details>` collapses each second**, and all 100 cards redraw every second at Top-100. |
| C12 | M | `app.js connect()` | Adds a new `setInterval` ping on every reconnect and never clears it (timer leak). |
| C13 | M | history API | 7-day view uses **stride sampling**, so a 3-minute score spike can disappear entirely from the 7d chart. |
| C14 | M | events | Confirmed-venue changes (1↔2) have no hysteresis or debounce. This produces event spam, which gets much worse with 100 coins. |
| C15 | M | `onchain.py` | Makes one API call per wallet, not per token. The `seen` set grows without bound. Events are in memory only (not persisted and not in history). Ethereum ERC-20 only, with no counterparty labels and no classification. |
| C16 | M | broadcaster | Sends the **full state** (every coin, venue rows, skipped markets) to every client every second, sequentially, so one slow phone stalls everyone. |
| C17 | L | `ios/` | Decodes a `symbols` key that the server stopped sending in v0.4, so **the iOS client shows nothing**. |
| C18 | L | misc | Version labels are inconsistent (HTML says v0.4, FastAPI says 0.4.0, README says v0.6). Uses the deprecated `@app.on_event`. Asset/venue strings are interpolated into HTML unescaped, which matters once symbols come from an external API. `scanner.py` is dead code. The batch-file error message tells users to paste output "back to ChatGPT". |

---

## 4. What fails at Top 100 (measured)

Setup: stdlib benchmark of the v0.6 code paths (`VenueState` imported with cryptofeed stubbed), Python 3.11, one core, assuming 100 coins × 4 venues = 400 venue states.

| Path | Measured | At Top 100 |
|---|---|---|
| `metric()` with a full 2 h baseline (median over 7 200 dicts, 3 metrics) | 4.8 ms per venue per sample | **~1.9 s of CPU per 1 s tick**. The loop can never keep up. |
| `metrics_history` (≤12 000 dicts × 36 keys) | ~0.9 KB/dict → 11 MB per venue | **~4.2 GB RAM** |
| `update_book` full diff (500 levels/side), excluding the cryptofeed → dict copy | ≥0.29 ms per update | 57 % of a core at 5 updates/s/venue; **2.3–5.7 cores** at 20–50 updates/s |
| SQLite, 1 day of Top-100 rows (v0.6 schema) | 1.7 M composite + 6.9 M venue rows = 1.0 GB | **~7 GB for 7 days** |
| Retention `DELETE … WHERE ts<?` (no index usable), every 5 s | 0.49 s at 1 day of data | **~3.4 s every 5 s at 7 days**, blocking the loop |
| `/api/history` for 1 asset, 1 day, rows materialised in Python | 0.9 s | **~6 s for 7d**, blocking the loop |

Other scaling failures:

- **CoinGecko:** v0.6 makes up to 2 calls per coin (search + tickers) at 1.2 s spacing. For 100 coins that is ~200 calls in ~4 minutes, which the free tier rejects with HTTP 429.
- **Exchange limits:** one connection per exchange carrying all symbols breaks per-connection subscription caps. MEXC, for example, allows roughly 30 streams per connection, and KuCoin, OKX and Bybit all have topic and subscribe-rate limits.
- **Raw-message deques:** `trades` (40k), `ask_add`/`ask_remove` (80k) and `prices` (30k) silently truncate on active markets, so the sums undercount without any error.
- **Event volume and UI payload** grow linearly with the number of coins, with no debouncing and no diffing.

---

## 5. Proposed v0.7 architecture

```
                 ┌──────────────── Universe service (hourly) ─────────────────┐
CoinGecko ──────►│ Top-N by mcap (1 call) − stablecoins − wrapped/LST/bridged │──► eligible assets
(rate-limited)   │ + pinned watchlist (QNT, XDC…) + rank hysteresis           │    (+ reasons)
                 └────────────────────────────┬───────────────────────────────┘
                 ┌──────────────── Venue discovery (every 30–60 min) ─────────┐
Exchange REST ──►│ 1 all-tickers call per exchange → per-coin ranking by live │──► desired markets
(ccxt)           │ USD volume, identity check, quality filters, hysteresis    │    per coin (3–5)
                 └────────────────────────────┬───────────────────────────────┘
                 ┌──────────────── Feed manager ───────────────────────────────┐
Exchange WS ────►│ per-exchange partitions (N symbols/connection), supervisor, │
                 │ backoff + jitter, circuit breaker, subscription diffing,    │
                 │ verification (book + update within 60 s), health states     │
                 └────────────────────────────┬────────────────────────────────┘
                                              │ normalized events (book delta / trade)
                 ┌──────────────── Market engine (per coin×venue) ─────────────┐
                 │ band-limited book (±2 %) → depth bands, adds/removes,       │
                 │ fills vs cancels, slippage; 1 s buckets (ring buffers);     │
                 │ 1-min aggregates → robust baselines (30 m / 2 h / 24 h)     │
                 └────────────────────────────┬────────────────────────────────┘
                      1 Hz feature rows       │   (in-process now; worker processes for Top 250/500)
                 ┌──────────────── Scoring engine (every 1–2 s) ───────────────┐
Wallet intel ───►│ per-venue flags → cross-venue aggregation → sub-scores →    │──► Top-anomalies table
(Etherscan)      │ gates/caps → late-move penalty → status machine → reasons   │──► event detector → timeline
                 └────────────────────────────┬────────────────────────────────┘
                 ┌──────────────── Storage (dedicated writer thread) ──────────┐
                 │ SQLite WAL, batched inserts, 5 s → 1 min rollups, chunked   │
                 │ retention, read-only import of v0.6 DB                      │
                 └─────────────────────────────────────────────────────────────┘
API (FastAPI lifespan): /api/top, /api/coin/{a}, /api/history/{a}, /api/timeline/{a}, /api/health,
                        /api/universe, /ws (topic subscriptions: top | coin:{a} | health)
```

Proposed layout (inside `pre_move_scanner_v07_work/`):

```
server/
  app.py              thin FastAPI app (lifespan, routes, WS hub)
  config.py           defaults + validation; never overwrites the user's config.json
  universe/           coingecko.py (rate-limited client), universe.py, venues.py, fx.py
  feeds/              base.py (adapter interface, health), manager.py, ccxt_adapter.py, sim_adapter.py
  engine/             book.py, market_state.py, baselines.py, features.py, scoring.py, events.py, leadlag.py
  intel/              labels.py, etherscan.py, classify.py, balances.py, scores.py
  storage/            db.py, migrations.py, writer.py, rollups.py, history.py, import_v06.py
web/                  index.html (Top anomalies), coin view, timeline, health; charts.js reused from v0.6
tests/                unit + scenario tests (fixtures, SIM adapter)
tools/selftest.py     live checks you run on your machine (universe, venues, feeds, example coins)
```

**Key principle:** ingestion does only O(changed levels) work per message. Everything else runs on a fixed 1 Hz clock that is independent of UI traffic. Nothing blocking runs on the event loop.

---

## 6. Feed manager redesign

**Library recommendation: move from cryptofeed to `ccxt` (its bundled `ccxt.pro` websocket layer), behind our own adapter interface.**

| | cryptofeed (v0.6) | ccxt / ccxt.pro (proposed) |
|---|---|---|
| Status in v0.6 | API calls likely wrong (C1) | — |
| Windows install | C extensions (`order_book`, Cython); can need a compiler on new Python versions | Pure Python wheels |
| Discovery and streaming share one symbol namespace | No (CoinGecko names → alias tables) | Yes: `load_markets` / `fetch_tickers` use the same symbols as `watch_*` |
| Coverage for XDC / QNT-type venues | Binance, OKX, Coinbase, Kraken, KuCoin, Gate… (no MEXC, Bitrue…) | All of those plus MEXC, Bitget, HTX, Upbit, Bithumb, Bitrue, … |
| Maintenance | Slow | Very active |
| Raw performance | Faster (C types, delta callbacks) | Slower per message; mitigated by band-limited processing and worker processes |

Trade-off: ccxt does not expose raw book deltas. v0.7 diffs only the **±2 % band** (typically 20–200 levels, not 1 000) on each update. That keeps add/remove/cancel accounting cheap and ignores far-book noise. The adapter interface keeps a native fast path possible later: `server/scanner.py` already contains a correct native Binance diff-depth sync (U/u sequencing plus snapshot resync), which can be revived as a fast adapter.

**Design details:**

1. **Partitions.** Subscriptions are grouped by exchange, then chunked into connections. A per-exchange policy table sets symbols per connection, subscribe messages per second and REST weight budget (for example, MEXC about 30 streams per connection). Each partition is one supervised task. A failure in one partition affects only that partition.
2. **Supervisor.** Exponential backoff with jitter (1 s → 60 s). A circuit breaker opens after N consecutive failures (5-minute cooldown, shown in the UI). Per-exchange counters track reconnects, errors, last message time and message rate.
3. **Resync hygiene.** After a reconnect or new snapshot, the market's flow accumulators are reset and flow metrics are excluded for a 60 s grace period (fixes C5's phantom spikes). The market's state is `RESYNCING`.
4. **Clock.** Receipt time is used for all windows. Exchange-vs-local lag is recorded as a health metric.
5. **Health state per market:**
   - `LIVE`
   - `STALE`: socket alive but no book update for more than max(20 s, 5× the market's own typical inter-update gap)
   - `DISCONNECTED`
   - `RESYNCING`
   - `WARMING`
   - `UNAVAILABLE` (with a reason)

   Per coin: `LIVE`, `PARTIAL` (some venues down), `LOW CONFIDENCE`, or `NO COVERAGE`. **Stale or resyncing markets never contribute to scores, confirmations or baselines.**
6. **Subscription diffing.** When discovery changes a coin's venues, only the affected partitions resubscribe. Everything else keeps streaming and keeps its baselines.
7. **Backpressure.** Ingestion is cheap, so no raw-message queue is needed in-process. In worker-process mode (Top 250/500), workers send only **1 Hz feature rows** through a bounded queue (drop-oldest, counted). WebSocket clients each get a latest-wins queue of depth 1–2, so a slow client drops frames instead of stalling others. Drop counters appear on the health page.
8. **Scale path.** `feed_workers: 1` (in-process) by default. `feed_workers: N` shards exchanges across N processes, each running feeds plus the market engine. Scoring and storage stay in the main process. Same code, different transport.

---

## 7. Universe and exchange/symbol discovery

**Universe (hourly):**

- One CoinGecko `/coins/markets` call (top 250) gives ids, symbols, market cap, price, and 1h/24h change.
- Exclusion lists are cached daily: CoinGecko categories (stablecoins, wrapped, bridged, liquid-staking / restaking, tokenized gold/BTC) plus a static fallback list, name patterns ("Wrapped", "Bridged", "Staked") and a peg heuristic. `include` and `exclude` overrides live in config.
- Hysteresis keeps coins from flapping in and out: a coin enters at rank ≤ 100 on 2 consecutive refreshes and leaves only above rank 115.
- **Pinned watchlist** (your v0.6 `watchlist`: QNT, LINK, XDC) is always scanned, even if a coin drops out of the Top 100.
- Every excluded or skipped coin is kept with a reason (`stablecoin`, `wrapped`, `no usable realtime venue`, `too illiquid`, …). Coverage is never fabricated.
- CoinGecko budget: about 1–2k calls/month (well inside free limits). An optional free Demo API key is supported.

**Venue discovery per coin (every 30–60 min, staggered):**

1. **Exchange catalog.** For each supported exchange, `load_markets` (daily) and **one** `fetch_tickers` call give 24h quote volume, bid/ask and last price for **every** spot pair. That is about 15 calls in total instead of about 200.
2. **Identity check.** Coins are matched by symbol plus per-exchange aliases (e.g. renamed tickers). The exchange price must be within ±5 % of CoinGecko's USD price (±12 % for KRW venues because of the local premium). This rejects **ticker collisions**, where a different token uses the same symbol.
3. **USD conversion** uses live FX: stablecoin quotes ≈ 1 with depeg checks; BTC, ETH, EUR, KRW and TRY quotes use live cross rates from the same catalog, refreshed every discovery cycle and on each tick for crypto quotes.
4. **Ranking.** Candidates are ranked by **that coin's own** USD volume per exchange, keeping one best pair per exchange. Quality filters: a minimum volume, spread ≤ 1 %, a recent last trade, and a **wash-volume sanity check** (24h volume ÷ ±2 % depth above a threshold demotes the venue). Up to 3–5 venues per coin.
5. **Realtime verification.** After subscribing, a market must deliver a book snapshot plus an update within 60 s, or it is demoted and the next candidate is promoted, with the reason recorded.
6. **CoinGecko ticker cross-check** runs once a day per coin. It flags `is_anomaly`/`is_stale` markets and lists top markets on exchanges we can't stream (shown as "top market unsupported").
7. **Change hysteresis.** A new venue replaces an existing one only if it has ≥ 1.5× the volume on 2 consecutive refreshes. Delisted or inactive markets are dropped immediately.

So XDC never gets Binance unless Binance actually lists XDC. Its venues come from where XDC itself trades.

---

## 8. Scoring architecture

### 8.1 Per-market features (every 1 s, from 1 s buckets and ring buffers)

- **Book:** depth ±0.5 / 1 / 2 % (bid and ask, USD), best bid/ask, spread, imbalance, simulated **buy-side slippage** for a coin-scaled order size.
- **Book flow in the ±2 % band:** ask/bid added and removed. `ask_filled` is taken from aggressive buys; `ask_cancelled` = max(0, removed − filled). Also cancellation intensity and replenishment (added ÷ removed, only once removal is material).
- **Tape:** aggressive buy/sell USD, trade count, average size, **largest-trade share**, volume acceleration.
- **Price:** returns over 1 / 5 / 15 / 30 / 60 min from a 1-hour 1 s mid-price ring, plus realized range (for compression).

### 8.2 Baselines (per coin × venue × metric)

- 1-minute aggregates (not 1 s samples, which cuts memory about 60×) feed robust **median + MAD** over **30 min / 2 h / 24 h**.
- The reference window is **lagged**: 2 h ending 10 min ago. An unfolding anomaly is compared with a clean reference instead of absorbing itself.
- Stale and resync minutes are excluded.
- Baselines are **rehydrated from SQLite 1-minute rollups on restart**, so a quick restart does not need a new 30-minute warm-up.
- Each feature is expressed both as a ratio to its median (readable: "ask depth −42 %") and as a robust z-score (is −42 % actually unusual *for this market*?).

### 8.3 Per-venue flags (each with strength 0–1 and a confidence)

| Flag | Meaning |
|---|---|
| `ASK_THINNING` | Ask depth well below the venue's own normal |
| `ASK_NOT_REPLENISHING` | Asks consumed or cancelled and not refilled |
| `ASK_PULLED` | Cancel spike on the ask side |
| `BID_SUPPORT` | Bid depth ≥ baseline while asks weaken |
| `BUY_PRESSURE` | Taker-buy share and net flow above **this venue's baseline share**, not an absolute 60 % |
| `VOLUME_ACCEL` | Volume rising versus baseline |
| `SLIPPAGE_UP` | Buying the same size costs more than normal |
| `SPREAD_WIDENING` | Low weight: can mean risk-off as well as pre-move |

**Venue confidence** combines:

- activity versus coin-scaled floors, e.g. floor = clamp(0.5 × baseline minute volume, $2k, $25k);
- trade-count floor;
- absolute book size;
- a concentration penalty when a single print exceeds 40 % of the minute's volume ("one unusual trade");
- warm-up state and data health.

### 8.4 Sub-scores (0–100; `null` = no reliable data, never 0 by default)

| Score | What it measures |
|---|---|
| **ORDER-BOOK** | Directional upside asymmetry: ask thinning, non-replenishment, ask pulls, bid support versus ask weakness, buy slippage rising |
| **LIQUIDITY** | How *movable* the market has become versus normal (both-side thinning, slippage, spread). This is not a quality score. |
| **BUY PRESSURE** | Relative taker-buy share and net flow, confidence-weighted. **Buying with flat price counts strongly only when asks are *not* replenishing**; with heavy ask refill it is a passive seller absorbing, which is not bullish. |
| **CROSS-VENUE** | Share of the coin's liquidity whose venues confirm (≥ 2 independent flags), number of confirming venues, and leader → follower propagation within 15 min |
| **MM / WHALE / CEX FLOW / SCARCITY** | From wallet intelligence (§10); `null` where unsupported |
| **CONFIDENCE** | Shown as a badge; drives gating |

### 8.5 PRE-MOVE score (the ranking key): conservative by construction

1. **Base** = weighted structure sub-scores: order-book 35 %, cross-venue 25 %, buy pressure 20 %, liquidity 20 %.
2. **Independence caps** by the number of distinct signal *families* active (book thinning, non-replenishment, flow, volume, slippage, cross-venue): 1 family → ≤ 35, 2 → ≤ 55, 3 → ≤ 72, 4+ → uncapped.
3. **Venue caps** (kept from v0.6): 1 live venue → ≤ 42. Confirmations: 0 → ≤ 39, 1 → ≤ 59, 2 → ≤ 82. New: if confirming venues hold < 30 % of the coin's liquidity → ≤ 60.
4. **Confidence**: multiplicative, plus hard caps (activity confidence < 0.2 → ≤ 32).
5. **On-chain context** adjusts the score by at most ±8 points and can never create a high score on its own.
6. **Late-move penalty.** Late index `L = max(r15/5 %, r30/8 %, r60/12 %)`, using your thresholds. Down-moves count at 0.7× weight.
   - L ≤ 0.4: no penalty.
   - 0.4–1: linear down to ×0.25 (**MOVE IN PROGRESS**).
   - L ≥ 1: capped at 25 (**LATE / MOVE ALREADY EXPANDED**).
7. **Compression bonus.** If the price range over 30–60 min is ≤ 0.6× its 24 h normal *and* structure is strong: up to +10 %. This rewards "abnormal structure + price still flat".
8. **Persistence.** The ranked score is the 3-minute rolling median, so single-sample spikes can't rank.
9. **Status machine** (with persistence and hysteresis):

   | Status | Condition |
   |---|---|
   | `WARMING` | Baselines not established |
   | `LOW CONFIDENCE` | Weak data |
   | `WATCH` | ≥ 45 for 2 min |
   | `EARLY` | ≥ 55, 2+ families, 2+ venues, L < 0.4 |
   | `STRONG PRE-MOVE` | ≥ 70, 3+ families, ≥ 2 confirmed venues holding ≥ 50 % of liquidity, L < 0.4, held 3 min |
   | `MOVE IN PROGRESS` | 0.4 ≤ L < 1 |
   | `LATE` | L ≥ 1 |

   LATE rows sort below all non-late rows.
10. **Reason column.** Generated from the top contributing features using templates, e.g. "Ask depth −42 % across 4 venues; price +0.4 % (1h)", "3/5 venues show weak ask replenishment", "Volume 3.1× normal but price compressed", "OKX led; Gate & KuCoin confirmed within 6 min".

### 8.6 Your two reference cases (these become unit tests)

| Case | Expected result |
|---|---|
| 100 % buys, $70, 2 trades | activity confidence ≈ 0.08 → buy pressure ≈ 0, one family at most, confidence cap → **< 15, LOW CONFIDENCE** |
| 70 % buys, $500k, hundreds of trades, asks −45 %, 3–4 venues confirming, price +0.4 % | 4+ families, cross-venue high, confidence ≈ 1, compression bonus → **75–90, STRONG PRE-MOVE** |
| Same as above but +9 % in 30 min | L ≈ 1.1 → **≤ 25, LATE** |

### 8.7 Outcome logging (for calibration)

Every status transition is stored together with the forward returns that follow (15 m / 1 h / 4 h / 24 h, filled in later). This lets the thresholds be tuned from evidence instead of guesswork. v0.6's README listed this as a TODO.

---

## 9. False-positive controls (summary)

- Coin-scaled activity and trade-count floors, plus a single-print concentration penalty.
- Absolute book-size confidence (restored from v0.3 and actually used).
- Venue baselines are relative (buy share, imbalance, depth, spread are all measured against the venue's own normal).
- Independent-family caps, venue-count caps and a liquidity-share confirmation cap.
- Persistence (3-minute median) plus status hysteresis.
- Stale, resync and warm-up exclusion; lagged baselines.
- Buy-pressure absorption logic (it requires non-replenishment).
- The late-move penalty uses 15 / 30 / 60 min returns.
- Wash-volume and ticker-collision filters in venue selection.
- On-chain signals are bounded context only.
- Outcome logging to measure hit rates later.

---

## 10. Wallet / MM / CEX intelligence

**Principle:** evidence-carrying, attribution-first, `UNKNOWN` by default.

1. **Label registry** (SQLite, seeded from an editable CSV/JSON). Each entry records:
   - `address`, `chain`, `entity` (e.g. Coinbase, Wintermute);
   - `entity_type`: `CEX_HOT`, `CEX_COLD`, `CEX_CUSTODY` (e.g. Prime), `MM`, `WHALE`, `CUSTODY_INSTITUTIONAL`, `PROTOCOL_TREASURY`, `DEX_POOL`, `BRIDGE`, `BURN`;
   - `source`, `confidence` (HIGH/MEDIUM/LOW) and notes.

   I will **not** invent addresses from memory. The seed contains your Wintermute QNT address plus an import tool. Any starter exchange-wallet list is marked "verify before trusting", and LOW-confidence labels cannot drive scores.
2. **Token watchers (Etherscan V2).** Poll **per token contract**, not per wallet, with a `startblock` cursor and a minimum USD size filter. Transfers are deduplicated by a unique `(chain, tx_hash, log_index)` key in the DB. About 30–40 Top-100 ERC-20 tokens polled every 60 s fits inside the free-tier daily budget.
   - Unsupported in v0.7: non-EVM chains (BTC, XRP, SOL, ADA, TRX, **XDC native**…). They are shown as "chain not supported".
   - Etherscan's free-tier chain coverage has changed over time, so the watcher reports per-chain `NOTOK` responses instead of failing silently.
3. **Balances.** Balances are anchored by a `tokenbalance` snapshot every 6 h and updated continuously from the transfer stream. That gives balance now, Δ1h / Δ6h / Δ24h / Δ7d, per address, per entity (all Coinbase wallets) and per entity type (CEX reserve for the token, MM inventory).
4. **Classifier.** A pure function with extensive unit tests. Every output carries a class, confidence, explanation and tx hash.

   | Flow | Class |
   |---|---|
   | Swap with a labelled DEX pool/router, token in | `BUY` (conf MEDIUM; full swap decoding later) |
   | Swap with a labelled DEX pool/router, token out | `SELL` |
   | CEX → labelled whale / custody (non-exchange, non-MM) | `ACCUMULATION-SIDE` |
   | Labelled whale / custody → CEX | `DISTRIBUTION-SIDE` |
   | CEX→CEX, same-entity hot↔cold, **Coinbase Hot → Coinbase Prime**, MM↔CEX, custody internal | `SHIFT` (never accumulation) |
   | CEX → *unlabelled* address, or anything else | `UNKNOWN` (shown as context: "large CEX withdrawal to unlabelled address", never scored as buying) |

5. **Scores:**

   | Score | How it is computed |
   |---|---|
   | **MM score** | Unusualness of **net** MM inventory change, plus a **directionality ratio** = \|net\| ÷ gross. The QNT/Wintermute pattern (exchange → Wintermute → another exchange) has high gross flow and low net, so it is labelled `ROUTING / REBALANCING`, not directional. |
   | **CEX flow score** | Net change in labelled exchange reserves **excluding SHIFTs**, measured against its own baseline |
   | **Whale score** | Net accumulation-side minus distribution-side flow, **plus the count of distinct whales on each side**. Simultaneous accumulation and distribution are shown explicitly (e.g. "3 accumulating / 2 distributing → net small"). |
   | **Scarcity / supply-drain** | `REAL SUPPLY DRAIN` only when CEX reserves fall net of shifts, the recipients are non-exchange and non-MM, the tokens are not returned within X hours, **and** order-book asks thin at the same time. Otherwise `RESHUFFLING`. |

6. All wallet events go into the same timeline as the microstructure events.

---

## 11. Storage architecture

- **SQLite in WAL mode** with `synchronous=NORMAL`. A **dedicated writer thread** takes batched inserts from a queue. Reads run in a thread pool on separate connections. **Nothing blocks the event loop.**
- **New file:** `data/scanner_v07.db`. Versioned migrations are recorded in a `schema_migrations` table.
- **Tables** (all new; v0.6 tables are never altered):

| Table | Cadence | Retention (configurable) |
|---|---|---|
| `asset_metrics_5s`: all sub-scores, status, price, returns, key features (columns, not JSON) | 5 s | 48 h |
| `market_metrics_10s`: per venue | 10 s | 24 h |
| `asset_metrics_1m`: avg / max / last | 1 min | 30 days |
| `market_metrics_1m`: also used for baseline rehydration | 1 min | 14 days |
| `events`: timeline (category, type, severity, venue, value, message, evidence JSON) | on event | 90 days |
| `universe_snapshots`, `venue_selection`: discovery decisions and reasons | on refresh | 90 days |
| `feed_health_1m` | 1 min | 14 days |
| `wallet_labels`, `onchain_transfers`, `wallet_balances`, `entity_flows_1h` | on event / hourly | 90 days |
| `signal_outcomes` | on status change | forever (small) |

- **Estimated steady-state size at Top 100:** about 1–1.5 GB, versus about 7 GB for v0.6 with less data. Retention deletes run in chunks every 10 minutes on the writer thread.
- **History API.** Buckets in SQL (`GROUP BY ts/bucket`) using **avg + max** (spikes can't vanish) and picks the table by range:
  - 1h / 6h → 5 s data
  - 24h → 1 min data
  - 7d → 1 min data bucketed to 5–10 min

  About 1 500 points per series.
- **v0.6 history preserved.** `tools/import_v06.py --from <v0.6 folder>\scanner.db` opens your v0.6 DB with SQLite **`mode=ro`** and copies `composite_history_v2`, `venue_history_v2` and `scanner_events_v2` verbatim into legacy tables in the v0.7 DB. Charts show v0.6 data before the switchover, with a marker, labelled "v0.6 score (different formula)".

---

## 12. UI

- **Main screen: "TOP ANOMALIES RIGHT NOW".** A sortable table: Rank, Coin, Pre-Move, Liquidity, Order-book, MM, Whale, CEX Flow, Price 15m, Price 1h, Confirmed venues (e.g. 3/4), Reason, Status, Confidence.
  - Filters: hide LATE, minimum confidence, status.
  - Keyed row updates (no full re-render, open panels stay open).
  - Stale or partial coverage is shown visibly.
- **Coin detail** (click a row, or `#/coin/QNT`):
  - score breakdown with the contributing features;
  - venue coverage and health;
  - a per-venue book panel (depth bands plus a mini depth histogram);
  - charts, all reusing the v0.6 canvas code: depth history, spread, replenishment, aggressive flow, volume, **price/score overlay**, per-venue ask depth;
  - a lead/lag panel;
  - MM / CEX / whale events and wallet balance Δ tables;
  - the **chronological timeline**, including an automatic "what preceded this breakout" look-back.
- **Health page:** per-exchange connection state, message rates, reconnects, stale markets, dropped frames, discovery status, CoinGecko/Etherscan budget use.
- **WebSocket topics:** `top` (compact, 1 Hz), `coin:{asset}` (only for the open coin) and `health`. That is about 20–30 KB/s instead of hundreds of KB/s.

---

## 13. What v0.7 preserves from v0.6

- Per-coin volume-ranked venue selection, with "skip, don't substitute" and transparency about skipped markets.
- The liquidity-weighted composite built from per-venue baselines (sum of baselines).
- The trade-confidence concept (notional × count) and every existing cap (coverage, confirmations, warm-up, low tape). They are extended, not removed.
- Replenishment `n/a` until material removal.
- All history charts (price, score, ask-depth / buy-ratio / relative-volume, 60 s volume + confirmed venues, per-venue ask depth), the 1h/6h/24h/7d buttons, and the event markers and event list.
- The v0.6 DB tables (imported read-only) and the ability to read them in charts.
- The dark theme, vanilla JS (no build step), PWA, LAN/iPhone browser access and the Windows one-click launcher (fixed).
- The Etherscan watcher's conservative semantics (extended).
- `scanner.py`'s native Binance sync logic, kept as a reference for a future fast adapter.

---

## 14. Migration plan

1. All work happens in `pre_move_scanner_v07_work/`. v0.6 is read-only; its hashes are checked at delivery.
2. **Config.** The new `config.example.json` adds sections: universe, discovery, feeds, scoring, storage, intel. The loader merges defaults, and old keys still work: `watchlist` → `pinned_assets`, `coingecko_ids` → overrides, `onchain` → intel. `run_windows.bat` creates `config.json` **only if it is missing**.
3. **Database.** v0.7 uses its own DB file. Your v0.6 DB is imported read-only on request. No destructive migration.
4. **Dependencies.** Add `ccxt` and `numpy` (lead/lag and statistics), plus `pytest` for development. Remove `cryptofeed`. Keep fastapi, uvicorn, httpx and websockets, updated to current versions.
5. **iOS.** Minimal update so the client decodes the new `top` payload, or it is documented as unsupported. Your call.

---

## 15. Implementation phases (after approval)

| Phase | Content | Tests |
|---|---|---|
| P1 | Market engine: band-limited book, 1 s buckets, fills vs cancels, slippage, baselines + rehydration, SIM adapter | Book accounting, resync, baselines, memory bounds |
| P2 | Scoring: flags, sub-scores, caps, late penalty, compression, persistence, status machine, reasons | **$70/2-trade case, $500k/4-venue case, late case**, stale/warm-up suppression |
| P3 | Storage: WAL writer, schema, rollups, retention, history API, v0.6 read-only import | Migration against a synthetic v0.6 DB; v0.6 DB file hash unchanged |
| P4 | Universe + venue discovery: CoinGecko client, filters, catalog, identity check, FX, hysteresis | Recorded-fixture tests (XDC not on Binance, ticker collision, stablecoin/wrapped exclusion) |
| P5 | Feed manager: ccxt adapter, partitions, supervisor, health, subscription diffing, workers | Fake-adapter fault injection (disconnect, stale, delisting) |
| P6 | Wallet intelligence: labels, Etherscan token watcher, balances, classifier, scores | QNT/Wintermute routing, Coinbase Hot→Prime = SHIFT, simultaneous accumulation/distribution |
| P7 | UI: Top table, coin detail, timeline, health; API routes | FastAPI TestClient, JS syntax, headless browser smoke test (Chromium is available) |
| P8 | `tools/selftest.py`, docs, Windows launcher, CHANGELOG, ZIP, v0.6 hash check | — |

---

## 16. Decisions needed from you

1. **Approve the architecture**, and specifically the **switch from cryptofeed to ccxt** (§6).
2. **Network access for testing.** This environment currently blocks PyPI and all market-data APIs. Options:
   - **(a)** Allow full network access (simplest), or allow at least `pypi.org` + `files.pythonhosted.org` + `api.coingecko.com` + `api.etherscan.io` + the exchange REST/WS hosts. I can then do the live Top-100, venue-discovery and websocket verification here.
   - **(b)** Allow only PyPI. The backend, API, websockets, DB, scoring and UI get tested end-to-end here against the SIM adapter and recorded fixtures. The live checks run on your PC via `tools/selftest.py`.
   - **(c)** Neither. Only stdlib unit tests can run here; FastAPI can't be started here.
3. **Universe definition.** Either "Top 100 by market cap minus exclusions" (about 80–85 coins, my default), or back-fill to 100 eligible coins from ranks 101+? Pinned coins are added in both cases.
4. **Wallet labels.** Do you have address lists (e.g. from your QNT research, or Arkham/Etherscan exports) to seed the registry? Without them, the MM / Whale / CEX scores will be `n/a` for most coins, which is correct but sparse.
5. **iOS client:** update minimally, or leave as unsupported?
