"""Row builders (engine → tables) and history / timeline / rehydration queries.

History is bucketed *in SQL* and keeps both MAX and AVG of the score, so a
3-minute spike cannot disappear from the 7-day chart (v0.6 used stride
sampling, which could skip it entirely).
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..engine.asset_state import STATUS_ORDER
from ..engine.baselines import MINUTE_RAW
from .schema import (ALERT_COLS, ALERT_JSON_COLS, ASSET_1M_COLS, ASSET_5S_COLS, EVENT_COLS, MARKET_10S_COLS,
                     MARKET_1M_COLS, TOKEN_CONTRACT_COLS)


# ---------------------------------------------------------------- row builders

def _r(x, nd=6):
    if x is None:
        return None
    try:
        if isinstance(x, float) and not math.isfinite(x):
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def asset_row_5s(res: Dict[str, Any]) -> Tuple:
    subs = res.get("subscores") or {}
    agg = res.get("agg") or {}
    rets = res.get("returns") or {}
    late = res.get("late") or {}
    st = res.get("status", "")
    d = {
        "asset": res["asset"], "ts": int(res["ts"]), "price": res.get("price"),
        "premove": res.get("premove"), "fast": res.get("fast"), "slow": res.get("slow"),
        "instant": res.get("instant"), "status": st,
        "status_rank": STATUS_ORDER.index(st) if st in STATUS_ORDER else 0,
        "confidence": res.get("confidence"),
        "liquidity": subs.get("liquidity"), "orderbook": subs.get("orderbook"),
        "buy_pressure": subs.get("buy_pressure"), "cross_venue": subs.get("cross_venue"),
        "mm": subs.get("mm"), "whale": subs.get("whale"), "cex_flow": subs.get("cex_flow"),
        "scarcity": subs.get("scarcity"),
        "r15": rets.get(15), "r60": rets.get(60), "late_index": late.get("L"),
        "coverage": res.get("coverage"), "coverage_total": res.get("coverage_total"),
        "confirmed": res.get("confirmed"), "families": res.get("n_families"),
        "ask_depth_1": agg.get("ask_depth_1"), "bid_depth_1": agg.get("bid_depth_1"),
        "ask_ratio": agg.get("ask_ratio"), "buy_share": agg.get("buy_share"),
        "volume_60s": agg.get("vol_60"), "vol_ratio": agg.get("vol_ratio"), "refill": agg.get("refill"),
        "spread_bps": agg.get("spread_bps"), "slippage_ratio": agg.get("slippage_ratio"),
        "cancel_proxy": agg.get("cancel_proxy_60"), "cancel_proxy_conf": agg.get("cancel_proxy_conf"),
    }
    return tuple(d[c] if isinstance(d[c], (str, int)) or d[c] is None else _r(d[c]) for c in ASSET_5S_COLS)


def market_row_10s(v: Dict[str, Any]) -> Tuple:
    d = {
        "asset": v["asset"], "exchange": v["exchange"], "ts": int(v["ts"]), "symbol": v["symbol"],
        "state": v.get("state"), "confirmed": 1 if v.get("confirmed") else 0,
        "families": len(v.get("active_families") or []), "mid": v.get("mid_usd"),
        "spread_bps": v.get("spread_bps"), "bid_depth_05": v.get("bid_depth_05"),
        "ask_depth_05": v.get("ask_depth_05"), "bid_depth_1": v.get("bid_depth_1"),
        "ask_depth_1": v.get("ask_depth_1"), "bid_depth_2": v.get("bid_depth_2"),
        "ask_depth_2": v.get("ask_depth_2"), "ask_ratio": v.get("ask1_ratio"), "bid_ratio": v.get("bid1_ratio"),
        "buy_share": v.get("buy_share_60"), "volume_60s": v.get("vol_60"), "vol_ratio": v.get("vol_ratio"),
        "trades_60s": v.get("trades_60"), "ask_repl": v.get("ask_repl_60"), "ask_refill": v.get("ask_refill"),
        "ask_net_pct": v.get("ask_net_pct"), "cancel_proxy": v.get("ask_cancel_proxy_60"),
        "cancel_proxy_conf": v.get("cancel_proxy_conf"), "slippage_bps": v.get("slippage_bps"),
        "confidence": v.get("confidence"), "book_rate_hz": v.get("book_rate_hz"),
    }
    return tuple(d[c] if isinstance(d[c], (str, int)) or d[c] is None else _r(d[c]) for c in MARKET_10S_COLS)


def asset_row_1m(rec: Dict[str, Any]) -> Tuple:
    return tuple(rec.get(c) if c in ("asset", "status_last") else (int(rec[c]) if c == "ts" else _r(rec.get(c)))
                 for c in ASSET_1M_COLS)


def market_row_1m(rec: Dict[str, Any]) -> Tuple:
    return tuple(rec.get(c) if c in ("asset", "exchange", "symbol") else (int(rec[c]) if c == "ts" else _r(rec.get(c)))
                 for c in MARKET_1M_COLS)


def event_row(ev: Dict[str, Any]) -> Tuple:
    return (ev["ts"], ev["asset"], ev.get("category"), ev["event_type"], ev.get("severity", 0), ev.get("venue"),
            _r(ev.get("level")), ev.get("message"), json.dumps(ev.get("evidence") or {}, default=str))


# ---------------------------------------------------------------- queries

def _bucket(hours: float, max_points: int, min_s: int) -> int:
    b = int(math.ceil(hours * 3600.0 / max(50, max_points)))
    b = max(min_s, b)
    return int(math.ceil(b / min_s) * min_s)


def asset_history(con: sqlite3.Connection, asset: str, hours: float, now: float,
                  max_points: int = 1500) -> Dict[str, Any]:
    since = now - hours * 3600.0
    rows: List[Dict[str, Any]] = []
    if hours <= 48:
        b = _bucket(hours, max_points, 5)
        q = f"""
          SELECT (ts / {b}) * {b} AS ts, AVG(price) AS price, MAX(premove) AS score, AVG(premove) AS score_avg,
                 MAX(fast) AS fast, AVG(orderbook) AS orderbook, AVG(liquidity) AS liquidity,
                 AVG(buy_pressure) AS buy_pressure, AVG(cross_venue) AS cross_venue, AVG(mm) AS mm,
                 AVG(whale) AS whale, AVG(cex_flow) AS cex_flow, AVG(scarcity) AS scarcity,
                 AVG(confidence) AS confidence, MAX(status_rank) AS status_rank, MAX(confirmed) AS confirmed_venues,
                 MIN(coverage) AS coverage, AVG(ask_ratio) AS ask_depth_ratio, AVG(buy_share) AS buy_ratio_60s,
                 AVG(vol_ratio) AS volume_ratio, AVG(volume_60s) AS volume_60s, AVG(refill) AS refill,
                 AVG(spread_bps) AS spread_bps, AVG(slippage_ratio) AS slippage_ratio,
                 AVG(ask_depth_1) AS ask_depth_1, AVG(bid_depth_1) AS bid_depth_1, AVG(r15) AS r15,
                 MAX(late_index) AS late_index, AVG(cancel_proxy) AS cancel_proxy
          FROM asset_metrics_5s WHERE asset=? AND ts>=? GROUP BY ts / {b} ORDER BY ts"""
        src = "asset_metrics_5s"
    else:
        b = _bucket(hours, max_points, 60)
        q = f"""
          SELECT (ts / {b}) * {b} AS ts, AVG(price_close) AS price, MAX(premove_max) AS score,
                 AVG(premove_avg) AS score_avg, MAX(fast_max) AS fast, AVG(orderbook_avg) AS orderbook,
                 AVG(liquidity_avg) AS liquidity, AVG(buy_pressure_avg) AS buy_pressure,
                 AVG(cross_venue_avg) AS cross_venue, AVG(mm_avg) AS mm, AVG(whale_avg) AS whale,
                 AVG(cex_flow_avg) AS cex_flow, AVG(scarcity_avg) AS scarcity, AVG(confidence_avg) AS confidence,
                 MAX(confirmed_max) AS confirmed_venues, MIN(coverage_min) AS coverage,
                 AVG(ask_ratio_avg) AS ask_depth_ratio, AVG(buy_share_avg) AS buy_ratio_60s,
                 AVG(vol_ratio_avg) AS volume_ratio, AVG(volume_sum) AS volume_60s,
                 AVG(spread_bps_avg) AS spread_bps, AVG(slippage_ratio_avg) AS slippage_ratio
          FROM asset_metrics_1m WHERE asset=? AND ts>=? GROUP BY ts / {b} ORDER BY ts"""
        src = "asset_metrics_1m"
    for r in con.execute(q, (asset, int(since))):
        d = dict(r)
        rk = d.pop("status_rank", None)
        d["status"] = STATUS_ORDER[int(rk)] if rk is not None and 0 <= int(rk) < len(STATUS_ORDER) else None
        d["src"] = "v07"
        rows.append(d)

    # v0.6 legacy history (imported read-only) for the part of the range before v0.7 data starts
    first_v07 = rows[0]["ts"] if rows else now
    legacy: List[Dict[str, Any]] = []
    if first_v07 > since:
        lb = _bucket(hours, max_points, 5)
        lq = f"""
          SELECT CAST(ts / {lb} AS INTEGER) * {lb} AS ts, AVG(price) AS price, MAX(score) AS score,
                 AVG(score) AS score_avg, MAX(confirmed_venues) AS confirmed_venues,
                 AVG(ask_depth_ratio) AS ask_depth_ratio, AVG(buy_ratio_60s) AS buy_ratio_60s,
                 AVG(volume_ratio) AS volume_ratio, AVG(volume_60s) AS volume_60s, AVG(spread_bps) AS spread_bps
          FROM composite_history_v2 WHERE asset=? AND ts>=? AND ts<? GROUP BY CAST(ts / {lb} AS INTEGER) ORDER BY ts"""
        for r in con.execute(lq, (asset, since, first_v07)):
            d = dict(r)
            d["src"] = "v06"
            legacy.append(d)
    return {"asset": asset, "hours": hours, "bucket_seconds": b, "source": src,
            "composite": rows, "legacy": legacy}


def venue_history(con: sqlite3.Connection, asset: str, hours: float, now: float,
                  max_points: int = 900) -> Dict[str, List[Dict[str, Any]]]:
    since = now - hours * 3600.0
    out: Dict[str, List[Dict[str, Any]]] = {}
    if hours <= 24:
        b = _bucket(hours, max_points, 10)
        q = f"""SELECT exchange, (ts / {b}) * {b} AS ts, AVG(ask_ratio) AS ask_depth_ratio, AVG(bid_ratio) AS bid_depth_ratio,
                  AVG(ask_depth_1) AS ask_depth_1, AVG(bid_depth_1) AS bid_depth_1, AVG(spread_bps) AS spread_bps,
                  AVG(buy_share) AS buy_ratio_60s, AVG(volume_60s) AS volume_60s, AVG(ask_refill) AS ask_refill,
                  AVG(ask_repl) AS ask_replenishment, AVG(cancel_proxy) AS cancel_proxy,
                  AVG(cancel_proxy_conf) AS cancel_proxy_conf, AVG(slippage_bps) AS slippage_bps,
                  MAX(confirmed) AS confirmed, AVG(mid) AS mid
                FROM market_metrics_10s WHERE asset=? AND ts>=? GROUP BY exchange, ts / {b} ORDER BY ts"""
    else:
        b = _bucket(hours, max_points, 60)
        q = f"""SELECT exchange, (ts / {b}) * {b} AS ts, AVG(ask_ratio) AS ask_depth_ratio, AVG(bid_ratio) AS bid_depth_ratio,
                  AVG(ask_depth_1) AS ask_depth_1, AVG(bid_depth_1) AS bid_depth_1, AVG(spread_bps) AS spread_bps,
                  AVG(buy_usd / NULLIF(buy_usd + sell_usd, 0)) AS buy_ratio_60s, AVG(buy_usd + sell_usd) AS volume_60s,
                  AVG(ask_added / NULLIF(ask_removed, 0)) AS ask_replenishment, AVG(ask_cancel_proxy) AS cancel_proxy,
                  AVG(slippage_bps) AS slippage_bps, AVG(mid_close) AS mid
                FROM market_metrics_1m WHERE asset=? AND ts>=? GROUP BY exchange, ts / {b} ORDER BY ts"""
    for r in con.execute(q, (asset, int(since))):
        d = dict(r)
        out.setdefault(d.pop("exchange"), []).append(d)
    if not out:
        # v0.6 per-venue history (legacy import)
        lb = _bucket(hours, max_points, 5)
        lq = f"""SELECT venue, CAST(ts / {lb} AS INTEGER) * {lb} AS ts, AVG(ask_depth_ratio) AS ask_depth_ratio,
                   AVG(ask_depth_1) AS ask_depth_1, AVG(buy_ratio_60s) AS buy_ratio_60s, AVG(volume_60s) AS volume_60s,
                   AVG(spread_bps) AS spread_bps, MAX(confirmed) AS confirmed
                 FROM venue_history_v2 WHERE asset=? AND ts>=? GROUP BY venue, CAST(ts / {lb} AS INTEGER) ORDER BY ts"""
        for r in con.execute(lq, (asset, since)):
            d = dict(r)
            d["src"] = "v06"
            out.setdefault(d.pop("venue"), []).append(d)
    return out


def events_query(con: sqlite3.Connection, asset: str, since: float, limit: int = 500) -> List[Dict[str, Any]]:
    rows = []
    for r in con.execute("SELECT * FROM events WHERE asset=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                         (asset, since, limit)):
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence") or "{}")
        except ValueError:
            d["evidence"] = {}
        rows.append(d)
    for r in con.execute("SELECT ts, asset, event_type, level, message FROM scanner_events_v2 "
                         "WHERE asset=? AND ts>=? ORDER BY ts DESC LIMIT ?", (asset, since, limit)):
        d = dict(r)
        d.update({"category": "LEGACY", "severity": 0, "venue": None, "evidence": {"source": "v0.6"}})
        rows.append(d)
    rows.sort(key=lambda d: d["ts"])
    return rows


def alert_events_query(con: sqlite3.Connection, since: float, limit: int = 500) -> List[Dict[str, Any]]:
    rows = []
    for r in con.execute("SELECT * FROM events WHERE category='ALERT' AND ts>=? ORDER BY ts DESC LIMIT ?",
                         (since, limit)):
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence") or "{}")
        except ValueError:
            d["evidence"] = {}
        rows.append(d)
    rows.reverse()
    return rows


def alert_row(al: Dict[str, Any]) -> tuple:
    """One alert (fired / updated / ended) as an `alerts` table row."""
    return tuple(json.dumps(al.get(c), separators=(",", ":"), default=str) if c in ALERT_JSON_COLS else al.get(c)
                 for c in ALERT_COLS)


def _alert_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    for c in ALERT_JSON_COLS:
        try:
            d[c] = json.loads(d.get(c) or "null")
        except ValueError:
            d[c] = None
    return d


def alerts_query(con: sqlite3.Connection, since: float, asset: Optional[str] = None,
                 limit: int = 500) -> List[Dict[str, Any]]:
    """Alerts that fired, ended or were still open inside [since, now], newest first."""
    q = "SELECT * FROM alerts WHERE (fired_ts>=? OR ended_ts>=? OR ended_ts IS NULL)"
    args: List[Any] = [since, since]
    if asset:
        q += " AND asset=?"
        args.append(asset)
    q += " ORDER BY fired_ts DESC LIMIT ?"
    args.append(limit)
    return [_alert_dict(r) for r in con.execute(q, args)]


def close_open_alerts(con: sqlite3.Connection, now: float, reason: str) -> int:
    """At start-up: alerts left open by the previous run cannot be re-verified."""
    cur = con.execute("UPDATE alerts SET state='INVALIDATED', ended_ts=?, updated_ts=?, end_reason=?, "
                      "duration_s=? - started_ts WHERE ended_ts IS NULL", (now, now, reason, now))
    con.commit()
    return cur.rowcount


def load_token_contracts(con: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    return {r["asset"]: dict(r) for r in con.execute("SELECT * FROM token_contracts")}


def token_contract_row(d: Dict[str, Any]) -> tuple:
    return tuple(d.get(c) for c in TOKEN_CONTRACT_COLS)


def load_market_minutes(con: sqlite3.Connection, asset: str, exchange: str, since: float) -> List[Dict[str, Any]]:
    cols = ",".join(["ts"] + list(MINUTE_RAW))
    return [dict(r) for r in con.execute(
        f"SELECT {cols} FROM market_metrics_1m WHERE asset=? AND exchange=? AND ts>=? ORDER BY ts",
        (asset, exchange, int(since)))]


def load_asset_closes(con: sqlite3.Connection, asset: str, since: float) -> List[Tuple[int, float]]:
    return [(int(r[0]), float(r[1])) for r in con.execute(
        "SELECT ts, price_close FROM asset_metrics_1m WHERE asset=? AND ts>=? AND price_close IS NOT NULL ORDER BY ts",
        (asset, int(since)))]


def fill_outcomes(con: sqlite3.Connection, now: float) -> int:
    """Fill forward returns for logged signals once enough time has passed."""
    n = 0
    horizons = (("fwd_15m", 900), ("fwd_1h", 3600), ("fwd_4h", 14400), ("fwd_24h", 86400))
    rows = con.execute("SELECT id, ts, asset, price, fwd_15m, fwd_1h, fwd_4h, fwd_24h FROM signal_outcomes "
                       "WHERE filled=0 AND ts < ? LIMIT 500", (now - 900,)).fetchall()
    for r in rows:
        upd = {}
        for col, h in horizons:
            if r[col] is not None or r["ts"] + h > now or not r["price"]:
                continue
            px = con.execute("SELECT price_close FROM asset_metrics_1m WHERE asset=? AND ts>=? AND ts<? "
                             "ORDER BY ts LIMIT 1", (r["asset"], int(r["ts"] + h - 60), int(r["ts"] + h + 300))).fetchone()
            if px and px[0]:
                upd[col] = (px[0] / r["price"] - 1.0) * 100.0
        filled = 1 if (r["ts"] + 86400 + 300 < now) else 0
        if upd or filled:
            sets = ", ".join(f"{k}=?" for k in upd) + (", " if upd else "") + "filled=?"
            con.execute(f"UPDATE signal_outcomes SET {sets} WHERE id=?", (*upd.values(), filled, r["id"]))
            n += 1
    return n


def outcome_summary(con: sqlite3.Connection, since: float) -> List[Dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT status, COUNT(*) AS n, AVG(fwd_15m) AS avg_15m, AVG(fwd_1h) AS avg_1h, AVG(fwd_4h) AS avg_4h, "
        "AVG(fwd_24h) AS avg_24h, SUM(CASE WHEN fwd_1h > 2 THEN 1 ELSE 0 END) AS hits_1h_2pct "
        "FROM signal_outcomes WHERE ts>=? GROUP BY status ORDER BY n DESC", (since,))]
