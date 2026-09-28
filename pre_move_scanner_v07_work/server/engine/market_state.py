"""Per-market (coin × venue) state.

Ingestion (`on_book`, `on_trades`) does only O(changed levels) work. All
metrics are computed on the 1 Hz `tick()` clock, independent of UI traffic.

Cancellation note (v0.7 amendment 1)
------------------------------------
An L2 feed plus the public trade tape cannot prove that a specific removal was
a cancellation: fills against hidden/iceberg size, update aggregation by the
exchange (several add/cancel cycles netting out between two book messages),
trade/book timestamp misalignment and truncated visible depth all blur it.
v0.7 therefore reports

    ask_cancel_proxy = max(0, ask liquidity removed − aggressive buy notional)

as a *cancellation/removal proxy* with an explicit confidence (0..0.8, never
1.0) and a label ("moderate" / "low" / "very low"). It is never presented as a
confirmed cancellation count.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..util import clamp, ramp, rnd
from .baselines import MINUTE_FIELDS, MINUTE_RAW, Baselines, derive_minute
from .book import BandBook
from .rings import MinuteRing, PriceRing, SecondRing

FLOW_FIELDS = ("buy", "sell", "trades", "maxtrade", "ask_add", "ask_rem", "bid_add", "bid_rem",
               "book_msgs", "flow_invalid")
FLOW_SECONDS = 900

LIVE_STATES = ("LIVE", "WARMING", "RESYNCING")

# Trade tuple: (exchange_ts_seconds or None, price, amount, side, trade_id or None)
Trade = Tuple[Optional[float], float, float, str, Optional[str]]


def cancel_proxy_confidence(book_rate_hz: float, trades_fresh: bool, resyncing: bool,
                            removed_usd: float, fills_usd: float, coverage_pct: float,
                            band_pct: float, throttled: bool) -> float:
    """Confidence (0..0.8) that `removed − fills` reflects pulled liquidity.

    The cap of 0.8 is deliberate: L2 + tape can never *prove* cancellations.
    """
    if resyncing or removed_usd <= 0:
        return 0.0
    if not trades_fresh:
        # Without a live tape every fill looks like a cancel.
        return 0.1
    granularity = clamp(book_rate_hz / 4.0, 0.3, 1.0)
    if throttled:
        granularity *= 0.85
    fill_share = clamp(fills_usd / removed_usd) if removed_usd > 0 else 1.0
    residual_share = 1.0 - fill_share
    # When fills explain most of the removal, the residual is small and noisy.
    significance = clamp(residual_share / 0.5, 0.2, 1.0)
    coverage = 1.0 if coverage_pct >= 0.9 * min(1.0, band_pct) else 0.6
    return round(min(0.8, granularity * significance * coverage), 3)


def proxy_label(conf: float) -> str:
    if conf >= 0.55:
        return "moderate"
    if conf >= 0.3:
        return "low"
    return "very low"


class MarketState:
    def __init__(self, asset: str, exchange: str, symbol: str, quote: str,
                 fx: Callable[[str], Optional[float]], ecfg: Dict[str, Any], fcfg: Dict[str, Any],
                 discovery_volume_24h_usd: float = 0.0, now: Optional[float] = None,
                 stagger: int = 0):
        self.asset = asset
        self.exchange = exchange
        self.symbol = symbol
        self.quote = quote
        self.key = f"{exchange}:{symbol}"
        self._fx = fx
        self.ecfg = ecfg
        self.fcfg = fcfg
        self.discovery_volume_24h_usd = float(discovery_volume_24h_usd or 0.0)
        self.band_pct = float(ecfg.get("band_pct", 2.0))
        self.book = BandBook(self.band_pct)
        self.flow = SecondRing(FLOW_FIELDS, FLOW_SECONDS)
        self.mid_ring = PriceRing(3600)
        self.minutes = MinuteRing(MINUTE_FIELDS, int(ecfg.get("baseline_long_minutes", 1440)) + 30)
        self.base = Baselines(ecfg)
        self.stagger = int(stagger) % 60
        self.created_ts = now if now is not None else time.time()

        # feed / health
        self.feed_status = "SUBSCRIBING"
        self.feed_reason = ""
        self.first_book_ts = 0.0
        self.last_book_ts = 0.0          # last book message received
        self.last_book_proc_ts = 0.0     # last book message processed (throttle)
        self.last_trade_ts = 0.0
        self.book_interval_ema = 1.0
        self.resync_until = 0.0
        self.resync_count = 0
        self.min_interval = float(fcfg.get("book_min_interval_ms", 100)) / 1000.0
        self.throttled = False
        self.dropped_historical = 0
        self.dropped_duplicate = 0
        self.skew = None  # EMA of (receive - exchange) for the freshest trade per batch
        self._seen_ids: Set[str] = set()
        self._seen_order: Deque[str] = deque(maxlen=4000)
        self.hist_s = float(ecfg.get("historical_trade_seconds", 10.0))

        # minute accumulation (sampled once per tick)
        self._cur_min: Optional[int] = None
        self._acc: Dict[str, float] = {}
        self._acc_n = 0
        self._valid_ticks = 0
        self.pending_minutes: List[Dict[str, Any]] = []
        self._base_due = False
        self._long_due_min = None
        self.slip_q_usd: Optional[float] = None
        self.verified = False
        self.last_features: Dict[str, Any] = {}

    # ------------------------------------------------------------------ fx
    def fx(self) -> float:
        v = self._fx(self.quote)
        return float(v) if v else 0.0

    # ----------------------------------------------------------- ingestion
    def set_feed_status(self, status: str, reason: str = "", ts: Optional[float] = None) -> None:
        prev = self.feed_status
        self.feed_status = status
        self.feed_reason = reason
        if status in ("DISCONNECTED", "BACKOFF", "CIRCUIT_OPEN") and prev not in ("DISCONNECTED", "BACKOFF", "CIRCUIT_OPEN"):
            # Next book must be treated as a snapshot.
            self.book.reset()

    def on_book(self, bids: Iterable, asks: Iterable, recv_ts: float, resync: bool = False) -> None:
        sec = int(recv_ts)
        self.flow.add(sec, "book_msgs", 1.0)
        if self.last_book_ts:
            gap = recv_ts - self.last_book_ts
            if 0 < gap < 3600:
                self.book_interval_ema += 0.05 * (min(gap, 120.0) - self.book_interval_ema)
        self.last_book_ts = recv_ts
        if not self.first_book_ts:
            self.first_book_ts = recv_ts
        if resync or not self.book.ready:
            if resync and self.book.ready:
                self.resync_count += 1
            self.book.update(bids, asks, snapshot=True)
            self.resync_until = recv_ts + float(self.fcfg.get("resync_grace_seconds", 60))
            self.last_book_proc_ts = recv_ts
            if self.feed_status in ("SUBSCRIBING", "VERIFYING", "DISCONNECTED", "BACKOFF"):
                self.feed_status = "STREAMING"
            return
        if recv_ts - self.last_book_proc_ts < self.min_interval:
            self.throttled = True
            return
        self.last_book_proc_ts = recv_ts
        flows = self.book.update(bids, asks)
        if self.book.updates >= 2:
            self.verified = True
        if self.feed_status != "STREAMING":
            self.feed_status = "STREAMING"
        if flows is None:
            return
        if recv_ts < self.resync_until:
            self.flow.add(sec, "flow_invalid", 1.0)
            return
        fx = self.fx()
        if fx <= 0:
            return
        b_add, b_rem, a_add, a_rem = flows
        if b_add:
            self.flow.add(sec, "bid_add", b_add * fx)
        if b_rem:
            self.flow.add(sec, "bid_rem", b_rem * fx)
        if a_add:
            self.flow.add(sec, "ask_add", a_add * fx)
        if a_rem:
            self.flow.add(sec, "ask_rem", a_rem * fx)

    def _is_duplicate(self, t: Trade) -> bool:
        tid = t[4]
        key = str(tid) if tid is not None else f"{t[0]}|{t[1]}|{t[2]}|{t[3]}"
        if key in self._seen_ids:
            return True
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen_ids.discard(self._seen_order[0])
        self._seen_order.append(key)
        self._seen_ids.add(key)
        return False

    def on_trades(self, trades: Iterable[Trade], recv_ts: float) -> int:
        """Ingest a batch of trades; returns how many were accepted.

        Windows use *receipt* time (local clock) so exchange clock skew cannot
        distort them. Trades that are much older than the freshest trade of the
        batch (after skew correction) are treated as historical replays and
        dropped, so a reconnect cannot dump old prints into the current second.
        """
        batch = list(trades)
        if not batch:
            return 0
        lags = [recv_ts - t[0] for t in batch if t[0]]
        if lags:
            freshest = min(lags)
            if self.skew is None:
                self.skew = freshest
            else:
                self.skew += 0.1 * (clamp(freshest, self.skew - 30.0, self.skew + 30.0) - self.skew)
        fx = self.fx()
        if fx <= 0:
            return 0
        sec = int(recv_ts)
        accepted = 0
        for t in batch:
            ts, price, amount, side = t[0], t[1], t[2], t[3]
            if not price or not amount or price <= 0 or amount <= 0:
                continue
            if self._is_duplicate(t):
                self.dropped_duplicate += 1
                continue
            if ts and self.skew is not None and (recv_ts - ts) - self.skew > self.hist_s:
                self.dropped_historical += 1
                continue
            notional = float(price) * float(amount) * fx
            if str(side).lower() == "buy":
                self.flow.add(sec, "buy", notional)
            else:
                self.flow.add(sec, "sell", notional)
            self.flow.add(sec, "trades", 1.0)
            self.flow.put_max(sec, "maxtrade", notional)
            accepted += 1
        if accepted:
            self.last_trade_ts = recv_ts
        return accepted

    # ------------------------------------------------------------- health
    def stale_threshold(self) -> float:
        return max(float(self.fcfg.get("stale_book_seconds", 20)),
                   float(self.fcfg.get("stale_multiplier", 5.0)) * self.book_interval_ema)

    def state(self, now: float) -> Tuple[str, str]:
        fs = self.feed_status
        if fs == "UNAVAILABLE":
            return "UNAVAILABLE", self.feed_reason or "market unavailable"
        if fs in ("DISCONNECTED", "BACKOFF", "CIRCUIT_OPEN"):
            return "DISCONNECTED", self.feed_reason or fs.lower()
        if not self.book.ready:
            return "VERIFYING", "waiting for first order book"
        if now - self.last_book_ts > self.stale_threshold():
            return "STALE", f"no book update for {now - self.last_book_ts:.0f}s"
        if now < self.resync_until:
            return "RESYNCING", "book re-synchronised; flow metrics paused"
        if not self.base.warm:
            return "WARMING", f"baseline {self.base.valid_minutes}/{self.base.min_minutes} min"
        return "LIVE", ""

    # ---------------------------------------------------------- rehydrate
    def rehydrate(self, records: Iterable[Dict[str, Any]], now: float) -> int:
        """Load persisted 1-minute records (oldest first) to restore baselines."""
        n = 0
        for rec in records:
            r = {k: rec.get(k) for k in MINUTE_RAW}
            derive_minute(r)
            self.minutes.append(int(rec["ts"]), r)
            n += 1
        if n:
            now_min = int(now) // 60 * 60
            self.base.recompute(self.minutes, now_min, include_long=True)
            self._update_slip_q()
        return n

    # ---------------------------------------------------------------- tick
    def _update_slip_q(self) -> None:
        e = self.ecfg
        ref = self.base.med("ask_depth_1", "long") or self.base.med("ask_depth_1", "main")
        if ref:
            self.slip_q_usd = clamp(float(e.get("slippage_fraction_of_depth", 0.2)) * ref,
                                    float(e.get("slippage_order_min_usd", 1000.0)),
                                    float(e.get("slippage_order_max_usd", 100000.0)))

    def _finalize_minute(self, minute_ts: int) -> None:
        end_sec = minute_ts + 59
        n = max(1, self._acc_n)
        acc = self._acc
        rec: Dict[str, Any] = {
            "ts": minute_ts,
            "mid_close": acc.get("mid_close"),
            "mid_high": acc.get("mid_high"),
            "mid_low": acc.get("mid_low"),
            "spread_bps": acc.get("spread_bps", 0.0) / n if self._acc_n else None,
            "imbalance_1": acc.get("imbalance_1", 0.0) / n if self._acc_n else None,
            "slippage_bps": (acc["slip_sum"] / acc["slip_n"]) if acc.get("slip_n") else None,
            "buy_usd": self.flow.sum("buy", end_sec, 60),
            "sell_usd": self.flow.sum("sell", end_sec, 60),
            "trades": self.flow.sum("trades", end_sec, 60),
            "max_trade_usd": self.flow.max("maxtrade", end_sec, 60),
            "ask_added": self.flow.sum("ask_add", end_sec, 60),
            "ask_removed": self.flow.sum("ask_rem", end_sec, 60),
            "bid_added": self.flow.sum("bid_add", end_sec, 60),
            "bid_removed": self.flow.sum("bid_rem", end_sec, 60),
            "book_msgs": self.flow.sum("book_msgs", end_sec, 60),
            "valid_frac": round(self._valid_ticks / 60.0, 3),
        }
        for k in ("bid_depth_05", "ask_depth_05", "bid_depth_1", "ask_depth_1", "bid_depth_2", "ask_depth_2"):
            rec[k] = acc.get(k, 0.0) / n if self._acc_n else None
        rec["ask_cancel_proxy"] = max(0.0, rec["ask_removed"] - rec["buy_usd"])
        derive_minute(rec)
        self.minutes.append(minute_ts, rec)
        out = dict(rec)
        out.update({"asset": self.asset, "exchange": self.exchange, "symbol": self.symbol})
        self.pending_minutes.append(out)
        self._base_due = True

    def _accumulate(self, sample: Dict[str, float], valid: bool) -> None:
        acc = self._acc
        if valid:
            self._valid_ticks += 1
        if sample.get("mid_usd"):
            m = sample["mid_usd"]
            acc["mid_close"] = m
            acc["mid_high"] = max(acc.get("mid_high", m), m)
            acc["mid_low"] = min(acc.get("mid_low", m), m)
        if not sample.get("book_fresh"):
            return
        self._acc_n += 1
        for k in ("spread_bps", "imbalance_1", "bid_depth_05", "ask_depth_05", "bid_depth_1",
                  "ask_depth_1", "bid_depth_2", "ask_depth_2"):
            acc[k] = acc.get(k, 0.0) + (sample.get(k) or 0.0)
        if sample.get("slippage_bps") is not None:
            acc["slip_sum"] = acc.get("slip_sum", 0.0) + sample["slippage_bps"]
            acc["slip_n"] = acc.get("slip_n", 0) + 1

    def tick(self, now: float) -> Dict[str, Any]:
        sec = int(now)
        self.flow.advance(sec)
        st, reason = self.state(now)
        fx = self.fx()
        book_fresh = st in LIVE_STATES and self.book.ready
        bd05 = ad05 = bd1 = ad1 = bd2 = ad2 = 0.0
        spread = imb = None
        slip = None
        mid_usd = None
        if self.book.ready and fx > 0:
            q = self.book.depths()
            bd05, ad05, bd1, ad1, bd2, ad2 = (x * fx for x in q)
            spread = self.book.spread_bps()
            imb = (bd1 - ad1) / (bd1 + ad1) if (bd1 + ad1) else 0.0
            mid_usd = self.book.mid * fx
            if self.slip_q_usd is None and ad1 > 0:
                self.slip_q_usd = clamp(float(self.ecfg.get("slippage_fraction_of_depth", 0.2)) * ad1,
                                        float(self.ecfg.get("slippage_order_min_usd", 1000.0)),
                                        float(self.ecfg.get("slippage_order_max_usd", 100000.0)))
            if self.slip_q_usd:
                s = self.book.buy_slippage_bps(self.slip_q_usd / fx)
                # Order larger than the visible band: record the band edge as a lower bound.
                slip = s if s is not None else self.band_pct * 100.0
        if book_fresh and mid_usd:
            self.mid_ring.put(sec, mid_usd)

        # --- minute rollover
        minute = sec // 60 * 60
        if self._cur_min is None:
            self._cur_min = minute
        elif minute > self._cur_min:
            self._finalize_minute(self._cur_min)
            self._cur_min = minute
            self._acc, self._acc_n, self._valid_ticks = {}, 0, 0
        valid = st in ("LIVE", "WARMING") and self.book.ready
        self._accumulate({"mid_usd": mid_usd if book_fresh else None, "book_fresh": book_fresh,
                          "spread_bps": spread, "imbalance_1": imb, "bid_depth_05": bd05,
                          "ask_depth_05": ad05, "bid_depth_1": bd1, "ask_depth_1": ad1,
                          "bid_depth_2": bd2, "ask_depth_2": ad2, "slippage_bps": slip}, valid)

        # --- staggered baseline recompute (once per minute per market)
        if self._base_due and (sec % 60) >= self.stagger:
            include_long = self._long_due_min is None or minute - self._long_due_min >= 600
            self.base.recompute(self.minutes, minute, include_long=include_long)
            if include_long:
                self._long_due_min = minute
                self._update_slip_q()
            self._base_due = False
            st, reason = self.state(now)

        f = self._features(now, sec, st, reason, fx, mid_usd, spread, imb, slip,
                           bd05, ad05, bd1, ad1, bd2, ad2)
        self.last_features = f
        return f

    def _features(self, now, sec, st, reason, fx, mid_usd, spread, imb, slip,
                  bd05, ad05, bd1, ad1, bd2, ad2) -> Dict[str, Any]:
        e = self.ecfg
        b = self.base
        fl = self.flow
        buy = fl.sum("buy", sec, 60)
        sell = fl.sum("sell", sec, 60)
        trades = fl.sum("trades", sec, 60)
        maxtrade = fl.max("maxtrade", sec, 60)
        vol = buy + sell
        a_add = fl.sum("ask_add", sec, 60)
        a_rem = fl.sum("ask_rem", sec, 60)
        b_add = fl.sum("bid_add", sec, 60)
        b_rem = fl.sum("bid_rem", sec, 60)
        msgs = fl.sum("book_msgs", sec, 60)
        invalid = fl.sum("flow_invalid", sec, 60)
        book_rate = msgs / 60.0

        material_ask = max(250.0, 0.03 * ad1)
        material_bid = max(250.0, 0.03 * bd1)
        ask_repl = (a_add / a_rem) if a_rem >= material_ask else None
        bid_repl = (b_add / b_rem) if b_rem >= material_bid else None
        resyncing = now < self.resync_until or invalid > 0
        cancel_proxy = max(0.0, a_rem - buy)
        trades_fresh = self.last_trade_ts > 0 and (now - self.last_trade_ts) < 120
        cp_conf = cancel_proxy_confidence(book_rate, trades_fresh, resyncing, a_rem, buy,
                                          self.book.coverage_pct(), self.band_pct, self.throttled)
        cancel_intensity = (cancel_proxy / ad1) if ad1 > 0 else None
        ask_net = a_add - a_rem
        ask_net_pct = (ask_net / ad1) if ad1 > 0 else None
        # "Asks not replenishing after buyers consume liquidity":
        # (added - cancels) / fills = 1 + net / fills. Churn cancels out.
        ask_refill = (1.0 + ask_net / buy) if buy >= material_ask else None

        buy_share = (buy / vol) if vol > 0 else None
        base_buy_share = b.med("buy_share")
        base_vol = b.med("vol")
        base_vol_short = b.med("vol", "short")
        base_trades = b.med("trades")
        base_depth = b.med("depth_1") or b.med("depth_1", "long")
        base_imb = b.med("imbalance_1")

        # --- confidence components
        f_lo, f_hi = e.get("activity_floor_usd", [1000.0, 25000.0])
        t_lo, t_hi = e.get("activity_floor_trades", [5, 30])
        floor_usd = clamp(0.5 * base_vol, f_lo, f_hi) if base_vol else 2.0 * f_lo
        floor_tr = clamp(0.5 * base_trades, t_lo, t_hi) if base_trades else 2.0 * t_lo
        activity = math.sqrt(clamp(vol / floor_usd) * clamp(trades / floor_tr))
        top_share = (maxtrade / vol) if vol > 0 else 0.0
        if top_share > 0.4:
            activity *= 1.0 - 0.7 * clamp((top_share - 0.4) / 0.6)
        depth_ref = base_depth if base_depth else (bd1 + ad1)
        book_conf = clamp(depth_ref / float(e.get("book_floor_usd", 20000.0)), 0.2, 1.0)
        data_conf = {"LIVE": 1.0, "WARMING": 0.5, "RESYNCING": 0.3}.get(st, 0.0)
        confidence = data_conf * math.sqrt(book_conf * max(activity, 0.1))

        mid_1m = self.mid_ring.at_or_before(sec - 60, 30)
        mid_5m = self.mid_ring.at_or_before(sec - 300, 60)
        cur_mid = self.mid_ring.latest()
        r1m = ((cur_mid / mid_1m - 1) * 100) if (cur_mid and mid_1m) else None
        r5m = ((cur_mid / mid_5m - 1) * 100) if (cur_mid and mid_5m) else None

        depth1 = bd1 + ad1
        return {
            "key": self.key, "asset": self.asset, "exchange": self.exchange, "symbol": self.symbol,
            "quote": self.quote, "fx": fx, "ts": now,
            "state": st, "state_reason": reason, "warmed": b.warm,
            "warm_minutes": b.valid_minutes, "warm_needed": b.min_minutes,
            "resyncing": resyncing, "verified": self.verified,
            "mid_usd": mid_usd, "best_bid": self.book.best_bid, "best_ask": self.book.best_ask,
            "spread_bps": spread, "imbalance_1": imb,
            "bid_depth_05": bd05, "ask_depth_05": ad05, "bid_depth_1": bd1, "ask_depth_1": ad1,
            "bid_depth_2": bd2, "ask_depth_2": ad2, "book_coverage_pct": self.book.coverage_pct(),
            "buy_usd_60": buy, "sell_usd_60": sell, "trades_60": trades, "max_trade_60": maxtrade,
            "vol_60": vol, "buy_share_60": buy_share, "net_flow_60": buy - sell, "top_trade_share": top_share,
            "ask_added_60": a_add, "ask_removed_60": a_rem, "bid_added_60": b_add, "bid_removed_60": b_rem,
            "ask_repl_60": ask_repl, "bid_repl_60": bid_repl,
            "ask_net_60": ask_net, "ask_net_pct": ask_net_pct, "ask_refill": ask_refill,
            "ask_net_z": b.z("ask_net_pct", ask_net_pct),
            "ask_refill_base": b.med("ask_refill"),
            "ask_cancel_proxy_60": cancel_proxy, "cancel_intensity": cancel_intensity,
            "cancel_proxy_conf": cp_conf, "cancel_proxy_label": proxy_label(cp_conf),
            "slippage_bps": slip, "slippage_q_usd": self.slip_q_usd,
            "book_rate_hz": book_rate, "book_age_s": (now - self.last_book_ts) if self.last_book_ts else None,
            "trade_age_s": (now - self.last_trade_ts) if self.last_trade_ts else None,
            "r1m": r1m, "r5m": r5m,
            # relative-to-own-baseline features
            "ask1_ratio": b.ratio("ask_depth_1", ad1), "ask1_z": b.z("ask_depth_1", ad1),
            "bid1_ratio": b.ratio("bid_depth_1", bd1), "bid1_z": b.z("bid_depth_1", bd1),
            "ask05_ratio": b.ratio("ask_depth_05", ad05),
            "depth_ratio": b.ratio("depth_1", depth1),
            "spread_ratio": b.ratio("spread_bps", spread),
            "slippage_ratio": b.ratio("slippage_bps", slip),
            "vol_ratio": (vol / base_vol) if base_vol and base_vol > 0 else None,
            "vol_ratio_short": (vol / base_vol_short) if base_vol_short and base_vol_short > 0 else None,
            "buy_share_base": base_buy_share,
            "buy_share_excess": (buy_share - base_buy_share) if (buy_share is not None and base_buy_share is not None) else None,
            "imbalance_delta": (imb - base_imb) if (imb is not None and base_imb is not None) else None,
            "repl_base": b.med("ask_repl"),
            "cancel_ratio": b.ratio("cancel_intensity", cancel_intensity) if cancel_intensity is not None else None,
            "base_vol_1m": base_vol, "base_trades_1m": base_trades, "base_depth_1": base_depth,
            "activity_conf": activity, "book_conf": book_conf, "data_conf": data_conf, "confidence": confidence,
            "discovery_volume_24h_usd": self.discovery_volume_24h_usd,
            "dropped_historical": self.dropped_historical,
        }

    def drain_minutes(self) -> List[Dict[str, Any]]:
        out, self.pending_minutes = self.pending_minutes, []
        return out

    def book_summary(self, bins: int = 20) -> Dict[str, Any]:
        fx = self.fx() or 0.0
        h = self.book.histogram(bins)
        h["bid"] = [rnd(x * fx, 2) for x in h["bid"]]
        h["ask"] = [rnd(x * fx, 2) for x in h["ask"]]
        return h

    def compact(self) -> Dict[str, Any]:
        """Small JSON-safe view of the latest features for the UI/storage."""
        out = {}
        for k, v in self.last_features.items():
            out[k] = rnd(v, 6) if isinstance(v, float) else v
        return out
