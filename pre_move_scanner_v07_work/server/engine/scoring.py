"""Scoring: venue flags → cross-venue aggregation → sub-scores → pipeline.

Pipeline order (v0.7 amendment 6) — bonuses and context can never bypass a
safety gate because every gate is applied *after* them:

  1. structural score        weighted structure sub-scores
  2. compression / context   price-compression bonus, bounded on-chain context
  3. confidence              multiplier + hard low-activity caps
  4. caps                    independent-family, venue-count, confirmations,
                             liquidity-share, partial coverage, warm-up
  5. late-move               penalty multiplier and the hard LATE cap — LAST
  6. persistence / status    fast (30 s) and slow (150 s) medians; the current
                             caps are re-applied to the persisted value, so
                             persistence can only hold or lower a score, never
                             resurrect one that a gate has since cut.

Null semantics (amendment 8): wallet/MM/CEX sub-scores are None when the data
is missing or unreliable. None is displayed as N/A, contributes no context
points, and never counts as an active family. It is never treated as 0 or as
a negative signal.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..util import clamp, median, ramp, robust_stats

FAMILIES = ("thinning", "no_replenish", "buy_flow", "volume", "bid_support")
ASSET_FAMILIES = FAMILIES + ("cross_venue", "onchain")
EXTRAS = ("depth_both", "spread", "slip")

FAMILY_LABELS = {
    "thinning": "ask thinning",
    "no_replenish": "weak ask replenishment",
    "buy_flow": "aggressive buying",
    "volume": "volume acceleration",
    "bid_support": "bid support vs ask weakness",
    "cross_venue": "cross-venue confirmation",
    "onchain": "wallet / CEX context",
}


# --------------------------------------------------------------------------
# 1. Per-venue flags

def venue_flags(f: Dict[str, Any], scfg: Dict[str, Any]) -> Dict[str, Any]:
    """Family strengths (0..1) for one venue, relative to its own baseline."""
    out: Dict[str, Any] = {k: 0.0 for k in FAMILIES + EXTRAS}
    out["active"] = []
    out["confirmed"] = False
    if f.get("state") != "LIVE" or not f.get("warmed"):
        return out
    act = f.get("activity_conf") or 0.0

    r, z = f.get("ask1_ratio"), f.get("ask1_z")
    s_ask = min(ramp(r, 0.85, 0.45), ramp(-z, 1.0, 2.5)) if (r is not None and z is not None) else 0.0
    s_slip = ramp(f.get("slippage_ratio"), 1.3, 2.5)
    out["thinning"] = max(s_ask, 0.8 * s_slip)
    out["slip"] = s_slip

    refill = f.get("ask_refill")
    s_refill = ramp(refill, 0.85, 0.40) if refill is not None else 0.0
    netz, netp = f.get("ask_net_z"), f.get("ask_net_pct")
    s_net = ramp(-netz, 1.5, 4.0) if (netz is not None and netp is not None and netp < -0.03) else 0.0
    cr, ci = f.get("cancel_ratio"), f.get("cancel_intensity")
    s_pull = (ramp(cr, 1.8, 4.0) * (f.get("cancel_proxy_conf") or 0.0)
              if (cr is not None and ci is not None and ci >= 0.05) else 0.0)
    out["no_replenish"] = max(s_refill, s_net, 0.7 * s_pull)
    out["pull_proxy"] = s_pull

    bse = f.get("buy_share_excess")
    out["buy_flow"] = (ramp(bse, 0.05, 0.25) * act
                       if (bse is not None and (f.get("net_flow_60") or 0.0) > 0) else 0.0)
    s_vol = ramp(f.get("vol_ratio"), 1.5, 4.0) * act
    if bse is not None and bse < -0.05:
        s_vol *= 0.3  # sell-driven volume is not a pre-move-up signal
    out["volume"] = s_vol

    imbd, b1r = f.get("imbalance_delta"), f.get("bid1_ratio")
    out["bid_support"] = (ramp(imbd, 0.08, 0.35) * ramp(b1r, 0.85, 1.0)
                          if (imbd is not None and b1r is not None) else 0.0)

    out["depth_both"] = ramp(f.get("depth_ratio"), 0.85, 0.50)
    out["spread"] = ramp(f.get("spread_ratio"), 1.3, 2.8)

    thr = float(scfg.get("venue_family_active", 0.4))
    out["active"] = [fam for fam in FAMILIES if out[fam] >= thr]
    out["confirmed"] = (len(out["active"]) >= int(scfg.get("venue_confirm_families", 2))
                        and (f.get("confidence") or 0.0) >= float(scfg.get("venue_confirm_min_confidence", 0.35)))
    return out


# --------------------------------------------------------------------------
# 2. Cross-venue aggregation

def _liq_weight(f: Dict[str, Any]) -> float:
    w = f.get("base_depth_1")
    if not w:
        w = (f.get("bid_depth_1") or 0.0) + (f.get("ask_depth_1") or 0.0)
    if not w:
        w = (f.get("discovery_volume_24h_usd") or 0.0) / 500.0
    return max(float(w), 1.0)


def aggregate(feats: Sequence[Dict[str, Any]], flags: Sequence[Dict[str, Any]],
              scfg: Dict[str, Any], selected_total: int) -> Dict[str, Any]:
    all_w = [_liq_weight(f) for f in feats]
    total_w = sum(all_w) or 1.0
    live_idx = [i for i, f in enumerate(feats) if f.get("state") in ("LIVE", "WARMING", "RESYNCING")]
    score_idx = [i for i in live_idx if feats[i].get("state") in ("LIVE", "WARMING")]
    warmed_idx = [i for i in score_idx if feats[i].get("state") == "LIVE"]
    sw = sum(all_w[i] for i in score_idx) or 0.0

    def wavg(key_fn, idx=None) -> float:
        idx = score_idx if idx is None else idx
        den = sum(all_w[i] for i in idx)
        if not den:
            return 0.0
        return sum(all_w[i] * key_fn(i) for i in idx) / den

    fam = {k: wavg(lambda i, k=k: flags[i][k]) for k in FAMILIES + EXTRAS}

    confirmed_idx = [i for i in warmed_idx if flags[i]["confirmed"]]
    n_conf = len(confirmed_idx)
    confirm_share = (sum(all_w[i] for i in confirmed_idx) / sw) if sw else 0.0

    # Liquidity-weighted aggregate ask/bid depth vs summed own baselines (kept from v0.6).
    def agg_ratio(depth_key, ratio_key):
        cur = base = 0.0
        for i in warmed_idx:
            d, r = feats[i].get(depth_key) or 0.0, feats[i].get(ratio_key)
            if r and r > 0:
                cur += d
                base += d / r
        return (cur / base) if base > 0 else None

    buy = sum(feats[i].get("buy_usd_60") or 0.0 for i in live_idx)
    sell = sum(feats[i].get("sell_usd_60") or 0.0 for i in live_idx)
    vol = buy + sell
    trades = sum(feats[i].get("trades_60") or 0.0 for i in live_idx)
    max_trade = max([feats[i].get("max_trade_60") or 0.0 for i in live_idx] or [0.0])
    base_vol = sum(feats[i].get("base_vol_1m") or 0.0 for i in warmed_idx)
    base_trades = sum(feats[i].get("base_trades_1m") or 0.0 for i in warmed_idx)
    base_depth = sum(feats[i].get("base_depth_1") or 0.0 for i in live_idx)
    base_share_num = sum(all_w[i] * feats[i]["buy_share_base"] for i in warmed_idx
                         if feats[i].get("buy_share_base") is not None)
    base_share_den = sum(all_w[i] for i in warmed_idx if feats[i].get("buy_share_base") is not None)

    refills = [(feats[i]["ask_refill"], all_w[i]) for i in warmed_idx if feats[i].get("ask_refill") is not None]
    refill = (sum(v * w for v, w in refills) / sum(w for _, w in refills)) if refills else None

    # asset activity confidence (aggregate tape) — the $70 / 2-trade guard
    floor_usd = clamp(0.5 * base_vol, 2500.0, 50000.0) if base_vol else 5000.0
    floor_tr = clamp(0.5 * base_trades, 10.0, 60.0) if base_trades else 20.0
    activity = math.sqrt(clamp(vol / floor_usd) * clamp(trades / floor_tr))
    top_share = (max_trade / vol) if vol > 0 else 0.0
    if top_share > 0.4:
        activity *= 1.0 - 0.7 * clamp((top_share - 0.4) / 0.6)
    book_conf = clamp((base_depth or sum((feats[i].get("bid_depth_1") or 0) + (feats[i].get("ask_depth_1") or 0)
                                         for i in live_idx)) / 50000.0, 0.2, 1.0)
    live_share = sum(all_w[i] for i in live_idx) / total_w
    data_share = sum(all_w[i] for i in warmed_idx) / total_w
    confidence = (activity ** 0.5) * (book_conf ** 0.25) * (max(data_share, 0.0) ** 0.25)

    spread_num = sum(math.sqrt(all_w[i]) * (feats[i].get("spread_bps") or 0.0) for i in live_idx)
    spread_den = sum(math.sqrt(all_w[i]) for i in live_idx)

    def count_family(k):
        thr = float(scfg.get("venue_family_active", 0.4))
        return sum(1 for i in warmed_idx if flags[i][k] >= thr)

    return {
        "weights": all_w, "live_idx": live_idx, "score_idx": score_idx, "warmed_idx": warmed_idx,
        "family": fam, "family_venue_counts": {k: count_family(k) for k in FAMILIES},
        "n_live": len(live_idx), "n_scoring": len(score_idx), "n_warmed": len(warmed_idx),
        "n_selected": max(selected_total, len(feats)),
        "n_confirmed": n_conf, "confirm_share": confirm_share,
        "confirmed_venues": [feats[i]["exchange"] for i in confirmed_idx],
        "live_share": live_share, "data_share": data_share,
        "ask_depth_1": sum(feats[i].get("ask_depth_1") or 0.0 for i in live_idx),
        "bid_depth_1": sum(feats[i].get("bid_depth_1") or 0.0 for i in live_idx),
        "ask_depth_05": sum(feats[i].get("ask_depth_05") or 0.0 for i in live_idx),
        "bid_depth_05": sum(feats[i].get("bid_depth_05") or 0.0 for i in live_idx),
        "ask_depth_2": sum(feats[i].get("ask_depth_2") or 0.0 for i in live_idx),
        "bid_depth_2": sum(feats[i].get("bid_depth_2") or 0.0 for i in live_idx),
        "ask_ratio": agg_ratio("ask_depth_1", "ask1_ratio"),
        "bid_ratio": agg_ratio("bid_depth_1", "bid1_ratio"),
        "buy_usd_60": buy, "sell_usd_60": sell, "vol_60": vol, "trades_60": trades,
        "buy_share": (buy / vol) if vol > 0 else None,
        "buy_share_base": (base_share_num / base_share_den) if base_share_den else None,
        "vol_ratio": (vol / base_vol) if base_vol > 0 else None,
        "refill": refill,
        "ask_net_60": sum(feats[i].get("ask_net_60") or 0.0 for i in warmed_idx),
        "cancel_proxy_60": sum(feats[i].get("ask_cancel_proxy_60") or 0.0 for i in warmed_idx),
        "cancel_proxy_conf": min([feats[i].get("cancel_proxy_conf") or 0.0 for i in warmed_idx] or [0.0]),
        "spread_bps": (spread_num / spread_den) if spread_den else None,
        "slippage_ratio": _wmean([(feats[i].get("slippage_ratio"), all_w[i]) for i in warmed_idx]),
        "activity_conf": activity, "book_conf": book_conf, "confidence": confidence,
        "top_trade_share": top_share,
    }


def _wmean(pairs):
    num = den = 0.0
    for v, w in pairs:
        if v is None:
            continue
        num += v * w
        den += w
    return (num / den) if den else None


# --------------------------------------------------------------------------
# 3. Sub-scores

def subscores(agg: Dict[str, Any], price_flat: bool) -> Dict[str, Optional[float]]:
    fam = agg["family"]
    ob = 100.0 * clamp(0.45 * fam["thinning"] + 0.33 * fam["no_replenish"] + 0.22 * fam["bid_support"])
    liq = 100.0 * clamp(0.40 * fam["depth_both"] + 0.35 * fam["slip"] + 0.25 * fam["spread"])
    buy = 100.0 * clamp(0.70 * fam["buy_flow"] + 0.30 * fam["volume"])
    # Buying into asks that keep being refilled with a flat price is a passive
    # seller absorbing — not a pre-move-up signal. Only reward flow when the
    # asks are NOT replenishing.
    absorbing = (agg.get("refill") is not None and agg["refill"] >= 1.1 and fam["no_replenish"] < 0.2
                 and price_flat)
    if absorbing:
        buy *= 0.5
    n_conf = agg["n_confirmed"]
    cross = 0.0
    if n_conf >= 1:
        cross = 100.0 * clamp(0.60 * agg["confirm_share"] + 0.25 * clamp((n_conf - 1) / 2.0)
                              + 0.15 * agg.get("propagation", 0.0))
    return {"orderbook": ob, "liquidity": liq, "buy_pressure": buy, "cross_venue": cross,
            "absorbing": absorbing}


# --------------------------------------------------------------------------
# 4. Late-move assessment (hard thresholds + volatility-normalised displacement)

def sigma_estimates(closes: Sequence[float], horizons=(15, 30, 60)) -> Dict[int, Optional[float]]:
    """Robust sigma (in %) of overlapping h-minute log returns from minute closes."""
    out: Dict[int, Optional[float]] = {}
    logs = [math.log(c) for c in closes if c and c > 0]
    for h in horizons:
        if len(logs) < h + 30:
            out[h] = None
            continue
        rets = [(logs[i + h] - logs[i]) * 100.0 for i in range(0, len(logs) - h)]
        _, scale = robust_stats(rets, 0.0, 1e-9)
        out[h] = scale
    return out


def late_assessment(returns: Dict[int, Optional[float]], sigmas: Dict[int, Optional[float]],
                    sigma_reliable: bool, lcfg: Dict[str, Any]) -> Dict[str, Any]:
    hard = {int(k): float(v) for k, v in lcfg["hard_pct"].items()}
    fb = {int(k): float(v) for k, v in lcfg["fallback_sigma_pct"].items()}
    mins = {int(k): float(v) for k, v in lcfg["min_sigma_pct"].items()}
    dw = float(lcfg.get("down_weight", 0.7))
    zlate = float(lcfg.get("vol_z_late", 3.0))
    min_abs = float(lcfg.get("vol_min_abs_pct", 1.0))
    L_hard = 0.0
    L_vol = 0.0
    trig = None
    zs: Dict[int, Optional[float]] = {}
    for h, thr in hard.items():
        r = returns.get(h)
        if r is None:
            zs[h] = None
            continue
        lh = (r / thr) if r > 0 else (-r / thr) * dw
        if lh > L_hard:
            L_hard = lh
            if lh >= L_vol:
                trig = {"horizon": h, "kind": "hard", "ret": r, "threshold": thr}
        sig = sigmas.get(h) if sigma_reliable else None
        sig = max(sig, mins.get(h, 0.1)) if sig else None
        if sig is None:
            zs[h] = None
            continue
        z = r / sig
        zs[h] = z
        if abs(r) < min_abs:
            continue  # tiny absolute moves on very calm assets are not "late"
        lv = (z / zlate) if z > 0 else (-z / zlate) * dw
        if lv > L_vol:
            L_vol = lv
            if lv > L_hard:
                trig = {"horizon": h, "kind": "vol", "ret": r, "z": z, "sigma": sig}
    L = max(L_hard, L_vol)
    start = float(lcfg.get("penalty_start", 0.35))
    in_prog = float(lcfg.get("in_progress", 0.6))
    min_mult = float(lcfg.get("min_multiplier", 0.3))
    if L <= start:
        mult, state = 1.0, "FLAT"
    elif L < 1.0:
        mult = 1.0 - (1.0 - min_mult) * (L - start) / (1.0 - start)
        state = "IN_PROGRESS" if L >= in_prog else "MOVING"
    else:
        mult, state = min_mult, "LATE"
    cap = float(lcfg.get("late_cap", 25.0)) if L >= 1.0 else None
    return {"L": L, "L_hard": L_hard, "L_vol": L_vol, "multiplier": mult, "cap": cap, "state": state,
            "trigger": trig, "z": zs, "sigma_reliable": sigma_reliable,
            "sigmas_used": {h: (sigmas.get(h) if sigma_reliable and sigmas.get(h) else fb.get(h)) for h in hard}}


def compression_ratio(current_range_pct: Optional[float], closes: Sequence[float],
                      window: int = 45) -> Optional[float]:
    """Current range over `window` minutes vs the median rolling range over the closes."""
    if current_range_pct is None or len(closes) < window * 4:
        return None
    ranges = []
    for i in range(0, len(closes) - window, 5):
        seg = closes[i:i + window]
        lo, hi = min(seg), max(seg)
        if lo > 0:
            ranges.append((hi / lo - 1.0) * 100.0)
    typ = median(ranges)
    if not typ or typ <= 0:
        return None
    return current_range_pct / typ


# --------------------------------------------------------------------------
# 5. The ordered pipeline

def _cap_lookup(table: Dict[str, float], n: int) -> Optional[float]:
    v = table.get(str(n))
    return float(v) if v is not None else None


def context_points(intel: Optional[Dict[str, Any]], max_points: float) -> Tuple[float, List[str]]:
    """Bounded on-chain context. Missing (None) inputs contribute exactly 0."""
    if not intel:
        return 0.0, []
    pts = 0.0
    notes: List[str] = []
    for key, w in (("cex_flow", 0.04), ("whale", 0.03), ("scarcity", 0.04)):
        v = intel.get(key)
        if v is None:
            continue
        d = intel.get(f"{key}_direction")
        if d in ("INFLOW", "DISTRIBUTION"):
            pts -= w * v
            notes.append(f"{key}:-")
        elif d in ("OUTFLOW", "ACCUMULATION", "REAL_SUPPLY_DRAIN"):
            pts += w * v
            notes.append(f"{key}:+")
    mm = intel.get("mm")
    if mm is not None:
        d = intel.get("mm_direction")
        if d == "OFF_EXCHANGE":
            pts += 0.02 * mm
        elif d == "TO_EXCHANGE":
            pts -= 0.02 * mm
    return clamp(pts, -max_points, max_points), notes


def run_pipeline(agg: Dict[str, Any], subs: Dict[str, Any], late: Dict[str, Any],
                 compression: Optional[float], intel: Optional[Dict[str, Any]],
                 scfg: Dict[str, Any]) -> Dict[str, Any]:
    w = scfg["weights"]
    # 1) structural
    s0 = (w["orderbook"] * subs["orderbook"] + w["cross_venue"] * subs["cross_venue"]
          + w["buy_pressure"] * subs["buy_pressure"] + w["liquidity"] * subs["liquidity"])
    s0 = clamp(s0, 0.0, 100.0)

    # 2) compression bonus + bounded context
    bonus_frac = 0.0
    if compression is not None:
        bonus_frac = float(scfg["compression_max_bonus"]) * ramp(compression, float(scfg["compression_ratio"]), 0.3)
    ctx, ctx_notes = context_points(intel, float(scfg["context_max_points"]))
    s1 = clamp(s0 * (1.0 + bonus_frac) + ctx, 0.0, 100.0)

    # 3) confidence
    conf = agg["confidence"]
    s2 = s1 * (0.35 + 0.65 * conf)
    caps: List[Dict[str, Any]] = []
    for thr, cap in scfg["activity_caps"]:
        if agg["activity_conf"] < thr:
            caps.append({"name": "low activity", "cap": float(cap),
                         "why": f"tape confidence {agg['activity_conf']:.2f} < {thr}"})
            break
    bthr, bcap = scfg["book_conf_cap"]
    if agg["book_conf"] < bthr:
        caps.append({"name": "tiny book", "cap": float(bcap), "why": f"book confidence {agg['book_conf']:.2f}"})

    # 4) structural caps
    active = [k for k in FAMILIES if agg["family"][k] >= float(scfg["family_active"])]
    if "volume" in active and (agg.get("buy_share") is not None and agg.get("buy_share_base") is not None
                               and agg["buy_share"] < agg["buy_share_base"] - 0.05):
        active.remove("volume")
    if agg["n_confirmed"] >= 2:
        active.append("cross_venue")
    if intel and ctx > 0 and any(intel.get(k) is not None for k in ("cex_flow", "whale", "scarcity")):
        active.append("onchain")
    n_fam = len(active)
    fc = _cap_lookup(scfg["family_caps"], n_fam)
    if fc is not None:
        caps.append({"name": "independent signals", "cap": fc, "why": f"{n_fam} independent signal famil{'y' if n_fam == 1 else 'ies'}"})
    cc = _cap_lookup(scfg["coverage_caps"], agg["n_live"])
    if cc is not None:
        caps.append({"name": "venue coverage", "cap": cc, "why": f"only {agg['n_live']} live venue{'s' if agg['n_live'] != 1 else ''}"})
    kc = _cap_lookup(scfg["confirmed_caps"], agg["n_confirmed"])
    if kc is not None:
        caps.append({"name": "confirmations", "cap": kc, "why": f"{agg['n_confirmed']} confirming venue{'s' if agg['n_confirmed'] != 1 else ''}"})
    if agg["n_confirmed"] >= 1 and agg["confirm_share"] < float(scfg["min_confirm_liquidity_share"]):
        caps.append({"name": "liquidity share", "cap": float(scfg["liquidity_share_cap"]),
                     "why": f"confirming venues hold {agg['confirm_share']:.0%} of liquidity"})
    if agg["live_share"] < float(scfg["partial_coverage_share"]):
        caps.append({"name": "partial coverage", "cap": float(scfg["partial_cap"]),
                     "why": f"only {agg['live_share']:.0%} of selected liquidity is live"})
    if agg["n_warmed"] < max(1, math.ceil(agg["n_live"] / 2.0)):
        caps.append({"name": "warm-up", "cap": float(scfg["warmup_cap"]),
                     "why": f"{agg['n_warmed']}/{agg['n_live']} venues have a baseline"})
    cap_total = min([c["cap"] for c in caps] or [100.0])
    s3 = min(s2, cap_total)

    # 5) late-move penalty and hard cap — LAST
    s4 = s3 * late["multiplier"]
    if late["cap"] is not None:
        caps.append({"name": "late move", "cap": late["cap"], "why": "move already expanded"})
        s4 = min(s4, late["cap"])
    current_cap = min([c["cap"] for c in caps] or [100.0])
    binding = min(caps, key=lambda c: c["cap"]) if caps and current_cap < s2 else None

    return {
        "structural": s0, "compression_bonus": bonus_frac, "context_points": ctx, "context_notes": ctx_notes,
        "after_context": s1, "after_confidence": s2, "caps": caps, "after_caps": s3,
        "late_multiplier": late["multiplier"], "instant": s4, "current_cap": current_cap,
        "binding_cap": binding, "families": active, "n_families": n_fam,
    }
