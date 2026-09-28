"""Human-readable reasons for a score ("why is this coin ranked here?")."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _pct(x: Optional[float], signed: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.0f}%" if signed else f"{x * 100:.0f}%"


def _money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def price_suffix(returns: Dict[int, Optional[float]]) -> str:
    r60, r15 = returns.get(60), returns.get(15)
    if r60 is not None:
        return f"price {r60:+.1f}% (1h)"
    if r15 is not None:
        return f"price {r15:+.1f}% (15m)"
    return ""


def build_reasons(asset: str, agg: Dict[str, Any], pipe: Dict[str, Any], late: Dict[str, Any],
                  returns: Dict[int, Optional[float]], comp: Optional[float], prop: Optional[Dict[str, Any]],
                  intel: Optional[Dict[str, Any]], status: str) -> Dict[str, Any]:
    fam = agg["family"]
    counts = agg.get("family_venue_counts", {})
    n_w = max(agg["n_warmed"], 1)
    cands: List[tuple] = []

    if fam["thinning"] > 0.15 and counts.get("thinning", 0) >= 1:
        n = counts.get("thinning", 0)
        if agg.get("ask_ratio") is not None:
            txt = f"Ask depth {_pct(agg['ask_ratio'] - 1)} vs normal; thinning on {n}/{n_w} venue{'s' if n_w > 1 else ''}"
        else:
            txt = f"Ask depth thinning on {n}/{n_w} venues"
        cands.append((fam["thinning"] * 0.45 * 35, txt))
    if fam["no_replenish"] > 0.15 and counts.get("no_replenish", 0) >= 1:
        n = counts.get("no_replenish", 0)
        txt = f"{n}/{n_w} venues show weak ask replenishment"
        if agg.get("refill") is not None:
            txt += f" (refill after fills {agg['refill']:.2f})"
        cands.append((fam["no_replenish"] * 0.33 * 35, txt))
    if fam["buy_flow"] > 0.15 and agg.get("buy_share") is not None:
        base = agg.get("buy_share_base")
        txt = f"Aggressive buys {_pct(agg['buy_share'], False)}"
        txt += f" vs normal {_pct(base, False)}" if base is not None else ""
        txt += f" on {_money(agg.get('vol_60'))}/min"
        cands.append((fam["buy_flow"] * 0.7 * 22, txt))
    if fam["volume"] > 0.15 and agg.get("vol_ratio"):
        txt = f"Volume {agg['vol_ratio']:.1f}× normal"
        if late.get("state") == "FLAT":
            txt += " but price still compressed"
        cands.append((fam["volume"] * 0.3 * 22, txt))
    if fam["bid_support"] > 0.15:
        cands.append((fam["bid_support"] * 0.22 * 35, "Bids steady while asks weaken"))
    if fam["slip"] > 0.2 and agg.get("slippage_ratio"):
        cands.append((fam["slip"] * 0.35 * 18, f"Buy-side slippage {agg['slippage_ratio']:.1f}× normal"))
    if prop and prop.get("leader") and prop.get("followers"):
        fol = ", ".join(f["venue"] for f in prop["followers"][:3])
        mins = max(1, round(max(f["after_s"] for f in prop["followers"]) / 60))
        cands.append((12.0, f"{prop['leader']} led; {fol} confirmed within {mins} min"))
    elif agg["n_confirmed"] >= 2:
        cands.append((10.0, f"{agg['n_confirmed']}/{agg['n_live']} venues confirm"))
    if pipe.get("compression_bonus", 0) > 0 and comp is not None:
        cands.append((pipe["compression_bonus"] * 100, f"price range {comp:.1f}× its normal (compressed)"))
    for r in (intel or {}).get("reasons", []) or []:
        cands.append((6.0, r))

    cands.sort(key=lambda x: -x[0])
    items = [t for _, t in cands]

    head: List[str] = []
    if status == "LATE" and late.get("trigger"):
        t = late["trigger"]
        if t["kind"] == "hard":
            head.append(f"Move already expanded: {t['ret']:+.1f}% in {t['horizon']}m (≥{t['threshold']:.0f}% threshold)")
        else:
            head.append(f"Move already large for {asset}: {t['ret']:+.1f}% in {t['horizon']}m ({abs(t['z']):.1f}σ)")
    elif status == "MOVE IN PROGRESS" and late.get("trigger"):
        t = late["trigger"]
        head.append(f"Move in progress: {t['ret']:+.1f}% in {t['horizon']}m")
    elif status == "LOW CONFIDENCE":
        head.append(f"Low confidence: {_money(agg.get('vol_60'))} traded, {int(agg.get('trades_60') or 0)} trades in 60s")
    elif status == "WARMING":
        head.append("Building baselines (warm-up)")

    cap_txt = None
    b = pipe.get("binding_cap")
    if b:
        cap_txt = f"capped at {b['cap']:.0f}: {b['why']}"

    lst = head + items
    text_parts = lst[:3]
    ps = price_suffix(returns)
    if ps and status not in ("LATE", "MOVE IN PROGRESS"):
        text_parts.append(ps)
    text = "; ".join(text_parts) if text_parts else "no notable structure"
    return {"list": lst + ([cap_txt] if cap_txt else []), "text": text, "cap": cap_txt}
