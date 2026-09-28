"""Per-asset state: returns, volatility, persistence, onsets and status.

Two-speed signal (v0.7 amendment 5)
-----------------------------------
* fast  = median of the instantaneous (fully gated) score over the last 30 s.
  Drives EMERGING — an early warning within ~30–60 s, robust to a single
  spike because a median needs the anomaly in more than half the samples.
* slow  = median over the last 150 s, plus a hold requirement (>= 70 % of the
  last 120 s above the CONFIRMED threshold). Drives CONFIRMED / STRONG.

The ranked Pre-Move score is max(slow, 0.85 × fast), then the *current* caps
(and, while price is moving, the current instantaneous value) are re-applied,
so persistence can only hold or lower a score.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..util import clamp, median, rnd
from .reasons import build_reasons
from .rings import PriceRing
from .scoring import (FAMILIES, aggregate, compression_ratio, late_assessment, run_pipeline,
                      sigma_estimates, subscores, venue_flags)

STATUS_ORDER = ["NO DATA", "STALE", "WARMING", "LOW CONFIDENCE", "NORMAL", "WATCH", "EMERGING",
                "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE", "MOVE IN PROGRESS", "LATE"]
PRE_MOVE_STATUSES = ("WATCH", "EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE")


def _rank(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else 0


class OnsetTracker:
    """Hysteresis on/off tracking of each (venue, family) signal."""

    def __init__(self, on: float, off: float, hold_s: float):
        self.on, self.off, self.hold = on, off, hold_s
        self.state: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def update(self, now: float, venue: str, fam: str, s: float) -> Optional[Dict[str, Any]]:
        k = (venue, fam)
        st = self.state.setdefault(k, {"active": False, "above_since": None, "below_since": None,
                                       "onset": None, "peak": 0.0})
        if not st["active"]:
            if s >= self.on:
                st["above_since"] = st["above_since"] or now
                if now - st["above_since"] >= self.hold:
                    st.update(active=True, onset=st["above_since"], below_since=None, peak=s)
                    return {"kind": "onset", "venue": venue, "family": fam, "ts": st["onset"], "strength": s}
            else:
                st["above_since"] = None
        else:
            st["peak"] = max(st["peak"], s)
            if s < self.off:
                st["below_since"] = st["below_since"] or now
                if now - st["below_since"] >= 60.0:
                    st.update(active=False, above_since=None)
                    return {"kind": "offset", "venue": venue, "family": fam, "ts": now, "strength": s}
            else:
                st["below_since"] = None
        return None

    def active(self, fam: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for (v, f), st in self.state.items():
            if st["active"] and (fam is None or f == fam):
                out.append({"venue": v, "family": f, "onset": st["onset"], "peak": st["peak"]})
        return sorted(out, key=lambda x: x["onset"])

    def forget_venue(self, venue: str) -> None:
        for k in [k for k in self.state if k[0] == venue]:
            del self.state[k]


class AssetState:
    def __init__(self, asset: str, cfg: Dict[str, Any], stagger: int = 0):
        self.asset = asset
        self.cfg = cfg
        self.scfg = cfg["scoring"]
        self.price_ring = PriceRing(3720)
        self.closes: Deque[Tuple[int, float]] = deque(maxlen=1500)
        self.hist: Deque[Tuple[float, float]] = deque(maxlen=600)
        self.onsets = OnsetTracker(float(self.scfg["onset_on"]), float(self.scfg["onset_off"]),
                                   float(self.scfg["onset_hold_seconds"]))
        self.status = "NO DATA"
        self.status_since = 0.0
        self.venue_states: Dict[str, str] = {}
        self.stagger = stagger % 60
        self._sig_cache: Tuple[float, Dict[int, Optional[float]], bool] = (0.0, {}, False)
        self._comp_cache: Tuple[float, Optional[float]] = (0.0, None)
        self._cur_min: Optional[int] = None
        self._macc: Dict[str, Any] = {}
        self.pending_minutes: List[Dict[str, Any]] = []
        self.last: Dict[str, Any] = {}
        self.ever_live = False
        self.first_warm_ts: Optional[float] = None

    # ------------------------------------------------------------ restore
    def rehydrate_closes(self, rows: List[Tuple[int, float]]) -> None:
        for ts, px in rows:
            if px and px > 0:
                self.closes.append((int(ts), float(px)))

    # ------------------------------------------------------------ helpers
    def _returns(self, feats, agg, sec) -> Dict[int, Optional[float]]:
        out: Dict[int, Optional[float]] = {}
        w = agg["weights"]
        for h in (15, 30, 60):
            pairs = [(feats[i].get(f"r{h}m"), w[i]) for i in agg["live_idx"]]
            pairs = [(r, wt) for r, wt in pairs if r is not None]
            if pairs:
                out[h] = sum(r * wt for r, wt in pairs) / sum(wt for _, wt in pairs)
            else:
                past = self._close_at(sec - h * 60)
                cur = self.price_ring.latest()
                out[h] = ((cur / past - 1) * 100.0) if (cur and past) else None
        return out

    def _close_at(self, ts: int) -> Optional[float]:
        for t, px in reversed(self.closes):
            if t <= ts:
                return px if ts - t <= 180 else None
        return None

    def _sigmas(self, now: float):
        ts, sig, ok = self._sig_cache
        if now - ts >= 300 or not sig:
            closes = [px for _, px in self.closes]
            sig = sigma_estimates(closes)
            ok = len(closes) >= int(self.scfg["late"]["min_vol_minutes"]) and all(sig.get(h) for h in (15, 30, 60))
            self._sig_cache = (now, sig, ok)
        return sig, ok

    def _compression(self, now: float, sec: int) -> Optional[float]:
        ts, val = self._comp_cache
        if now - ts >= 30:
            rng = self.price_ring.range_pct(sec, 45 * 60)
            span = self.price_ring.span_seconds(sec)
            if span < 40 * 60:
                rng = None
            val = compression_ratio(rng, [px for _, px in self.closes])
            self._comp_cache = (now, val)
        return val

    # ------------------------------------------------------------ update
    def update(self, now: float, feats: List[Dict[str, Any]], selected_total: int,
               intel: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sec = int(now)
        scfg = self.scfg
        transitions: List[Dict[str, Any]] = []

        for f in feats:
            prev = self.venue_states.get(f["exchange"])
            if prev != f["state"]:
                if prev is not None:
                    transitions.append({"kind": "venue_state", "venue": f["exchange"], "from": prev,
                                        "to": f["state"], "reason": f.get("state_reason", ""), "ts": now})
                self.venue_states[f["exchange"]] = f["state"]

        flags = [venue_flags(f, scfg) for f in feats]
        agg = aggregate(feats, flags, scfg, selected_total)
        if agg["n_live"] == 0:
            status = "STALE" if self.ever_live else "NO DATA"
            return self._finish(now, status, None, feats, flags, agg, None, None, None, None, transitions, intel)
        self.ever_live = True

        # composite price (liquidity-weighted USD mid of live venues)
        w = agg["weights"]
        pairs = [(feats[i]["mid_usd"], w[i]) for i in agg["live_idx"] if feats[i].get("mid_usd")]
        price = (sum(p * wt for p, wt in pairs) / sum(wt for _, wt in pairs)) if pairs else None
        if price:
            self.price_ring.put(sec, price)

        # onsets per venue & family (for lead/lag and events)
        for f, fl in zip(feats, flags):
            for fam in FAMILIES:
                t = self.onsets.update(now, f["exchange"], fam, fl[fam])
                if t:
                    t["detail"] = _onset_detail(f, fam)
                    transitions.append(t)
        prop = self._propagation(now)
        agg["propagation"] = prop["score"]

        returns = self._returns(feats, agg, sec)
        sig, sig_ok = self._sigmas(now)
        late = late_assessment(returns, sig, sig_ok, scfg["late"])
        comp = self._compression(now, sec)
        subs = subscores(agg, price_flat=late["state"] == "FLAT")
        pipe = run_pipeline(agg, subs, late, comp, intel, scfg)

        # 6) persistence
        s4 = pipe["instant"]
        self.hist.append((now, s4))
        fast_w = float(scfg["fast_window_seconds"])
        slow_w = float(scfg["slow_window_seconds"])
        fast = median([v for t, v in self.hist if now - t <= fast_w]) or 0.0
        slow = median([v for t, v in self.hist if now - t <= slow_w]) or 0.0
        premove = max(slow, float(scfg["emerging_weight"]) * fast)
        premove = min(premove, pipe["current_cap"])
        if late["state"] != "FLAT":
            premove = min(premove, s4)
        pipe.update({"fast": fast, "slow": slow, "premove": premove})

        n_fam = pipe["n_families"]
        if agg["n_warmed"] == 0:
            status = "WARMING"
        elif late["state"] == "LATE":
            status = "LATE"
        elif late["state"] == "IN_PROGRESS":
            status = "MOVE IN PROGRESS"
        elif agg["confidence"] < float(scfg["status"]["low_confidence"]):
            status = "LOW CONFIDENCE"
        else:
            base = self._pre_status(now, 0.0, slow, fast, premove, n_fam, agg)
            status = base
            # Hysteresis: an established pre-move status is kept while its
            # condition still holds with a small margin (no flicker at 72.0).
            if self.status in PRE_MOVE_STATUSES and _rank(self.status) > _rank(base):
                keep = self._pre_status(now, float(scfg["status"].get("hysteresis", 5.0)),
                                        slow, fast, premove, n_fam, agg)
                status = max(base, min(self.status, keep, key=_rank), key=_rank)
        if agg["n_warmed"] and self.first_warm_ts is None:
            self.first_warm_ts = now
            transitions.append({"kind": "warm", "ts": now})
        return self._finish(now, status, price, feats, flags, agg, subs, pipe, late, returns, transitions,
                            intel, comp=comp, prop=prop, sig=sig, sig_ok=sig_ok)

    def _pre_status(self, now: float, margin: float, slow: float, fast: float, premove: float,
                    n_fam: int, agg: Dict[str, Any]) -> str:
        stc = self.scfg["status"]
        hold_w = float(self.scfg["confirm_hold_seconds"])
        window = [v for t, v in self.hist if now - t <= hold_w]
        span = (now - self.hist[0][0]) if self.hist else 0.0
        thr_c = float(stc["confirmed"]) - margin
        hold_frac = (sum(1 for v in window if v >= thr_c) / len(window)) if window else 0.0
        held = span >= hold_w * 0.95 and hold_frac >= 0.7
        if (slow >= float(stc["strong"]) - margin and held and n_fam >= int(stc["strong_min_families"])
                and agg["n_confirmed"] >= 2
                and agg["confirm_share"] >= float(stc["strong_min_liquidity_share"])):
            return "STRONG PRE-MOVE"
        if (slow >= thr_c and held and n_fam >= int(stc["confirmed_min_families"])
                and agg["n_confirmed"] >= 2):
            return "CONFIRMED PRE-MOVE"
        if (fast >= float(stc["emerging"]) - margin and n_fam >= int(stc["emerging_min_families"])
                and (agg["n_confirmed"] >= 2 or agg["confirm_share"] >= 0.5)):
            return "EMERGING"
        if premove >= float(stc["watch"]) - margin:
            return "WATCH"
        return "NORMAL"

    def _propagation(self, now: float) -> Dict[str, Any]:
        """Leader → follower propagation of structural signals across venues."""
        win = float(self.scfg["propagation_window_minutes"]) * 60.0
        best = {"score": 0.0, "leader": None, "followers": [], "family": None}
        for fam in ("thinning", "no_replenish", "buy_flow"):
            act = self.onsets.active(fam)
            if len(act) < 2:
                continue
            lead = act[0]
            fol = [a for a in act[1:] if 0 <= a["onset"] - lead["onset"] <= win]
            if fol and len(fol) + 1 > len(best["followers"]) + (1 if best["leader"] else 0):
                best = {"score": 1.0, "leader": lead["venue"], "family": fam,
                        "lead_onset": lead["onset"],
                        "followers": [{"venue": a["venue"], "after_s": round(a["onset"] - lead["onset"])} for a in fol]}
        return best

    def _finish(self, now, status, price, feats, flags, agg, subs, pipe, late, returns, transitions,
                intel, comp=None, prop=None, sig=None, sig_ok=False) -> Dict[str, Any]:
        if status != self.status:
            transitions.append({"kind": "status", "from": self.status, "to": status, "ts": now})
            self.status = status
            self.status_since = now
        intel = intel or {}
        subs_out = None
        if subs is not None:
            subs_out = {k: rnd(v, 1) for k, v in subs.items() if k != "absorbing"}
        for k in ("mm", "whale", "cex_flow", "scarcity"):
            v = intel.get(k)
            if subs_out is not None:
                subs_out[k] = rnd(v, 1) if v is not None else None  # None = N/A, never 0

        venues = []
        for f, fl in zip(feats, flags):
            v = {k: (rnd(val, 6) if isinstance(val, float) else val) for k, val in f.items()}
            v["flags"] = {k: rnd(fl[k], 3) for k in FAMILIES}
            v["active_families"] = fl["active"]
            v["confirmed"] = fl["confirmed"]
            venues.append(v)

        res: Dict[str, Any] = {
            "asset": self.asset, "ts": now, "status": status, "status_since": self.status_since,
            "price": rnd(price, 10) if price else None,
            "coverage": agg["n_live"], "coverage_total": agg["n_selected"], "warmed": agg["n_warmed"],
            "confirmed": agg["n_confirmed"], "confirm_share": rnd(agg["confirm_share"], 3),
            "confirmed_venues": agg["confirmed_venues"],
            "live_share": rnd(agg["live_share"], 3),
            "confidence": rnd(agg["confidence"], 3), "activity_conf": rnd(agg["activity_conf"], 3),
            "book_conf": rnd(agg["book_conf"], 3),
            "subscores": subs_out,
            "venues": venues,
            "transitions": transitions,
            "intel": intel or None,
        }
        if pipe is None:
            res.update({"premove": None, "fast": None, "slow": None, "instant": None, "families": [],
                        "n_families": 0, "reasons": [], "reason": "no live market data",
                        "pipeline": None, "late": None, "returns": {}})
        else:
            agg_out = {k: (rnd(v, 6) if isinstance(v, float) else v) for k, v in agg.items()
                       if k not in ("weights", "live_idx", "score_idx", "warmed_idx")}
            reasons = build_reasons(self.asset, agg, pipe, late, returns, comp, prop, intel, status)
            res.update({
                "premove": rnd(pipe["premove"], 1), "fast": rnd(pipe["fast"], 1), "slow": rnd(pipe["slow"], 1),
                "instant": rnd(pipe["instant"], 1), "families": pipe["families"], "n_families": pipe["n_families"],
                "pipeline": {k: (rnd(v, 2) if isinstance(v, float) else v) for k, v in pipe.items()},
                "late": {"L": rnd(late["L"], 3), "L_hard": rnd(late["L_hard"], 3), "L_vol": rnd(late["L_vol"], 3),
                         "multiplier": rnd(late["multiplier"], 3), "state": late["state"],
                         "trigger": late["trigger"], "z": {h: rnd(z, 2) for h, z in late["z"].items()},
                         "sigma_reliable": late["sigma_reliable"],
                         "sigmas": {h: rnd(s, 3) for h, s in (sig or {}).items()}},
                "returns": {h: rnd(r, 3) for h, r in returns.items()},
                "compression": rnd(comp, 3), "propagation": prop,
                "agg": agg_out, "reasons": reasons["list"], "reason": reasons["text"],
                "cap_reason": reasons["cap"],
            })
        self._minute(now, res)
        self.last = res
        return res

    def _minute(self, now: float, r: Dict[str, Any]) -> None:
        minute = int(now) // 60 * 60
        if self._cur_min is None:
            self._cur_min = minute
        if minute > self._cur_min:
            m = self._macc
            if m.get("n"):
                n = m["n"]
                rec = {
                    "asset": self.asset, "ts": self._cur_min,
                    "price_open": m.get("open"), "price_high": m.get("high"), "price_low": m.get("low"),
                    "price_close": m.get("close"),
                    "premove_avg": m["premove"] / n, "premove_max": m["premove_max"], "fast_max": m["fast_max"],
                    "status_last": m["status"], "confirmed_max": m["confirmed_max"], "coverage_min": m["coverage_min"],
                    "confidence_avg": m["conf"] / n,
                }
                for k in ("orderbook", "liquidity", "buy_pressure", "cross_venue", "mm", "whale", "cex_flow", "scarcity"):
                    vals = m["subs"].get(k)
                    rec[f"{k}_avg"] = (sum(vals) / len(vals)) if vals else None
                for k in ("ask_ratio", "buy_share", "vol_ratio", "spread_bps", "slippage_ratio"):
                    vals = m["agg"].get(k)
                    rec[f"{k}_avg"] = (sum(vals) / len(vals)) if vals else None
                rec["volume_sum"] = m.get("vol_sum", 0.0)
                self.pending_minutes.append(rec)
                if rec["price_close"]:
                    self.closes.append((self._cur_min, rec["price_close"]))
            self._cur_min = minute
            self._macc = {}
        m = self._macc
        p = r.get("price")
        if p:
            m.setdefault("open", p)
            m["high"] = max(m.get("high", p), p)
            m["low"] = min(m.get("low", p), p)
            m["close"] = p
        if r.get("premove") is None:
            return
        m["n"] = m.get("n", 0) + 1
        m["premove"] = m.get("premove", 0.0) + r["premove"]
        m["premove_max"] = max(m.get("premove_max", 0.0), r["premove"])
        m["fast_max"] = max(m.get("fast_max", 0.0), r["fast"] or 0.0)
        m["status"] = r["status"]
        m["confirmed_max"] = max(m.get("confirmed_max", 0), r["confirmed"])
        m["coverage_min"] = min(m.get("coverage_min", 99), r["coverage"])
        m["conf"] = m.get("conf", 0.0) + (r["confidence"] or 0.0)
        subs = m.setdefault("subs", {})
        for k, v in (r.get("subscores") or {}).items():
            if v is not None:
                subs.setdefault(k, []).append(v)
        agg = m.setdefault("agg", {})
        for k in ("ask_ratio", "buy_share", "vol_ratio", "spread_bps", "slippage_ratio"):
            v = (r.get("agg") or {}).get(k)
            if v is not None:
                agg.setdefault(k, []).append(v)
        # The rolling 60 s volume at the minute's last tick ≈ that minute's volume.
        m["vol_sum"] = (r.get("agg") or {}).get("vol_60") or 0.0

    def drain_minutes(self) -> List[Dict[str, Any]]:
        out, self.pending_minutes = self.pending_minutes, []
        return out


def _onset_detail(f: Dict[str, Any], fam: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {"exchange": f["exchange"], "symbol": f["symbol"]}
    if fam == "thinning":
        d["ask1_ratio"] = f.get("ask1_ratio")
        d["slippage_ratio"] = f.get("slippage_ratio")
    elif fam == "no_replenish":
        d["ask_refill"] = f.get("ask_refill")
        d["ask_net_pct"] = f.get("ask_net_pct")
        d["cancel_proxy_conf"] = f.get("cancel_proxy_conf")
    elif fam == "buy_flow":
        d["buy_share"] = f.get("buy_share_60")
        d["buy_share_base"] = f.get("buy_share_base")
    elif fam == "volume":
        d["vol_ratio"] = f.get("vol_ratio")
    elif fam == "bid_support":
        d["imbalance_delta"] = f.get("imbalance_delta")
    return d
