from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Deque, Tuple, Any, Optional, List
from pathlib import Path

from cryptofeed import FeedHandler
from cryptofeed.defines import L2_BOOK, TRADES
from cryptofeed.exchanges import EXCHANGE_MAP

from .discover import VenueDiscoverer


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def pct_change(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b / a - 1.0) * 100.0


def norm_name(x: str) -> str:
    return "".join(ch for ch in str(x).upper() if ch.isalnum())


VENUE_ALIASES = {
    "Binance": {"BINANCE"},
    "OKX": {"OKX", "OKEX"},
    "Bybit": {"BYBIT"},
    "Coinbase": {"COINBASE"},
    "Gate": {"GATEIO", "GATE"},
    "MEXC": {"MEXC"},
    "KuCoin": {"KUCOIN"},
    "Kraken": {"KRAKEN"},
    "Bitget": {"BITGET"},
    "Crypto.com": {"CRYPTOCOM", "CRYPTOCOMEXCHANGE"},
    "Bitfinex": {"BITFINEX"},
    "Bitstamp": {"BITSTAMP"},
    "Upbit": {"UPBIT"},
    "HTX": {"HTX", "HUOBI"},
    "Gemini": {"GEMINI"},
    "Poloniex": {"POLONIEX"},
    "Phemex": {"PHEMEX"},
    "Bithumb": {"BITHUMB"},
}


@dataclass
class VenueState:
    asset: str
    venue: str
    exchange_id: str
    symbol: str
    quote_to_usd: float = 1.0
    discovery_volume_24h_usd: float = 0.0

    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    last_price: float = 0.0
    last_book_ts: float = 0.0
    last_trade_ts: float = 0.0

    # (ts, quote_notional, aggressive_buy, price)
    trades: Deque[Tuple[float, float, bool, float]] = field(default_factory=lambda: deque(maxlen=40000))
    prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=30000))

    # order-book notional added/removed, inferred by comparing successive L2 snapshots
    ask_add: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=80000))
    ask_remove: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=80000))
    bid_add: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=80000))
    bid_remove: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=80000))

    metrics_history: Deque[Dict[str, float]] = field(default_factory=lambda: deque(maxlen=12000))
    last_sample_ts: float = 0.0
    cached_metric: Dict[str, Any] = field(default_factory=dict)

    def _purge(self, dq: Deque, cutoff: float):
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def update_book(self, bids: Dict[float, float], asks: Dict[float, float], ts: float):
        # Compare snapshots. This works across normalized cryptofeed L2 books
        # regardless of the exchange's native delta/snapshot format.
        old_bids, old_asks = self.bids, self.asks

        if old_asks:
            keys = set(old_asks) | set(asks)
            for p in keys:
                old = old_asks.get(p, 0.0)
                new = asks.get(p, 0.0)
                if new > old:
                    self.ask_add.append((ts, (new - old) * p * self.quote_to_usd))
                elif old > new:
                    self.ask_remove.append((ts, (old - new) * p * self.quote_to_usd))

        if old_bids:
            keys = set(old_bids) | set(bids)
            for p in keys:
                old = old_bids.get(p, 0.0)
                new = bids.get(p, 0.0)
                if new > old:
                    self.bid_add.append((ts, (new - old) * p * self.quote_to_usd))
                elif old > new:
                    self.bid_remove.append((ts, (old - new) * p * self.quote_to_usd))

        self.bids = bids
        self.asks = asks
        self.last_book_ts = ts

        if bids and asks and not self.last_price:
            self.last_price = ((max(bids) + min(asks)) / 2.0) * self.quote_to_usd

    def add_trade(self, ts: float, price: float, amount: float, side: str):
        quote = price * amount * self.quote_to_usd
        price_usd = price * self.quote_to_usd
        aggressive_buy = str(side).lower() == "buy"
        self.trades.append((ts, quote, aggressive_buy, price_usd))
        self.prices.append((ts, price_usd))
        self.last_price = price_usd
        self.last_trade_ts = ts

    def _depth(self, pct: float):
        if not self.bids or not self.asks:
            return 0.0, 0.0, 0.0, 0.0
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        mid = (best_bid + best_ask) / 2.0
        bid_floor = mid * (1.0 - pct)
        ask_ceil = mid * (1.0 + pct)
        bd = sum(p*q*self.quote_to_usd for p, q in self.bids.items() if p >= bid_floor)
        ad = sum(p*q*self.quote_to_usd for p, q in self.asks.items() if p <= ask_ceil)
        return bd, ad, best_bid, best_ask

    def _trade_window(self, now: float, start_s: float, end_s: float = 0.0):
        lo, hi = now - start_s, now - end_s
        buy = sell = 0.0
        count = 0
        for ts, quote, aggressive_buy, _ in reversed(self.trades):
            if ts < lo:
                break
            if ts <= hi:
                count += 1
                if aggressive_buy:
                    buy += quote
                else:
                    sell += quote
        return buy, sell, count

    def _price_at(self, target_ts: float):
        if not self.prices:
            return self.last_price
        candidate = self.prices[0][1]
        for ts, price in reversed(self.prices):
            candidate = price
            if ts <= target_ts:
                return price
        return candidate

    def metric(self, baseline_minutes: int, warmup_minutes: int):
        now = time.time()

        # Avoid adding multiple baseline samples because /api, websocket and DB
        # can request snapshots independently.
        if self.cached_metric and now - self.last_sample_ts < 0.8:
            return self.cached_metric

        self._purge(self.ask_add, now - 300)
        self._purge(self.ask_remove, now - 300)
        self._purge(self.bid_add, now - 300)
        self._purge(self.bid_remove, now - 300)

        bid05, ask05, best_bid, best_ask = self._depth(0.005)
        bid1, ask1, _, _ = self._depth(0.01)
        bid2, ask2, _, _ = self._depth(0.02)

        mid_quote = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        mid = mid_quote * self.quote_to_usd if mid_quote else self.last_price
        spread_bps = ((best_ask-best_bid)/mid_quote*10000.0) if mid_quote and best_bid and best_ask else 0.0
        imbalance = (bid1-ask1)/(bid1+ask1) if bid1+ask1 else 0.0

        b60, s60, n60 = self._trade_window(now, 60)
        bp, sp, np = self._trade_window(now, 120, 60)
        vol60 = b60+s60
        prev60 = bp+sp
        buy_ratio = b60/vol60 if vol60 else 0.5
        volume_accel = vol60/prev60 if prev60 > 1 else (2.0 if vol60 > 1000 else 1.0)

        p5 = self._price_at(now-300)
        ch5 = pct_change(p5, self.last_price) if p5 else 0.0

        def sum60(dq):
            return sum(v for ts, v in dq if ts >= now-60)

        ask_added = sum60(self.ask_add)
        ask_removed = sum60(self.ask_remove)
        bid_added = sum60(self.bid_add)
        bid_removed = sum60(self.bid_remove)

        min_ask_activity = max(250.0, ask1*0.03)
        min_bid_activity = max(250.0, bid1*0.03)
        ask_repl = ask_added/ask_removed if ask_removed >= min_ask_activity else None
        bid_repl = bid_added/bid_removed if bid_removed >= min_bid_activity else None

        cutoff = now-baseline_minutes*60
        baseline = [m for m in self.metrics_history if m["ts"] >= cutoff]
        asks = [m["ask_depth_1"] for m in baseline if m["ask_depth_1"] > 0]
        vols = [m["volume_60s"] for m in baseline if m["volume_60s"] > 0]
        spreads = [m["spread_bps"] for m in baseline if m["spread_bps"] >= 0]

        med_ask = statistics.median(asks) if len(asks) >= 30 else ask1
        med_vol = statistics.median(vols) if len(vols) >= 30 else vol60
        med_spread = statistics.median(spreads) if len(spreads) >= 30 else spread_bps

        ask_ratio = ask1/med_ask if med_ask > 0 else 1.0
        vol_ratio = vol60/med_vol if med_vol > 1 else 1.0
        spread_ratio = spread_bps/med_spread if med_spread > 0 else 1.0

        notional_conf = clamp(vol60/1500.0, 0, 1)
        count_conf = clamp(n60/12.0, 0, 1)
        trade_conf = math.sqrt(notional_conf*count_conf)
        book_conf = clamp((bid1+ask1)/25000.0, 0.2, 1.0)

        oldest = self.metrics_history[0]["ts"] if self.metrics_history else now
        age = now-oldest
        warmed = len(baseline)>=120 and age>=warmup_minutes*60

        metric = {
            "ts": now,
            "asset": self.asset,
            "venue": self.venue,
            "exchange_id": self.exchange_id,
            "symbol": self.symbol,
            "price": self.last_price or mid,
            "discovery_volume_24h_usd": self.discovery_volume_24h_usd,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": spread_bps,
            "spread_ratio": spread_ratio,
            "bid_depth_05": bid05,
            "ask_depth_05": ask05,
            "bid_depth_1": bid1,
            "ask_depth_1": ask1,
            "bid_depth_2": bid2,
            "ask_depth_2": ask2,
            "imbalance_1": imbalance,
            "buy_quote_60s": b60,
            "sell_quote_60s": s60,
            "buy_ratio_60s": buy_ratio,
            "trade_count_60s": n60,
            "volume_60s": vol60,
            "volume_accel": volume_accel,
            "volume_ratio": vol_ratio,
            "price_change_5m_pct": ch5,
            "ask_added_60s": ask_added,
            "ask_removed_60s": ask_removed,
            "ask_replenishment": ask_repl,
            "bid_replenishment": bid_repl,
            "ask_depth_ratio": ask_ratio,
            "trade_confidence": trade_conf,
            "book_confidence": book_conf,
            "baseline_samples": len(baseline),
            "warmed_up": warmed,
            "stale": (now-max(self.last_book_ts, self.last_trade_ts)) > 15,
        }

        self.metrics_history.append(metric)
        self.cached_metric = metric
        self.last_sample_ts = now
        return metric


class MultiVenueScanner:
    """
    One composite signal per asset, merging the five largest spot venues used
    in this build: Binance, OKX, Bybit, Coinbase and Gate.

    The composite is liquidity-weighted. A small venue cannot create a high
    signal on its own. High scores also require cross-venue confirmation.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.assets = [str(x).upper() for x in cfg.get("watchlist", ["QNT","LINK","XDC"])]
        self.max_venues = int(cfg.get("venue_discovery", {}).get("max_venues_per_asset", 5))
        self.baseline_minutes = int(cfg.get("baseline_minutes", 120))
        self.warmup_minutes = int(cfg.get("warmup_minutes", 30))
        self.root = Path(__file__).resolve().parents[1]
        self.discoverer = VenueDiscoverer(self.root, cfg)
        self.discovery = {"assets": {}}
        self.selected_markets: Dict[str, List[Dict[str,Any]]] = defaultdict(list)
        self.skipped_markets: Dict[str, List[Dict[str,Any]]] = defaultdict(list)

        self.states: Dict[Tuple[str,str], VenueState] = {}
        self.exchange_key_to_display: Dict[str,str] = {}
        self.symbol_to_asset: Dict[Tuple[str,str],str] = {}
        self.unavailable: Dict[str, List[str]] = defaultdict(list)
        self.last_error = ""
        self.feed_handler: Optional[FeedHandler] = None
        self._stop = asyncio.Event()

    def _find_exchange(self, display: str):
        aliases = VENUE_ALIASES.get(display, {norm_name(display)})
        for key, cls in EXCHANGE_MAP.items():
            nk = norm_name(key)
            nc = norm_name(getattr(cls, "__name__", ""))
            if nk in aliases or nc in aliases:
                return key, cls
        return None, None

    def _match_pair(self, symbols: List[str], asset: str, target: str):
        wanted = f"{asset.upper()}-{target.upper()}"
        if wanted in symbols:
            return wanted
        # Cryptofeed normalized spot symbols are normally BASE-QUOTE.
        # Keep this conservative: do not silently substitute a different quote.
        for s in symbols:
            parts = s.split("-")
            if len(parts) == 2 and parts[0].upper() == asset.upper() and parts[1].upper() == target.upper():
                return s
        return None


    @staticmethod
    def _levels(side, limit=500):
        out = {}
        try:
            n = min(len(side), limit)
            for i in range(n):
                p, q = side.index(i)
                p, q = float(p), float(q)
                if q > 0:
                    out[p] = q
        except Exception:
            # fallback for mapping-like implementations
            try:
                for i, (p, q) in enumerate(side.items()):
                    if i >= limit:
                        break
                    out[float(p)] = float(q)
            except Exception:
                pass
        return out

    async def _book_cb(self, book, receipt_timestamp):
        ex = str(book.exchange)
        symbol = str(book.symbol)
        asset = self.symbol_to_asset.get((ex, symbol))
        if not asset:
            # exchange id formatting can differ from EXCHANGE_MAP key
            for (k,s), a in self.symbol_to_asset.items():
                if norm_name(k)==norm_name(ex) and s==symbol:
                    asset=a
                    ex=k
                    break
        if not asset:
            return
        venue = self.exchange_key_to_display.get(ex, ex)
        st = self.states.get((asset, venue))
        if not st:
            return
        bids = self._levels(book.book.bids)
        asks = self._levels(book.book.asks)
        if bids and asks:
            st.update_book(bids, asks, receipt_timestamp or time.time())

    async def _trade_cb(self, trade, receipt_timestamp):
        ex = str(trade.exchange)
        symbol = str(trade.symbol)
        asset = self.symbol_to_asset.get((ex, symbol))
        if not asset:
            for (k,s), a in self.symbol_to_asset.items():
                if norm_name(k)==norm_name(ex) and s==symbol:
                    asset=a
                    ex=k
                    break
        if not asset:
            return
        venue = self.exchange_key_to_display.get(ex, ex)
        st = self.states.get((asset, venue))
        if st:
            ts = float(trade.timestamp or receipt_timestamp or time.time())
            st.add_trade(ts, float(trade.price), float(trade.amount), str(trade.side))

    async def run(self):
        try:
            self.discovery = await self.discoverer.discover(self.assets)
            fh = FeedHandler()
            self.feed_handler = fh

            # Build feeds per exchange, but choose venues PER ASSET by actual
            # current 24h market-pair volume, not by global exchange rank.
            subscriptions = defaultdict(list)  # exchange_key -> [(asset, display, row, symbol)]

            for asset in self.assets:
                discovered = self.discovery.get("assets", {}).get(asset, {})
                candidates = discovered.get("markets", [])

                selected = 0
                for row in candidates:
                    if selected >= self.max_venues:
                        break
                    display = row.get("venue")
                    if not display:
                        continue
                    key, cls = self._find_exchange(display)
                    if not cls:
                        bad = dict(row); bad["reason"] = "no realtime adapter"
                        self.skipped_markets[asset].append(bad)
                        continue

                    try:
                        await cls.load_symbols(cache_ttl=3600)
                        supported = list(cls.symbols())
                    except Exception as exc:
                        bad = dict(row); bad["reason"] = f"symbol load failed: {exc!r}"
                        self.skipped_markets[asset].append(bad)
                        continue

                    symbol = self._match_pair(supported, asset, row.get("target",""))
                    if not symbol:
                        bad = dict(row); bad["reason"] = "discovered pair not available in realtime adapter"
                        self.skipped_markets[asset].append(bad)
                        continue

                    st = VenueState(
                        asset=asset,
                        venue=display,
                        exchange_id=str(key),
                        symbol=symbol,
                        quote_to_usd=float(row.get("quote_to_usd") or 1.0),
                        discovery_volume_24h_usd=float(row.get("volume_24h_usd") or 0.0),
                    )
                    self.states[(asset, display)] = st
                    self.exchange_key_to_display[str(key)] = display
                    self.symbol_to_asset[(str(key), symbol)] = asset
                    self.selected_markets[asset].append(dict(row))
                    subscriptions[str(key)].append((asset, display, row, symbol, key))
                    selected += 1

                # Record unsupported top raw markets for transparency.
                selected_market_names = {x.get("market_name") for x in self.selected_markets[asset]}
                for raw in discovered.get("raw_top", [])[:10]:
                    if raw.get("market_name") not in selected_market_names and not raw.get("venue"):
                        bad = dict(raw); bad["reason"] = "exchange not supported by realtime feed library"
                        self.skipped_markets[asset].append(bad)

            # Add one feed per exchange, with all selected asset pairs.
            for ex_key, items in subscriptions.items():
                symbols = sorted({x[3] for x in items})
                key = items[0][4]
                try:
                    fh.add_feed(
                        key,
                        symbols=symbols,
                        channels=[TRADES, L2_BOOK],
                        callbacks={TRADES: self._trade_cb, L2_BOOK: self._book_cb},
                        max_depth=500,
                    )
                except TypeError:
                    fh.add_feed(
                        key,
                        symbols=symbols,
                        channels=[TRADES, L2_BOOK],
                        callbacks={TRADES: self._trade_cb, L2_BOOK: self._book_cb},
                    )

            await fh.run_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = repr(exc)


    async def close(self):
        self._stop.set()
        if self.feed_handler:
            try:
                await self.feed_handler.stop_async()
            except Exception:
                pass

    @staticmethod
    def _weighted_avg(rows, value_key, weight_key, default=0.0):
        num = den = 0.0
        for r in rows:
            v = r.get(value_key)
            w = r.get(weight_key, 0.0)
            if v is None:
                continue
            w = max(float(w or 0), 0.0)
            num += float(v)*w
            den += w
        return num/den if den else default

    def _composite(self, asset: str, rows: List[Dict[str,Any]]):
        rows = [r for r in rows if not r.get("stale") and r.get("price",0)>0]
        now = time.time()
        if not rows:
            return {
                "asset": asset, "score": None, "coverage": 0,
                "coverage_total": len(self.selected_markets.get(asset, [])), "venues": [],
                "unavailable": self.unavailable.get(asset, []),
                "status": "waiting for market data"
            }

        # Use actual depth for depth metrics.
        bid1 = sum(r["bid_depth_1"] for r in rows)
        ask1 = sum(r["ask_depth_1"] for r in rows)
        bid05 = sum(r["bid_depth_05"] for r in rows)
        ask05 = sum(r["ask_depth_05"] for r in rows)
        bid2 = sum(r["bid_depth_2"] for r in rows)
        ask2 = sum(r["ask_depth_2"] for r in rows)

        # Reconstruct each venue's baseline ask depth; then sum. This gives a
        # real liquidity-weighted cross-exchange ask-depth ratio.
        baseline_ask_sum = 0.0
        for r in rows:
            ar = r["ask_depth_ratio"]
            if ar and ar > 0:
                baseline_ask_sum += r["ask_depth_1"]/ar
        ask_ratio = ask1/baseline_ask_sum if baseline_ask_sum > 0 else 1.0

        buy_quote = sum(r["buy_quote_60s"] for r in rows)
        sell_quote = sum(r["sell_quote_60s"] for r in rows)
        volume = buy_quote+sell_quote
        buy_ratio = buy_quote/volume if volume else 0.5
        trades60 = sum(r["trade_count_60s"] for r in rows)

        # Aggregate volume baseline similarly.
        baseline_vol_sum = 0.0
        for r in rows:
            vr = r["volume_ratio"]
            if vr and vr > 0:
                baseline_vol_sum += r["volume_60s"]/vr
        volume_ratio = volume/baseline_vol_sum if baseline_vol_sum > 1 else 1.0

        imbalance = (bid1-ask1)/(bid1+ask1) if bid1+ask1 else 0.0

        # Replenishment is aggregated from raw additions/removals, not averaged.
        ask_add = sum(r["ask_added_60s"] for r in rows)
        ask_remove = sum(r["ask_removed_60s"] for r in rows)
        min_activity = max(1000.0, ask1*0.02)
        ask_repl = ask_add/ask_remove if ask_remove >= min_activity else None

        # Liquidity-weighted spread and price.
        for r in rows:
            r["_liq_weight"] = math.sqrt(max(r["bid_depth_1"]+r["ask_depth_1"], 1.0))
            r["_price_weight"] = max(r["volume_60s"], 100.0) * r["_liq_weight"]
        spread = self._weighted_avg(rows, "spread_bps", "_liq_weight")
        price = self._weighted_avg(rows, "price", "_price_weight")
        change5 = self._weighted_avg(rows, "price_change_5m_pct", "_price_weight")

        # Cross-venue agreement: the crucial v0.4 change.
        venue_flags = []
        for r in rows:
            flags = 0
            if r["ask_depth_ratio"] < 0.75:
                flags += 1
            if r["buy_ratio_60s"] > 0.60 and r["trade_confidence"] >= 0.45:
                flags += 1
            if r["volume_ratio"] > 1.6 and r["trade_count_60s"] >= 5:
                flags += 1
            if r["ask_replenishment"] is not None and r["ask_replenishment"] < 0.70:
                flags += 1
            venue_flags.append({
                "venue": r["venue"],
                "symbol": r["symbol"],
                "flags": flags,
                "confirmed": flags >= 2,
                "ask_depth_1": round(r["ask_depth_1"],2),
                "bid_depth_1": round(r["bid_depth_1"],2),
                "ask_depth_ratio": round(r["ask_depth_ratio"],3),
                "buy_ratio_60s": round(r["buy_ratio_60s"],4),
                "volume_60s": round(r["volume_60s"],2),
                "volume_ratio": round(r["volume_ratio"],3),
                "spread_bps": round(r["spread_bps"],3),
                "ask_replenishment": None if r["ask_replenishment"] is None else round(r["ask_replenishment"],3),
                "trade_count_60s": r["trade_count_60s"],
                "volume_24h_discovery_usd": round(r.get("discovery_volume_24h_usd",0),2),
            })

        confirmed_venues = sum(1 for x in venue_flags if x["confirmed"])
        coverage = len(rows)

        # Confidence from aggregate tape. $5k/min and ~30 prints is full
        # confidence across several venues; lower activity suppresses the score.
        trade_conf = math.sqrt(
            clamp(volume/5000.0,0,1) *
            clamp(trades60/30.0,0,1)
        )

        # Composite components.
        c_ask = clamp((0.72-ask_ratio)/0.47,0,1)*28
        c_buy = clamp((buy_ratio-0.58)/0.24,0,1)*18*trade_conf
        c_vol = clamp((volume_ratio-1.5)/3.5,0,1)*16*max(0.35,trade_conf)
        c_imb = clamp((imbalance-0.12)/0.55,0,1)*13*max(0.3,trade_conf)
        c_repl = 0 if ask_repl is None else clamp((0.70-ask_repl)/0.60,0,1)*13
        c_cross = clamp(confirmed_venues/max(2,coverage),0,1)*12

        score = c_ask+c_buy+c_vol+c_imb+c_repl+c_cross

        # Coverage gating: one venue can never pretend to be a market-wide signal.
        if coverage == 1:
            score = min(score, 42)
        elif coverage == 2:
            score = min(score, 72)

        # Cross-venue gating.
        if confirmed_venues == 0:
            score = min(score, 39)
        elif confirmed_venues == 1:
            score = min(score, 59)
        elif confirmed_venues == 2:
            score = min(score, 82)

        # Tiny total tape gets capped.
        if trade_conf < 0.20:
            score = min(score, 32)
        elif trade_conf < 0.40:
            score = min(score, 48)

        # Pre-move only.
        if change5 > 3:
            score *= clamp(1-(change5-3)/12,0.2,1)

        warmed = sum(1 for r in rows if r["warmed_up"])
        if warmed < max(1, math.ceil(coverage/2)):
            score = min(score,44)

        return {
            "ts": now,
            "asset": asset,
            "display_symbol": f"{asset}/USD*",
            "price": round(price, 8),
            "price_change_5m_pct": round(change5,3),
            "score": round(score,1),
            "coverage": coverage,
            "coverage_total": len(self.selected_markets.get(asset, [])),
            "confirmed_venues": confirmed_venues,
            "bid_depth_05": round(bid05,2),
            "ask_depth_05": round(ask05,2),
            "bid_depth_1": round(bid1,2),
            "ask_depth_1": round(ask1,2),
            "bid_depth_2": round(bid2,2),
            "ask_depth_2": round(ask2,2),
            "ask_depth_ratio_vs_baseline": round(ask_ratio,3),
            "buy_ratio_60s": round(buy_ratio,4),
            "trade_count_60s": trades60,
            "volume_60s": round(volume,2),
            "volume_ratio_vs_baseline": round(volume_ratio,3),
            "book_imbalance_1": round(imbalance,4),
            "ask_replenishment": None if ask_repl is None else round(ask_repl,3),
            "spread_bps": round(spread,3),
            "trade_confidence": round(trade_conf,3),
            "warmed_venues": warmed,
            "venues": sorted(venue_flags, key=lambda x: x["venue"]),
            "unavailable": self.unavailable.get(asset, []),
            "components": {
                "ask_thinning": round(c_ask,1),
                "buy_pressure": round(c_buy,1),
                "volume": round(c_vol,1),
                "book_imbalance": round(c_imb,1),
                "ask_replenishment": round(c_repl,1),
                "cross_venue": round(c_cross,1),
            }
        }

    def snapshot(self):
        per_asset = defaultdict(list)
        for (asset, venue), st in self.states.items():
            per_asset[asset].append(st.metric(self.baseline_minutes, self.warmup_minutes))

        return {
            "mode": "PER_ASSET_TOP5_VOLUME_COMPOSITE",
            "discovery_source": self.discovery.get("source"),
            "discovery_ts": self.discovery.get("ts"),
            "last_error": self.last_error or self.discoverer.last_error,
            "selected_markets": self.selected_markets,
            "skipped_markets": self.skipped_markets,
            "assets": {
                asset: self._composite(asset, per_asset.get(asset, []))
                for asset in self.assets
            }
        }
