from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Deque, Tuple, Any, Optional

import httpx
import websockets

BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def pct_change(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b / a - 1.0) * 100.0


@dataclass
class SymbolState:
    symbol: str
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    last_update_id: int = 0
    book_ready: bool = False
    last_price: float = 0.0
    last_event_ts: float = 0.0

    trades: Deque[Tuple[float, float, bool, float]] = field(default_factory=lambda: deque(maxlen=20000))
    prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=20000))
    metrics_history: Deque[Dict[str, float]] = field(default_factory=lambda: deque(maxlen=7200))

    ask_add: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=50000))
    ask_remove: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=50000))
    bid_add: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=50000))
    bid_remove: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=50000))

    def _purge(self, dq: Deque, cutoff: float) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def apply_book_side(self, side: str, updates, now: float):
        book = self.bids if side == "bid" else self.asks
        addq = self.bid_add if side == "bid" else self.ask_add
        remq = self.bid_remove if side == "bid" else self.ask_remove

        for p_raw, q_raw in updates:
            p = float(p_raw)
            q = float(q_raw)
            old = book.get(p, 0.0)
            if q > old:
                addq.append((now, (q - old) * p))
            elif q < old:
                remq.append((now, (old - q) * p))

            if q == 0.0:
                book.pop(p, None)
            else:
                book[p] = q

    def add_trade(self, event_ts: float, price: float, qty: float, buyer_is_maker: bool):
        quote = price * qty
        aggressive_buy = not buyer_is_maker
        self.trades.append((event_ts, quote, aggressive_buy, price))
        self.prices.append((event_ts, price))
        self.last_price = price
        self.last_event_ts = event_ts

    def _book_depth(self, pct: float):
        if not self.bids or not self.asks:
            return 0.0, 0.0, 0.0, 0.0
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        mid = (best_bid + best_ask) / 2.0
        bid_floor = mid * (1.0 - pct)
        ask_ceil = mid * (1.0 + pct)

        bid_notional = sum(p * q for p, q in self.bids.items() if p >= bid_floor)
        ask_notional = sum(p * q for p, q in self.asks.items() if p <= ask_ceil)
        return bid_notional, ask_notional, best_bid, best_ask

    def _trade_window(self, now: float, start_s: float, end_s: float = 0.0):
        # returns quote buy, sell, trade_count between now-start_s and now-end_s
        lo = now - start_s
        hi = now - end_s
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

    def _price_at(self, target_ts: float) -> float:
        if not self.prices:
            return self.last_price
        # newest -> oldest
        candidate = self.prices[0][1]
        for ts, price in reversed(self.prices):
            candidate = price
            if ts <= target_ts:
                return price
        return candidate

    def compute_metrics(self, baseline_minutes: int = 120) -> Dict[str, Any]:
        now = time.time()
        self._purge(self.ask_add, now - 300)
        self._purge(self.ask_remove, now - 300)
        self._purge(self.bid_add, now - 300)
        self._purge(self.bid_remove, now - 300)

        bid05, ask05, best_bid, best_ask = self._book_depth(0.005)
        bid1, ask1, _, _ = self._book_depth(0.01)
        bid2, ask2, _, _ = self._book_depth(0.02)

        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else self.last_price
        spread_bps = ((best_ask - best_bid) / mid * 10000.0) if mid and best_ask and best_bid else 0.0
        imbalance1 = (bid1 - ask1) / (bid1 + ask1) if (bid1 + ask1) else 0.0

        b60, s60, trades60 = self._trade_window(now, 60, 0)
        b_prev, s_prev, trades_prev = self._trade_window(now, 120, 60)
        vol60 = b60 + s60
        vol_prev = b_prev + s_prev
        buy_ratio = b60 / vol60 if vol60 else 0.5
        volume_accel = (vol60 / vol_prev) if vol_prev > 1 else (2.0 if vol60 > 1000 else 1.0)

        p5m = self._price_at(now - 300)
        change5m = pct_change(p5m, self.last_price) if p5m else 0.0

        def sum60(dq):
            return sum(v for ts, v in dq if ts >= now - 60)

        ask_added_60 = sum60(self.ask_add)
        ask_removed_60 = sum60(self.ask_remove)
        bid_added_60 = sum60(self.bid_add)
        bid_removed_60 = sum60(self.bid_remove)

        # Replenishment is only meaningful if enough notional was actually removed.
        min_ask_activity = max(250.0, ask1 * 0.03)
        min_bid_activity = max(250.0, bid1 * 0.03)
        ask_repl_valid = ask_removed_60 >= min_ask_activity
        bid_repl_valid = bid_removed_60 >= min_bid_activity
        ask_replenishment = (ask_added_60 / ask_removed_60) if ask_repl_valid else None
        bid_replenishment = (bid_added_60 / bid_removed_60) if bid_repl_valid else None

        baseline_cutoff = now - baseline_minutes * 60
        baseline = [m for m in self.metrics_history if m["ts"] >= baseline_cutoff]
        ask1_vals = [m["ask_depth_1"] for m in baseline if m["ask_depth_1"] > 0]
        spread_vals = [m["spread_bps"] for m in baseline if m["spread_bps"] >= 0]
        vol_vals = [m["volume_60s"] for m in baseline if m["volume_60s"] > 0]

        med_ask1 = statistics.median(ask1_vals) if len(ask1_vals) >= 30 else ask1
        med_spread = statistics.median(spread_vals) if len(spread_vals) >= 30 else spread_bps
        med_vol = statistics.median(vol_vals) if len(vol_vals) >= 30 else vol60

        ask_depth_ratio = ask1 / med_ask1 if med_ask1 > 0 else 1.0
        spread_ratio = spread_bps / med_spread if med_spread > 0 else 1.0
        volume_ratio = vol60 / med_vol if med_vol > 1 else 1.0

        # v0.3: multi-signal scoring + trade/liquidity confidence.
        # A $70 minute with two buys must not look like a strong buy-pressure event.
        warmup_seconds = 30 * 60
        oldest_ts = self.metrics_history[0]["ts"] if self.metrics_history else now
        warmup_age = now - oldest_ts
        warmed_up = len(baseline) >= 120 and warmup_age >= warmup_seconds

        # Trade confidence uses both notional and number of prints.
        # It is intentionally conservative for tiny/noisy books.
        notional_conf = clamp(vol60 / 1500.0, 0, 1)
        count_conf = clamp(trades60 / 12.0, 0, 1)
        trade_confidence = math.sqrt(notional_conf * count_conf)

        # Book confidence: tiny absolute books can be informative, but should not
        # generate a huge score by themselves.
        total_depth_1 = bid1 + ask1
        book_confidence = clamp(total_depth_1 / 25000.0, 0.20, 1.0)

        s_ask_thin = clamp((0.70 - ask_depth_ratio) / 0.45, 0, 1) * 24 * book_confidence
        s_buy = clamp((buy_ratio - 0.58) / 0.22, 0, 1) * 18 * trade_confidence
        s_vol = clamp((max(volume_accel, volume_ratio) - 1.6) / 3.4, 0, 1) * 16 * max(0.35, trade_confidence)
        s_imb = clamp((imbalance1 - 0.15) / 0.50, 0, 1) * 14 * max(0.25, trade_confidence) * book_confidence

        if ask_replenishment is None:
            s_repl = 0.0
        else:
            s_repl = clamp((0.65 - ask_replenishment) / 0.55, 0, 1) * 14 * book_confidence

        s_spread = clamp((spread_ratio - 1.35) / 1.65, 0, 1) * 8 * book_confidence

        confirms = {
            "ask_thinning": ask_depth_ratio < 0.72 and book_confidence >= 0.45,
            "buy_pressure": buy_ratio > 0.60 and trade_confidence >= 0.55,
            "volume_accel": max(volume_accel, volume_ratio) > 1.8 and trades60 >= 5,
            "book_imbalance": imbalance1 > 0.18 and trade_confidence >= 0.35 and book_confidence >= 0.45,
            "weak_ask_replenishment": (
                ask_replenishment is not None
                and ask_replenishment < 0.60
                and book_confidence >= 0.45
            ),
            "spread_widening": spread_ratio > 1.50 and book_confidence >= 0.45,
        }
        confirm_count = sum(1 for v in confirms.values() if v)

        raw_score = s_ask_thin + s_buy + s_vol + s_imb + s_repl + s_spread

        # High scores require multiple independent confirmations.
        if confirm_count <= 1:
            raw_score = min(raw_score, 39)
        elif confirm_count == 2:
            raw_score = min(raw_score, 54)
        elif confirm_count == 3:
            raw_score = min(raw_score, 69)
        elif confirm_count == 4:
            raw_score = min(raw_score, 84)

        # Low-confidence trade tape gets a hard cap.
        # This specifically prevents "100% buys on $70 volume" from becoming a high signal.
        if trade_confidence < 0.20:
            raw_score = min(raw_score, 32)
        elif trade_confidence < 0.40:
            raw_score = min(raw_score, 45)

        # We want PRE-move anomalies, not coins that have already escaped.
        move_penalty = 1.0
        if change5m > 3:
            move_penalty = clamp(1.0 - (change5m - 3.0) / 12.0, 0.20, 1.0)

        if not warmed_up:
            warmup_factor = clamp(warmup_age / warmup_seconds, 0.15, 1.0)
            raw_score = min(raw_score * warmup_factor, 44)

        score = round(raw_score * move_penalty, 1)

        row = {
            "ts": now,
            "exchange": "Binance",
            "symbol": self.symbol,
            "price": self.last_price or mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_bps": round(spread_bps, 3),
            "bid_depth_05": round(bid05, 2),
            "ask_depth_05": round(ask05, 2),
            "bid_depth_1": round(bid1, 2),
            "ask_depth_1": round(ask1, 2),
            "bid_depth_2": round(bid2, 2),
            "ask_depth_2": round(ask2, 2),
            "imbalance_1": round(imbalance1, 4),
            "buy_ratio_60s": round(buy_ratio, 4),
            "trade_count_60s": trades60,
            "trade_confidence": round(trade_confidence, 3),
            "book_confidence": round(book_confidence, 3),
            "volume_60s": round(vol60, 2),
            "volume_accel": round(volume_accel, 3),
            "price_change_5m_pct": round(change5m, 3),
            "ask_added_60s": round(ask_added_60, 2),
            "ask_removed_60s": round(ask_removed_60, 2),
            "ask_replenishment": round(ask_replenishment, 3) if ask_replenishment is not None and math.isfinite(ask_replenishment) else None,
            "bid_replenishment": round(bid_replenishment, 3) if bid_replenishment is not None and math.isfinite(bid_replenishment) else None,
            "ask_depth_ratio_vs_baseline": round(ask_depth_ratio, 3),
            "spread_ratio_vs_baseline": round(spread_ratio, 3),
            "volume_ratio_vs_baseline": round(volume_ratio, 3),
            "score": score,
            "baseline_samples": len(baseline),
            "warmed_up": warmed_up,
            "warmup_age_seconds": round(warmup_age, 1),
            "confirmation_count": confirm_count,
            "confirmations": confirms,
            "book_ready": self.book_ready,
            "components": {
                "ask_thinning": round(s_ask_thin, 1),
                "buy_pressure": round(s_buy, 1),
                "volume": round(s_vol, 1),
                "book_imbalance": round(s_imb, 1),
                "ask_replenishment": round(s_repl, 1),
                "spread": round(s_spread, 1),
            }
        }
        self.metrics_history.append(row)
        return row


class BinanceScanner:
    def __init__(self, symbols, baseline_minutes=30):
        self.symbols = [s.upper() for s in symbols]
        self.baseline_minutes = baseline_minutes
        self.states = {s: SymbolState(s) for s in self.symbols}
        self._stop = asyncio.Event()
        self._client: Optional[httpx.AsyncClient] = None
        self.last_error = ""

    async def close(self):
        self._stop.set()
        if self._client:
            await self._client.aclose()

    async def _snapshot(self, symbol: str):
        if not self._client:
            self._client = httpx.AsyncClient(timeout=10.0)
        r = await self._client.get(f"{BINANCE_REST}/api/v3/depth", params={"symbol": symbol, "limit": 1000})
        r.raise_for_status()
        data = r.json()
        st = self.states[symbol]
        st.bids = {float(p): float(q) for p, q in data["bids"]}
        st.asks = {float(p): float(q) for p, q in data["asks"]}
        st.last_update_id = int(data["lastUpdateId"])
        st.book_ready = True

    async def prepare(self):
        await asyncio.gather(*(self._snapshot(s) for s in self.symbols))

    def _streams(self):
        streams = []
        for s in self.symbols:
            sl = s.lower()
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade"]
        return "/".join(streams)

    async def run(self):
        while not self._stop.is_set():
            try:
                await self.prepare()
                url = BINANCE_WS + self._streams()
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=5000) as ws:
                    self.last_error = ""
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        event = data.get("e")
                        symbol = data.get("s", "").upper()
                        if symbol not in self.states:
                            continue
                        st = self.states[symbol]
                        now = time.time()

                        if event == "depthUpdate":
                            # If the stream and snapshot are out of sync, rebuild.
                            U = int(data["U"])
                            u = int(data["u"])
                            if u <= st.last_update_id:
                                continue
                            if U > st.last_update_id + 1:
                                st.book_ready = False
                                await self._snapshot(symbol)
                                continue
                            st.apply_book_side("bid", data.get("b", []), now)
                            st.apply_book_side("ask", data.get("a", []), now)
                            st.last_update_id = u
                            st.book_ready = True

                        elif event == "aggTrade":
                            ts = float(data.get("T", data.get("E", int(now * 1000)))) / 1000.0
                            st.add_trade(ts, float(data["p"]), float(data["q"]), bool(data["m"]))

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                await asyncio.sleep(2)

    def snapshot(self):
        return {
            "exchange": "Binance Spot",
            "last_error": self.last_error,
            "symbols": {
                s: st.compute_metrics(self.baseline_minutes)
                for s, st in self.states.items()
            }
        }
