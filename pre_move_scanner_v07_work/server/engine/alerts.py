"""Strict composite high-conviction alerting.

This layer intentionally sits *above* the ordinary Pre-Move score.  It is not
an estimator of trade probability and never calls a wallet transfer a buy.
A HIGH_CONVICTION alert is only allowed when independent market-structure and
execution evidence agree across several live venues while price is still flat.
Wallet intelligence can strengthen or veto the setup, but is never required.

The state machine adds two protections against noisy banners:
* persistence: strict conditions must hold for a sustained interval before fire
* hysteresis: a fired alert survives a brief dip, then clears if the setup stays
  invalid; LATE / hostile-wallet evidence clears immediately.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..util import clamp, ramp, rnd


BULLISH_WALLET = {
    "cex_flow": {"OUTFLOW"},
    "whale": {"ACCUMULATION"},
    "scarcity": {"REAL_SUPPLY_DRAIN"},
    "mm": {"OFF_EXCHANGE"},
}
HOSTILE_WALLET = {
    "cex_flow": {"INFLOW"},
    "whale": {"DISTRIBUTION"},
    "mm": {"TO_EXCHANGE"},
}


class HighConvictionAlerts:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.state: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def _st(self, asset: str) -> Dict[str, Any]:
        return self.state.setdefault(asset, {
            "candidate_since": None,
            "active": None,
            "bad_since": None,
        })

    @staticmethod
    def _wallet(res: Dict[str, Any]) -> Dict[str, Any]:
        intel = res.get("intel") or {}
        available = any(intel.get(k) is not None for k in ("cex_flow", "whale", "scarcity", "mm"))
        supportive: List[str] = []
        hostile: List[str] = []
        pos_scores: List[float] = []
        for key in ("cex_flow", "whale", "scarcity", "mm"):
            val = intel.get(key)
            if val is None:
                continue
            direction = intel.get(f"{key}_direction")
            if direction in BULLISH_WALLET.get(key, set()) and float(val) >= 35.0:
                supportive.append(f"{key}:{direction.lower()}")
                pos_scores.append(float(val))
            if direction in HOSTILE_WALLET.get(key, set()) and float(val) >= 50.0:
                hostile.append(f"{key}:{direction.lower()}")
        if hostile:
            status = "hostile"
        elif len(supportive) >= 2:
            status = "supportive"
        elif available:
            status = "neutral"
        else:
            status = "unavailable"
        return {
            "available": available,
            "status": status,
            "supportive": supportive,
            "hostile": hostile,
            "score": (sum(pos_scores) / len(pos_scores)) if pos_scores else None,
        }

    def assess(self, res: Dict[str, Any]) -> Dict[str, Any]:
        c = self.cfg
        subs = res.get("subscores") or {}
        agg = res.get("agg") or {}
        fam = agg.get("family") or {}
        fam_counts = agg.get("family_venue_counts") or {}
        wallet = self._wallet(res)

        pm = float(res.get("premove") or 0.0)
        orderbook = float(subs.get("orderbook") or 0.0)
        buy = float(subs.get("buy_pressure") or 0.0)
        cross = float(subs.get("cross_venue") or 0.0)
        conf = float(res.get("confidence") or 0.0)
        confirmed = int(res.get("confirmed") or 0)
        coverage = int(res.get("coverage") or 0)
        coverage_total = int(res.get("coverage_total") or coverage)
        live_share = float(res.get("live_share") or 0.0)
        top_trade = float(agg.get("top_trade_share") or 0.0)
        late_state = (res.get("late") or {}).get("state")

        # Independent bullish market families.  We deliberately require both a
        # book-side family and aggressive-buy flow; volume alone cannot pass.
        active = set(res.get("families") or [])
        base_active = active & {"thinning", "no_replenish", "buy_flow", "volume", "bid_support"}
        book_family = bool(base_active & {"thinning", "no_replenish", "bid_support"})
        flow_family = "buy_flow" in base_active
        structural_venues = max(int(fam_counts.get("thinning") or 0), int(fam_counts.get("no_replenish") or 0))
        buy_venues = int(fam_counts.get("buy_flow") or 0)

        # A 0..100 evidence score for display.  It is *not* a probability.
        structure = clamp(
            0.58 * orderbook
            + 18.0 * float(fam.get("thinning") or 0.0)
            + 14.0 * float(fam.get("no_replenish") or 0.0)
            + 10.0 * float(fam.get("bid_support") or 0.0),
            0.0, 100.0,
        )
        vol_strength = 100.0 * ramp(agg.get("vol_ratio"), 1.25, 3.0)
        execution = clamp(0.48 * buy + 0.32 * cross + 0.12 * (conf * 100.0) + 0.08 * vol_strength,
                          0.0, 100.0)
        wallet_score = wallet.get("score")
        if wallet_score is not None and wallet["status"] == "supportive":
            evidence = 0.34 * pm + 0.28 * structure + 0.25 * execution + 0.13 * float(wallet_score)
        else:
            evidence = 0.39 * pm + 0.32 * structure + 0.29 * execution
        evidence = clamp(evidence, 0.0, 99.0)

        if wallet["status"] == "supportive":
            min_pm = float(c.get("min_premove_with_wallet", 82.0))
        elif wallet["available"]:
            min_pm = float(c.get("min_premove_wallet_neutral", 86.0))
        else:
            min_pm = float(c.get("min_premove_no_wallet", 90.0))

        checks = {
            "price_still_flat": late_state == "FLAT",
            "premove": pm >= min_pm,
            "venues": confirmed >= int(c.get("min_confirmed_venues", 3)) and coverage >= int(c.get("min_live_venues", 3)),
            "coverage": live_share >= float(c.get("min_live_liquidity_share", 0.70)),
            "feed_complete": (coverage_total <= 0 or coverage / coverage_total >= float(c.get("min_selected_live_ratio", 1.0))),
            "confidence": conf >= float(c.get("min_engine_confidence", 0.55)),
            "orderbook": orderbook >= float(c.get("min_orderbook", 65.0)),
            "buy_pressure": buy >= float(c.get("min_buy_pressure", 60.0)),
            "cross_venue": cross >= float(c.get("min_cross_venue", 68.0)),
            "volume": agg.get("vol_ratio") is not None and float(agg.get("vol_ratio")) >= float(c.get("min_vol_ratio", 1.25)),
            "spread_ok": float(fam.get("spread") or 0.0) <= float(c.get("max_spread_anomaly", 0.65)),
            "families": len(base_active) >= int(c.get("min_base_families", 3)) and book_family and flow_family,
            "family_venues": structural_venues >= int(c.get("min_structure_venues", 2)) and buy_venues >= int(c.get("min_buy_venues", 2)),
            "not_one_print": top_trade <= float(c.get("max_top_trade_share", 0.35)),
            "wallet_not_hostile": wallet["status"] != "hostile",
        }
        strict = bool(c.get("enabled", True)) and all(checks.values())

        reasons: List[str] = []
        if orderbook >= float(c.get("min_orderbook", 65.0)):
            reasons.append(f"order-book {orderbook:.0f}")
        if buy >= float(c.get("min_buy_pressure", 60.0)):
            reasons.append(f"buy pressure {buy:.0f}")
        if confirmed:
            reasons.append(f"{confirmed}/{coverage} venues confirm")
        if agg.get("vol_ratio") is not None and float(agg["vol_ratio"]) >= 1.25:
            reasons.append(f"volume {float(agg['vol_ratio']):.1f}x normal")
        if wallet["status"] == "supportive":
            reasons.append("wallet/CEX flow supportive")
        elif wallet["status"] == "unavailable":
            reasons.append("wallet data unavailable; stricter threshold used")

        return {
            "strict": strict,
            "checks": checks,
            "threshold": min_pm,
            "evidence_score": rnd(evidence, 1),
            "structure_score": rnd(structure, 1),
            "execution_score": rnd(execution, 1),
            "wallet": wallet,
            "reasons": reasons,
        }

    def update(self, res: Dict[str, Any], now: float) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], bool]:
        """Return (active_alert, emitted_events, fired_now)."""
        asset = res["asset"]
        st = self._st(asset)
        a = self.assess(res)
        events: List[Dict[str, Any]] = []
        fired_now = False
        persist_s = float(self.cfg.get("persistence_seconds", 120.0))
        clear_s = float(self.cfg.get("clear_after_seconds", 60.0))

        if a["strict"]:
            st["bad_since"] = None
            if st["candidate_since"] is None:
                st["candidate_since"] = now
            if st["active"] is None and now - st["candidate_since"] >= persist_s:
                self._seq += 1
                alert = {
                    "id": f"{asset}-{int(st['candidate_since'])}-{self._seq}",
                    "asset": asset,
                    "state": "HIGH_CONVICTION",
                    "started_ts": st["candidate_since"],
                    "fired_ts": now,
                    "updated_ts": now,
                    "duration_s": now - st["candidate_since"],
                    "evidence_score": a["evidence_score"],
                    "peak_evidence": a["evidence_score"],
                    "premove": res.get("premove"),
                    "peak_premove": res.get("premove"),
                    "price": res.get("price"),
                    "price_at_fire": res.get("price"),
                    "confirmed": res.get("confirmed"),
                    "coverage": res.get("coverage"),
                    "confirmed_venues": list(res.get("confirmed_venues") or []),
                    "structure_score": a["structure_score"],
                    "execution_score": a["execution_score"],
                    "wallet_status": a["wallet"]["status"],
                    "reasons": list(a["reasons"]),
                }
                st["active"] = alert
                fired_now = True
                events.append({
                    "asset": asset, "ts": now, "category": "ALERT", "event_type": "high_conviction_buy_setup",
                    "severity": 3, "level": a["evidence_score"],
                    "message": f"HIGH-CONVICTION BUY SETUP · evidence {a['evidence_score']:.0f}/100 · "
                               + " · ".join(a["reasons"][:4]),
                    "evidence": {"alert": alert, "checks": a["checks"],
                                 "note": "Evidence score is not a probability or proof of a purchase."},
                })
            elif st["active"] is not None:
                al = st["active"]
                al["updated_ts"] = now
                al["duration_s"] = now - al["started_ts"]
                al["evidence_score"] = a["evidence_score"]
                al["peak_evidence"] = max(float(al.get("peak_evidence") or 0.0), float(a["evidence_score"] or 0.0))
                al["premove"] = res.get("premove")
                al["peak_premove"] = max(float(al.get("peak_premove") or 0.0), float(res.get("premove") or 0.0))
                al["price"] = res.get("price")
                al["confirmed"] = res.get("confirmed")
                al["coverage"] = res.get("coverage")
                al["confirmed_venues"] = list(res.get("confirmed_venues") or [])
                al["structure_score"] = a["structure_score"]
                al["execution_score"] = a["execution_score"]
                al["wallet_status"] = a["wallet"]["status"]
                al["reasons"] = list(a["reasons"])
        else:
            # A candidate that never fired resets immediately.  A fired alert
            # has hysteresis, except for hard invalidation: move already begun
            # or strongly bearish wallet/CEX evidence.
            if st["active"] is None:
                st["candidate_since"] = None
            else:
                hard = ((res.get("late") or {}).get("state") != "FLAT" or a["wallet"]["status"] == "hostile")
                if st["bad_since"] is None:
                    st["bad_since"] = now
                if hard or now - st["bad_since"] >= clear_s:
                    al = st["active"]
                    al["state"] = "INVALIDATED"
                    al["updated_ts"] = now
                    al["ended_ts"] = now
                    al["duration_s"] = now - al["started_ts"]
                    al["price"] = res.get("price")
                    why = "move no longer flat" if (res.get("late") or {}).get("state") != "FLAT" else \
                          "hostile wallet/CEX flow" if a["wallet"]["status"] == "hostile" else "composite confirmation faded"
                    events.append({
                        "asset": asset, "ts": now, "category": "ALERT", "event_type": "high_conviction_cleared",
                        "severity": 0, "level": al.get("evidence_score"),
                        "message": f"High-conviction setup cleared: {why}",
                        "evidence": {"alert": dict(al), "checks": a["checks"]},
                    })
                    st["active"] = None
                    st["candidate_since"] = None
                    st["bad_since"] = None

        active = st.get("active")
        return (dict(active) if active else None), events, fired_now

    def active(self) -> List[Dict[str, Any]]:
        rows = [dict(st["active"]) for st in self.state.values() if st.get("active")]
        rows.sort(key=lambda x: (-(x.get("evidence_score") or 0.0), x.get("fired_ts") or 0.0))
        return rows

    def get(self, asset: str) -> Optional[Dict[str, Any]]:
        st = self.state.get(asset.upper())
        return dict(st["active"]) if st and st.get("active") else None
