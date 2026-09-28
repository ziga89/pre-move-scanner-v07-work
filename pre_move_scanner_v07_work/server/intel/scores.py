"""MM / Whale / CEX-flow / Scarcity scores from classified transfers.

Null semantics (v0.7 amendment 8): every score is None (displayed N/A) unless
wallet coverage for the asset exists (configured token on a supported chain,
reliable labelled addresses polled successfully). None is never read as 0 or
as negative. A numeric 0 means "covered, nothing notable".

Scores are 0–100 *strengths* with a separate direction label; the scoring
pipeline turns them into bounded context points (±8 max).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..util import ramp
from .classify import ACC, DIST, SHIFT, UNKNOWN
from .labels import CEX_TYPES, HOLDER_TYPES

WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400}


def _usd(t: Dict[str, Any]) -> float:
    return float(t.get("usd_value") or 0.0)


def flow_windows(transfers: List[Dict[str, Any]], now: float) -> Dict[str, Dict[str, float]]:
    out = {}
    for name, secs in WINDOWS.items():
        w = [t for t in transfers if now - t["ts"] <= secs]
        # Reserve flows exclude SHIFTs; transfers to/from UNLABELLED addresses are
        # real reserve changes but unattributed, so they count at half weight.
        def wt(t):
            return 0.5 if t["classification"] == UNKNOWN else 1.0
        cex_in = sum(_usd(t) * wt(t) for t in w if t.get("to_type") in CEX_TYPES and t["classification"] != SHIFT)
        cex_out = sum(_usd(t) * wt(t) for t in w if t.get("from_type") in CEX_TYPES and t["classification"] != SHIFT)
        out[name] = {
            "accumulation_usd": sum(_usd(t) for t in w if t["classification"] == ACC),
            "distribution_usd": sum(_usd(t) for t in w if t["classification"] == DIST),
            "shift_usd": sum(_usd(t) for t in w if t["classification"] == SHIFT),
            "unknown_usd": sum(_usd(t) for t in w if t["classification"] == UNKNOWN),
            "cex_inflow_usd": cex_in, "cex_outflow_usd": cex_out, "net_cex_outflow_usd": cex_out - cex_in,
            "mm_in_usd": sum(_usd(t) for t in w if t.get("to_type") == "MM"),
            "mm_out_usd": sum(_usd(t) for t in w if t.get("from_type") == "MM"),
            "transfers": len(w),
        }
    return out


def whale_candidates(tx: List[Dict[str, Any]], min_usd: float, limit: int = 10) -> List[Dict[str, Any]]:
    """Large transfers between a labelled exchange and an UNLABELLED address.

    Shown as "UNKNOWN / WHALE CANDIDATE": the address is not identified, the transfer is
    not counted in any score (it may be the exchange's own unlabelled wallet), and no
    identity is inferred.
    """
    out = []
    for t in tx:
        usd = _usd(t)
        if usd < min_usd or t.get("classification") != UNKNOWN:
            continue
        if t.get("from_type") in CEX_TYPES and not t.get("to_type"):
            out.append({"address": t.get("to_addr"), "side": "received from exchange",
                        "counterparty": t.get("from_entity"), "usd": usd, "ts": t.get("ts"), "tx": t.get("tx_hash"),
                        "label": "UNKNOWN / WHALE CANDIDATE"})
        elif t.get("to_type") in CEX_TYPES and not t.get("from_type"):
            out.append({"address": t.get("from_addr"), "side": "sent to exchange",
                        "counterparty": t.get("to_entity"), "usd": usd, "ts": t.get("ts"), "tx": t.get("tx_hash"),
                        "label": "UNKNOWN / WHALE CANDIDATE"})
    out.sort(key=lambda x: -x["usd"])
    return out[:limit]


def compute_scores(asset: str, transfers: List[Dict[str, Any]], coverage: Dict[str, Any], now: float,
                   daily_volume_usd: Optional[float], ask_thinning: float = 0.0,
                   whale_candidate_usd: float = 250_000.0) -> Dict[str, Any]:
    na = {"mm": None, "whale": None, "cex_flow": None, "scarcity": None, "mm_direction": None,
          "whale_direction": None, "cex_flow_direction": None, "scarcity_direction": None,
          "cex_outflow_attributed_share": None, "whale_candidates": [],
          "reasons": [], "coverage": coverage, "status": coverage.get("reason", "n/a")}
    if not coverage.get("covered"):
        return na
    tx = [t for t in transfers if t["asset"] == asset and now - t["ts"] <= 86400]
    if any(t.get("usd_value") is None for t in tx) or not daily_volume_usd:
        na["status"] = "no USD price / volume reference for normalisation"
        return na
    fw = flow_windows(tx, now)
    w = fw["24h"]
    vol = float(daily_volume_usd)
    incomplete = bool(coverage.get("lagging"))
    damp = 0.6 if incomplete else 1.0
    res: Dict[str, Any] = {"coverage": coverage, "windows": fw, "reasons": [], "status": "ok" + (" (partial coverage)" if incomplete else "")}

    # --- CEX flow: net reserve change excluding SHIFTs (CEX<->CEX, CEX<->MM, internal)
    net_out = w["net_cex_outflow_usd"]
    ratio = abs(net_out) / vol
    res["cex_flow"] = round(100 * ramp(ratio, 0.002, 0.03) * damp, 1)
    res["cex_flow_direction"] = "OUTFLOW" if net_out > 0 and res["cex_flow"] > 0 else "INFLOW" if net_out < 0 and res["cex_flow"] > 0 else "NEUTRAL"
    # Share of exchange outflow that reached LABELLED holders; the rest (unlabelled recipients)
    # may be the exchange's own wallets and must never be read as buying.
    out_all = sum(_usd(t) for t in tx if t.get("from_type") in CEX_TYPES and t["classification"] != SHIFT)
    out_attr = sum(_usd(t) for t in tx if t.get("from_type") in CEX_TYPES and t["classification"] == ACC)
    res["cex_outflow_attributed_share"] = round(out_attr / out_all, 3) if out_all > 0 else None
    res["whale_candidates"] = whale_candidates(tx, whale_candidate_usd)
    if res["cex_flow"] >= 30:
        res["reasons"].append(f"Net CEX {'outflow' if net_out > 0 else 'inflow'} ${abs(net_out):,.0f} (24h, excl. shifts)")

    # --- MM: net inventory change vs gross routing
    mm_in, mm_out = w["mm_in_usd"], w["mm_out_usd"]
    gross = mm_in + mm_out
    net = mm_in - mm_out
    direc = abs(net) / gross if gross else 0.0
    res["mm"] = round(100 * ramp(abs(net) / vol, 0.002, 0.03) * ramp(direc, 0.3, 0.8) * damp, 1)
    if gross and direc < 0.3:
        res["mm_direction"] = "ROUTING"
        if gross / vol >= 0.005:
            res["reasons"].append(f"MM routing/rebalancing: ${gross:,.0f} gross, net ${net:+,.0f} (not directional)")
    elif net > 0:
        cex_src = sum(_usd(t) for t in tx if t.get("to_type") == "MM" and t.get("from_type") in CEX_TYPES)
        res["mm_direction"] = "OFF_EXCHANGE" if cex_src >= 0.5 * mm_in else "INVENTORY_UP"
    elif net < 0:
        cex_dst = sum(_usd(t) for t in tx if t.get("from_type") == "MM" and t.get("to_type") in CEX_TYPES)
        res["mm_direction"] = "TO_EXCHANGE" if cex_dst >= 0.5 * mm_out else "INVENTORY_DOWN"
    else:
        res["mm_direction"] = "NEUTRAL"
    if res["mm"] >= 30 and res["mm_direction"] == "OFF_EXCHANGE":
        res["reasons"].append(f"MM inventory shifted off exchange (net ${net:,.0f})")

    # --- Whales: accumulation-side vs distribution-side, with distinct wallet counts
    acc, dist = w["accumulation_usd"], w["distribution_usd"]
    acc_w = {t["to_addr"] for t in tx if t["classification"] == ACC}
    dist_w = {t["from_addr"] for t in tx if t["classification"] == DIST}
    mixed = (min(acc, dist) / max(acc, dist)) if max(acc, dist) > 0 else 0.0
    wnet = acc - dist
    res["whale"] = round(100 * ramp(abs(wnet) / vol, 0.002, 0.03) * (1 - 0.7 * mixed) * damp, 1)
    res["whale_wallets"] = {"accumulating": len(acc_w), "distributing": len(dist_w)}
    if mixed >= 0.5 and max(acc, dist) > 0:
        res["whale_direction"] = "MIXED"
        res["reasons"].append(f"Whales mixed: {len(acc_w)} accumulating / {len(dist_w)} distributing (net ${wnet:+,.0f})")
    elif wnet > 0:
        res["whale_direction"] = "ACCUMULATION"
        if res["whale"] >= 30:
            res["reasons"].append(f"Whale accumulation-side flow ${acc:,.0f} ({len(acc_w)} wallets)")
    elif wnet < 0:
        res["whale_direction"] = "DISTRIBUTION"
    else:
        res["whale_direction"] = "NEUTRAL"

    # --- Scarcity: REAL supply drain vs reshuffling
    returned = sum(_usd(t) for t in tx if t["classification"] == DIST and t["from_addr"] in acc_w)
    drain = (res["cex_flow_direction"] == "OUTFLOW" and res["cex_flow"] >= 30 and res["whale_direction"] == "ACCUMULATION"
             and res["whale"] >= 30 and returned < 0.2 * max(acc, 1.0))
    total_moves = acc + dist + w["shift_usd"]
    if drain:
        res["scarcity"] = round(min(res["cex_flow"], res["whale"]) * (1.0 if ask_thinning >= 0.3 else 0.5), 1)
        res["scarcity_direction"] = "REAL_SUPPLY_DRAIN"
        if ask_thinning >= 0.3:
            res["reasons"].append("Real supply drain: CEX outflow to holders while asks thin")
    elif total_moves / vol >= 0.01 and abs(net_out) / max(total_moves, 1.0) < 0.3:
        res["scarcity"] = 0.0
        res["scarcity_direction"] = "RESHUFFLING"
        res["reasons"].append("Large wallet moves net out (custody / exchange / MM reshuffling)")
    else:
        res["scarcity"] = 0.0
        res["scarcity_direction"] = "NEUTRAL"
    return res


def entity_balances(balances: Dict[tuple, List[tuple]], labels, asset_token: str, now: float) -> List[Dict[str, Any]]:
    """Balance now and Δ1h/6h/24h/7d per labelled entity for one token."""
    per: Dict[str, Dict[str, Any]] = {}
    for (chain, token, addr), hist in balances.items():
        if token != asset_token or not hist:
            continue
        lab = labels.lookup(chain, addr)
        name = lab.entity if lab else addr
        e = per.setdefault(name, {"entity": name, "entity_type": lab.entity_type if lab else None,
                                  "addresses": 0, "balance": 0.0, "d": {"1h": 0.0, "6h": 0.0, "24h": 0.0, "7d": 0.0},
                                  "complete": {"1h": True, "6h": True, "24h": True, "7d": True}})
        e["addresses"] += 1
        cur = hist[-1][1]
        e["balance"] += cur
        for k, secs in (("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)):
            past = [b for t, b in hist if t <= now - secs]
            if past:
                e["d"][k] += cur - past[-1]
            else:
                e["complete"][k] = False
    out = []
    for e in per.values():
        e["delta"] = {k: (round(v, 4) if e["complete"][k] else None) for k, v in e["d"].items()}
        del e["d"], e["complete"]
        out.append(e)
    return sorted(out, key=lambda e: -abs(e["balance"]))
