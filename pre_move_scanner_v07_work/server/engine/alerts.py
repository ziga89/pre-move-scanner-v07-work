"""Signal Radar and strict high-conviction alerts (v0.7.2 alert rail → v0.7.3 radar).

This layer sits *above* the ordinary Pre-Move score. It is not an estimate of
trade probability and never calls a wallet transfer a buy.

Radar states (one headline state is always shown):
  NO HIGH-CONVICTION SETUP   nothing qualifies
  WATCH / CONFIRMING         WATCH: the mandatory gates hold but not every strict
                             check; CONFIRMING: every strict check holds and the
                             persistence timer is running
  HIGH-CONVICTION BUY SETUP  every strict check held continuously for
                             `persistence_seconds` (fired alert)
  INVALIDATED                a fired alert ended (move started, hostile wallet
                             flow, feeds stale, or confirmation faded)

A HIGH-CONVICTION alert needs *independent* evidence to agree:
* market structure is mandatory: a book-side family (ask thinning / no
  replenishment / bid support) on at least two venues and a strong order-book
  sub-score;
* aggressive buy flow on at least two venues; cross-venue confirmation on at
  least three venues with every selected feed live;
* price still flat (MOVING / IN_PROGRESS / LATE are rejected);
* no single print dominating the tape (`max_top_trade_share`);
* persistence: all of the above held continuously for `persistence_seconds`;
  a fired alert survives a brief dip (`clear_after_seconds`), but a move,
  hostile wallet flow or missing data end it.

Wallet intelligence can strengthen or veto a setup but is never required.
Exchange / market-maker reshuffling never counts as buying: only labelled
holders accumulating from exchanges (plus a real supply drain or an attributed
exchange outflow) is "supportive". Market-maker direction and unattributed
outflows are never supportive.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..util import clamp, ramp, rnd

LABELS = {"NONE": "NO HIGH-CONVICTION SETUP", "WATCH": "WATCH / CONFIRMING", "CONFIRMING": "WATCH / CONFIRMING",
          "HIGH_CONVICTION": "HIGH-CONVICTION BUY SETUP", "INVALIDATED": "INVALIDATED"}
MOVE_STATES = {"MOVING", "IN_PROGRESS", "LATE"}          # price no longer flat
BOOK_FAMILIES = {"thinning", "no_replenish", "bid_support"}
BASE_FAMILIES = {"thinning", "no_replenish", "buy_flow", "volume", "bid_support"}

# Directions that count against a setup (a conservative veto may include unattributed flows).
HOSTILE_WALLET = {
    "cex_flow": {"INFLOW"},
    "whale": {"DISTRIBUTION"},
    "mm": {"TO_EXCHANGE"},
}
# Directions that are routing / reshuffling: shown, never counted as buying.
RESHUFFLE_DIRECTIONS = {"mm": {"ROUTING", "OFF_EXCHANGE", "INVENTORY_UP", "INVENTORY_DOWN"},
                        "scarcity": {"RESHUFFLING"}}

CHECK_TEXT = {
    "price_still_flat": "price no longer flat",
    "premove": "pre-move score below threshold",
    "venues": "not enough confirming venues",
    "coverage": "live liquidity share too low",
    "feed_complete": "not every selected feed is live",
    "confidence": "engine confidence too low",
    "orderbook": "order-book structure too weak",
    "buy_pressure": "buy pressure too weak",
    "cross_venue": "cross-venue confirmation too weak",
    "volume": "volume not above normal",
    "spread_ok": "spread anomaly",
    "families": "needs book-side + buy-flow families (3+)",
    "family_venues": "structure / buy flow on fewer than 2 venues",
    "not_one_print": "one large trade dominates the tape",
    "wallet_not_hostile": "hostile wallet / exchange flow",
}


class HighConvictionAlerts:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.state: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._changed: Dict[str, Dict[str, Any]] = {}      # alert id -> alert row to persist

    def _st(self, asset: str) -> Dict[str, Any]:
        return self.state.setdefault(asset, {
            "candidate_since": None, "active": None, "bad_since": None,
            "watch_since": None, "watch_seen": None, "invalidated": None, "last": None, "seen": None,
        })

    # ------------------------------------------------------------------ wallet
    @staticmethod
    def _wallet(res: Dict[str, Any]) -> Dict[str, Any]:
        intel = res.get("intel") or {}
        explicit = (res.get("wallet_status") or {})
        available = any(intel.get(k) is not None for k in ("cex_flow", "whale", "scarcity", "mm"))

        def val(k: str) -> float:
            v = intel.get(k)
            return float(v) if v is not None else 0.0

        def direction(k: str) -> Optional[str]:
            return intel.get(f"{k}_direction")

        hostile: List[str] = []
        for key, dirs in HOSTILE_WALLET.items():
            if intel.get(key) is not None and direction(key) in dirs and val(key) >= 50.0:
                hostile.append(f"{key}:{direction(key).lower()}")
        reshuffle: List[str] = []
        for key, dirs in RESHUFFLE_DIRECTIONS.items():
            if intel.get(key) is not None and direction(key) in dirs:
                reshuffle.append(f"{key}:{direction(key).lower()}")

        # Only attributed accumulation supports a setup: labelled holders receiving from
        # exchanges (whale ACCUMULATION is built from labelled holders only), confirmed by a
        # real supply drain or by an exchange outflow that is mostly attributed.
        supportive: List[str] = []
        pos: List[float] = []
        whale_acc = direction("whale") == "ACCUMULATION" and val("whale") >= 35.0
        drain = direction("scarcity") == "REAL_SUPPLY_DRAIN" and val("scarcity") >= 35.0
        attributed = float(intel.get("cex_outflow_attributed_share") or 0.0)
        outflow = direction("cex_flow") == "OUTFLOW" and val("cex_flow") >= 35.0 and attributed >= 0.5
        if whale_acc:
            supportive.append("whale:accumulation")
            pos.append(val("whale"))
        if drain:
            supportive.append("scarcity:real_supply_drain")
            pos.append(val("scarcity"))
        if outflow:
            supportive.append("cex_flow:attributed_outflow")
            pos.append(val("cex_flow"))
        if hostile:
            status = "hostile"
        elif whale_acc and (drain or outflow):
            status = "supportive"
        elif available:
            status = "neutral"
        else:
            status = "unavailable"
        return {"available": available, "status": status, "supportive": supportive if status == "supportive" else [],
                "hostile": hostile, "reshuffle_not_counted": reshuffle,
                "score": (sum(pos) / len(pos)) if (pos and status == "supportive") else None,
                "state": explicit.get("state"), "label": explicit.get("label"), "reason": explicit.get("reason")}

    # ------------------------------------------------------------------ assessment
    def assess(self, res: Dict[str, Any]) -> Dict[str, Any]:
        c = self.cfg
        subs = res.get("subscores") or {}
        agg = res.get("agg") or {}
        fam = agg.get("family") or {}
        fam_counts = agg.get("family_venue_counts") or {}
        wallet = self._wallet(res)

        has_data = res.get("premove") is not None
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

        active = set(res.get("families") or [])
        base_active = active & BASE_FAMILIES
        book_family = bool(base_active & BOOK_FAMILIES)
        flow_family = "buy_flow" in base_active
        structural_venues = max(int(fam_counts.get("thinning") or 0), int(fam_counts.get("no_replenish") or 0),
                                int(fam_counts.get("bid_support") or 0))
        buy_venues = int(fam_counts.get("buy_flow") or 0)

        # A 0..100 evidence score for display. It is *not* a probability.
        structure = clamp(0.58 * orderbook + 18.0 * float(fam.get("thinning") or 0.0)
                          + 14.0 * float(fam.get("no_replenish") or 0.0) + 10.0 * float(fam.get("bid_support") or 0.0),
                          0.0, 100.0)
        vol_strength = 100.0 * ramp(agg.get("vol_ratio"), 1.25, 3.0)
        execution = clamp(0.48 * buy + 0.32 * cross + 0.12 * (conf * 100.0) + 0.08 * vol_strength, 0.0, 100.0)
        if wallet["score"] is not None:
            evidence = 0.34 * pm + 0.28 * structure + 0.25 * execution + 0.13 * float(wallet["score"])
        else:
            evidence = 0.39 * pm + 0.32 * structure + 0.29 * execution
        evidence = clamp(evidence, 0.0, 99.0) if has_data else 0.0

        if wallet["status"] == "supportive":
            min_pm = float(c.get("min_premove_with_wallet", 82.0))
        elif wallet["available"]:
            min_pm = float(c.get("min_premove_wallet_neutral", 86.0))
        else:
            min_pm = float(c.get("min_premove_no_wallet", 90.0))

        feed_ok = has_data and coverage_total > 0 and coverage / coverage_total >= float(c.get("min_selected_live_ratio", 1.0))
        checks = {
            "price_still_flat": late_state == "FLAT",
            "premove": has_data and pm >= min_pm,
            "venues": confirmed >= int(c.get("min_confirmed_venues", 3)) and coverage >= int(c.get("min_live_venues", 3)),
            "coverage": live_share >= float(c.get("min_live_liquidity_share", 0.70)),
            "feed_complete": feed_ok,
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

        # Mandatory gates for WATCH: real multi-venue structure on flat price with complete feeds.
        # A single venue, a single print, a moving price or stale feeds never reach the radar.
        market_structure = book_family and structural_venues >= int(c.get("min_structure_venues", 2)) \
            and orderbook >= float(c.get("watch_min_orderbook", 50.0))
        gate = {
            "price_still_flat": checks["price_still_flat"],
            "feed_complete": feed_ok,
            "market_structure": market_structure,
            "cross_venue": confirmed >= int(c.get("watch_min_confirmed_venues", 2)),
            "not_one_print": checks["not_one_print"],
            "wallet_not_hostile": checks["wallet_not_hostile"],
            "premove": has_data and pm >= float(c.get("watch_min_premove", 70.0)),
        }
        watch = bool(c.get("enabled", True)) and all(gate.values())

        reasons: List[Tuple[float, str]] = []
        if orderbook:
            reasons.append((orderbook, f"order-book {orderbook:.0f}"))
        if buy:
            reasons.append((buy, f"buy pressure {buy:.0f}"))
        if cross:
            reasons.append((cross, f"cross-venue {cross:.0f}"))
        if confirmed:
            reasons.append((60.0 + 10.0 * confirmed, f"{confirmed}/{coverage_total or coverage} venues confirm"))
        thin = float(agg.get("ask_ratio") or 1.0)
        if thin < 0.9:
            reasons.append((100.0 * (1.0 - thin) + 40.0, f"ask depth {100.0 * (thin - 1.0):+.0f}% vs normal"))
        if agg.get("vol_ratio") is not None and float(agg["vol_ratio"]) >= 1.25:
            reasons.append((min(99.0, 30.0 * float(agg["vol_ratio"])), f"volume {float(agg['vol_ratio']):.1f}x normal"))
        if wallet["status"] == "supportive":
            reasons.append((wallet["score"] or 50.0, "labelled holders accumulating (wallet supportive)"))
        reasons.sort(key=lambda x: -x[0])
        missing = [CHECK_TEXT.get(k, k) for k, ok in checks.items() if not ok]
        if not checks["premove"] and has_data:
            missing[missing.index(CHECK_TEXT["premove"])] = f"pre-move {pm:.0f} < {min_pm:.0f}"
        if not checks["venues"]:
            missing[missing.index(CHECK_TEXT["venues"])] = \
                f"{confirmed} confirming venues (needs {int(c.get('min_confirmed_venues', 3))})"

        return {
            "strict": strict, "watch": watch, "checks": checks, "gate": gate, "missing": missing,
            "threshold": min_pm, "evidence_score": rnd(evidence, 1), "structure_score": rnd(structure, 1),
            "execution_score": rnd(execution, 1), "wallet": wallet, "reasons": [t for _, t in reasons][:6],
            "has_data": has_data,
        }

    # ------------------------------------------------------------------ state machine
    def _summary(self, res: Dict[str, Any], a: Dict[str, Any], now: float) -> Dict[str, Any]:
        return {"asset": res["asset"], "ts": now, "evidence_score": a["evidence_score"], "premove": res.get("premove"),
                "price": res.get("price"), "confirmed": res.get("confirmed"), "coverage": res.get("coverage"),
                "coverage_total": res.get("coverage_total"), "confirmed_venues": list(res.get("confirmed_venues") or []),
                "reasons": a["reasons"][:4], "missing": a["missing"][:4], "threshold": a["threshold"],
                "wallet": {k: a["wallet"].get(k) for k in ("status", "state", "label", "reason", "supportive",
                                                           "hostile", "reshuffle_not_counted")},
                "late_state": (res.get("late") or {}).get("state"), "strict": a["strict"], "watch": a["watch"]}

    def update(self, res: Dict[str, Any], now: float) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], bool]:
        """Advance one asset. Return (active_alert, emitted_events, fired_now)."""
        asset = res["asset"]
        st = self._st(asset)
        a = self.assess(res)
        st["last"] = self._summary(res, a, now)
        st["seen"] = now
        events: List[Dict[str, Any]] = []
        fired_now = False
        persist_s = float(self.cfg.get("persistence_seconds", 120.0))
        clear_s = float(self.cfg.get("clear_after_seconds", 60.0))

        # WATCH bookkeeping with a short linger so the radar does not flicker
        if a["watch"] or a["strict"]:
            st["watch_since"] = st["watch_since"] or now
            st["watch_seen"] = now
        elif st["watch_since"] is not None and now - (st["watch_seen"] or now) > float(self.cfg.get("watch_linger_seconds", 20.0)):
            st["watch_since"] = None

        if a["strict"]:
            st["bad_since"] = None
            if st["candidate_since"] is None:
                st["candidate_since"] = now
            if st["active"] is None and now - st["candidate_since"] >= persist_s:
                self._seq += 1
                alert = {
                    "id": f"{asset}-{int(st['candidate_since'])}-{self._seq}",
                    "asset": asset, "state": "HIGH_CONVICTION",
                    "started_ts": st["candidate_since"], "fired_ts": now, "updated_ts": now, "ended_ts": None,
                    "duration_s": now - st["candidate_since"],
                    "evidence_score": a["evidence_score"], "peak_evidence": a["evidence_score"],
                    "premove": res.get("premove"), "peak_premove": res.get("premove"),
                    "price": res.get("price"), "price_at_fire": res.get("price"), "price_at_end": None,
                    "confirmed": res.get("confirmed"), "coverage": res.get("coverage"),
                    "coverage_total": res.get("coverage_total"),
                    "confirmed_venues": list(res.get("confirmed_venues") or []),
                    "structure_score": a["structure_score"], "execution_score": a["execution_score"],
                    "wallet_status": a["wallet"]["status"], "wallet_state": a["wallet"]["state"],
                    "reasons": list(a["reasons"][:4]), "end_reason": None, "checks": dict(a["checks"]),
                }
                st["active"] = alert
                st["invalidated"] = None
                fired_now = True
                self._mark(alert, now)
                events.append({
                    "asset": asset, "ts": now, "category": "ALERT", "event_type": "high_conviction_buy_setup",
                    "severity": 3, "level": a["evidence_score"],
                    "message": f"HIGH-CONVICTION BUY SETUP · evidence {a['evidence_score']:.0f}/100 · "
                               + " · ".join(a["reasons"][:4]),
                    "evidence": {"alert": dict(alert), "checks": a["checks"],
                                 "note": "Evidence score is not a probability or proof of a purchase."},
                })
            elif st["active"] is not None:
                self._refresh(st["active"], res, a, now)
        else:
            if st["active"] is None:
                st["candidate_since"] = None      # a candidate that never fired resets immediately
            else:
                end = self._end_reason(res, a, st, now, clear_s)
                if end is not None:
                    events.append(self._invalidate(st, res, a, now, end))
                else:
                    self._refresh(st["active"], res, a, now, strict=False)
        active = st.get("active")
        return (dict(active) if active else None), events, fired_now

    def _refresh(self, al: Dict[str, Any], res: Dict[str, Any], a: Dict[str, Any], now: float, strict: bool = True) -> None:
        al["updated_ts"] = now
        al["duration_s"] = now - al["started_ts"]
        al["price"] = res.get("price")
        if strict:
            al["evidence_score"] = a["evidence_score"]
            al["peak_evidence"] = max(float(al.get("peak_evidence") or 0.0), float(a["evidence_score"] or 0.0))
            al["premove"] = res.get("premove")
            al["peak_premove"] = max(float(al.get("peak_premove") or 0.0), float(res.get("premove") or 0.0))
            al["confirmed"] = res.get("confirmed")
            al["coverage"] = res.get("coverage")
            al["coverage_total"] = res.get("coverage_total")
            al["confirmed_venues"] = list(res.get("confirmed_venues") or [])
            al["structure_score"] = a["structure_score"]
            al["execution_score"] = a["execution_score"]
            al["wallet_status"] = a["wallet"]["status"]
            al["wallet_state"] = a["wallet"]["state"]
            al["reasons"] = list(a["reasons"][:4])
        al["dipping"] = not strict
        if now - float(al.get("_persisted_ts") or 0.0) >= float(self.cfg.get("persist_update_seconds", 30.0)):
            self._mark(al, now)

    def _end_reason(self, res: Dict[str, Any], a: Dict[str, Any], st: Dict[str, Any], now: float,
                    clear_s: float) -> Optional[str]:
        al = st["active"]
        late_state = (res.get("late") or {}).get("state")
        p0, p1 = al.get("price_at_fire"), res.get("price")
        move = (100.0 * (float(p1) / float(p0) - 1.0)) if (p0 and p1) else None
        # Hard invalidation: the move has started or the wallet flow turned hostile.
        if late_state in MOVE_STATES:
            where = f" ({move:+.1f}% since fire)" if move is not None else ""
            return f"price no longer flat: {late_state.lower().replace('_', ' ')}{where} - no longer a pre-move entry"
        if a["wallet"]["status"] == "hostile":
            return "hostile wallet / exchange flow: " + ", ".join(a["wallet"]["hostile"])
        if st["bad_since"] is None:
            st["bad_since"] = now
        if now - st["bad_since"] < clear_s:
            return None                               # hysteresis: survive a brief dip
        if not a["checks"]["feed_complete"] or not a["has_data"]:
            cov, tot = res.get("coverage") or 0, res.get("coverage_total") or 0
            return f"feeds stale / incomplete ({cov}/{tot} selected venues live) for {now - st['bad_since']:.0f}s"
        return "composite confirmation faded: " + "; ".join(a["missing"][:3])

    def _invalidate(self, st: Dict[str, Any], res: Dict[str, Any], a: Dict[str, Any], now: float,
                    reason: str) -> Dict[str, Any]:
        al = st["active"]
        al["state"] = "INVALIDATED"
        al["updated_ts"] = now
        al["ended_ts"] = now
        al["duration_s"] = now - al["started_ts"]
        al["price"] = res.get("price")
        al["price_at_end"] = res.get("price")
        al["end_reason"] = reason
        al["dipping"] = False
        self._mark(al, now)
        st["invalidated"] = dict(al)
        st["active"] = None
        st["candidate_since"] = None
        st["bad_since"] = None
        return {"asset": al["asset"], "ts": now, "category": "ALERT", "event_type": "high_conviction_cleared",
                "severity": 2, "level": al.get("evidence_score"),
                "message": f"High-conviction setup INVALIDATED: {reason}",
                "evidence": {"alert": dict(al), "checks": a["checks"]}}

    def sweep(self, now: float, live_assets: Optional[set] = None) -> List[Dict[str, Any]]:
        """Assets that stopped updating (feed loss, left the universe) cannot keep an alert."""
        events: List[Dict[str, Any]] = []
        stale_s = float(self.cfg.get("clear_after_seconds", 60.0))
        for asset, st in self.state.items():
            gone = live_assets is not None and asset not in live_assets
            if st.get("active") and (gone or now - float(st.get("seen") or now) >= stale_s):
                res = {"asset": asset, "price": st["active"].get("price"),
                       "coverage": 0, "coverage_total": st["active"].get("coverage_total")}
                a = {"checks": {"feed_complete": False}, "has_data": False, "missing": []}
                why = "asset left the scanner universe" if gone else f"no fresh data for {now - float(st.get('seen') or now):.0f}s"
                events.append(self._invalidate(st, res, a, now, f"feeds stale / incomplete: {why}"))
            if gone or (st.get("seen") and now - float(st["seen"]) >= stale_s):
                st["candidate_since"] = None
                st["watch_since"] = None
        return events

    def _mark(self, al: Dict[str, Any], now: float) -> None:
        al["_persisted_ts"] = now
        self._changed[al["id"]] = {k: v for k, v in al.items() if not k.startswith("_")}

    def pop_changes(self) -> List[Dict[str, Any]]:
        """Alert rows that fired / ended / changed since the last call (for SQLite persistence)."""
        rows = list(self._changed.values())
        self._changed.clear()
        return rows

    # ------------------------------------------------------------------ views
    def active(self) -> List[Dict[str, Any]]:
        rows = [self._public(st["active"]) for st in self.state.values() if st.get("active")]
        rows.sort(key=lambda x: (-(x.get("evidence_score") or 0.0), x.get("fired_ts") or 0.0))
        return rows

    def get(self, asset: str) -> Optional[Dict[str, Any]]:
        st = self.state.get(asset.upper())
        return self._public(st["active"]) if st and st.get("active") else None

    @staticmethod
    def _public(al: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in al.items() if not k.startswith("_")}

    def radar(self, now: float, wallet_summary: Optional[Dict[str, Any]] = None,
              feeds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The always-visible radar: one headline state plus the other live entries."""
        persist_s = float(self.cfg.get("persistence_seconds", 120.0))
        inv_show = float(self.cfg.get("invalidated_display_seconds", 900.0))
        inv_head = float(self.cfg.get("invalidated_headline_seconds", 600.0))
        fresh_s = float(self.cfg.get("clear_after_seconds", 60.0))
        entries: List[Dict[str, Any]] = []
        for asset, st in self.state.items():
            last = st.get("last") or {}
            fresh = st.get("seen") is not None and now - float(st["seen"]) < fresh_s
            if st.get("active"):
                al = st["active"]
                entries.append({**self._entry_base(last, al), "state": "HIGH_CONVICTION",
                                "persistence_s": now - al["started_ts"], "fired_ts": al["fired_ts"],
                                "evidence_score": al.get("evidence_score"), "reasons": al.get("reasons") or [],
                                "confirmed": al.get("confirmed"), "coverage": al.get("coverage"),
                                "coverage_total": al.get("coverage_total"),
                                "confirmed_venues": al.get("confirmed_venues") or [],
                                "dipping": bool(al.get("dipping"))})
            elif fresh and st.get("candidate_since") is not None:
                entries.append({**self._entry_base(last), "state": "CONFIRMING",
                                "persistence_s": now - st["candidate_since"], "persistence_required_s": persist_s})
            elif fresh and st.get("watch_since") is not None:
                entries.append({**self._entry_base(last), "state": "WATCH",
                                "persistence_s": now - st["watch_since"], "persistence_required_s": persist_s})
            inv = st.get("invalidated")
            if inv and now - float(inv.get("ended_ts") or 0.0) <= inv_show:
                entries.append({**self._entry_base(last, inv), "state": "INVALIDATED",
                                "persistence_s": inv.get("duration_s"), "fired_ts": inv.get("fired_ts"),
                                "ended_ts": inv.get("ended_ts"), "end_reason": inv.get("end_reason"),
                                "evidence_score": inv.get("peak_evidence"), "reasons": inv.get("reasons") or [],
                                "confirmed": inv.get("confirmed"), "coverage": inv.get("coverage"),
                                "coverage_total": inv.get("coverage_total"),
                                "confirmed_venues": inv.get("confirmed_venues") or [],
                                "price_change_pct": _pct(inv.get("price_at_fire"), inv.get("price_at_end"))})
        rank = {"HIGH_CONVICTION": 0, "INVALIDATED": 1, "CONFIRMING": 2, "WATCH": 3}

        def key(e):
            if e["state"] == "INVALIDATED":
                return (rank["INVALIDATED"] if now - float(e.get("ended_ts") or 0) <= inv_head else 4, -float(e.get("ended_ts") or 0))
            if e["state"] == "CONFIRMING":
                return (rank["CONFIRMING"], -float(e.get("persistence_s") or 0))
            return (rank[e["state"]], -float(e.get("evidence_score") or 0))
        entries.sort(key=key)
        head = entries[0] if entries else None
        state = head["state"] if head else "NONE"
        if head is not None and head["state"] == "INVALIDATED" and key(head)[0] == 4:
            state = "NONE"                              # old invalidation: listed, not the headline
        return {"ts": now, "state": state, "label": LABELS[state], "primary": head if state != "NONE" else None,
                "entries": entries[:8], "wallet": wallet_summary or {}, "feeds": feeds or {},
                "persistence_seconds": persist_s,
                "note": "Evidence score is a composite strength, not a probability or proof of a purchase."}

    @staticmethod
    def _entry_base(last: Dict[str, Any], al: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        src = al or last
        return {"asset": src.get("asset") or last.get("asset"), "label": None,
                "evidence_score": last.get("evidence_score"), "premove": last.get("premove"),
                "price": last.get("price"), "confirmed": last.get("confirmed"), "coverage": last.get("coverage"),
                "coverage_total": last.get("coverage_total"), "confirmed_venues": last.get("confirmed_venues") or [],
                "reasons": last.get("reasons") or [], "missing": last.get("missing") or [],
                "threshold": last.get("threshold"), "wallet": last.get("wallet") or {}}


def _pct(p0: Any, p1: Any) -> Optional[float]:
    try:
        return rnd(100.0 * (float(p1) / float(p0) - 1.0), 2) if p0 and p1 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
