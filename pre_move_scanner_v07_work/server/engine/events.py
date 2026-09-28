"""Event detection for the chronological timeline.

All events are debounced or hysteretic, so 100 coins do not produce an event
storm (v0.6 logged every 1↔2 flicker of the confirmed-venue count). When price
breaks out, the detector looks back over the asset's recent events and
attaches the *precursors* — what changed before the move, and how long before.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

SEVERITY = {"STRONG PRE-MOVE": 3, "CONFIRMED PRE-MOVE": 2, "EMERGING": 2, "LATE": 2,
            "MOVE IN PROGRESS": 1, "WATCH": 1}
PRECURSOR_CATEGORIES = ("BOOK", "FLOW", "VENUE", "STATUS", "SCORE", "WALLET", "ALERT")


def _fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.0f}%"


def onset_message(t: Dict[str, Any]) -> (str, str):
    d = t.get("detail") or {}
    v = t["venue"]
    fam = t["family"]
    if fam == "thinning":
        r = d.get("ask1_ratio")
        return "BOOK", f"Ask depth {_fmt_pct(r - 1 if r else None)} vs normal on {v}"
    if fam == "no_replenish":
        rf = d.get("ask_refill")
        extra = f" (refill after fills {rf:.2f})" if rf is not None else ""
        return "BOOK", f"Weak ask replenishment on {v}{extra}"
    if fam == "buy_flow":
        bs, bb = d.get("buy_share"), d.get("buy_share_base")
        if bs is not None and bb is not None:
            return "FLOW", f"Aggressive buys {bs:.0%} on {v} (normal {bb:.0%})"
        return "FLOW", f"Aggressive buying on {v}"
    if fam == "volume":
        vr = d.get("vol_ratio")
        return "FLOW", f"Volume {vr:.1f}× normal on {v}" if vr else f"Volume rising on {v}"
    if fam == "bid_support":
        return "BOOK", f"Bids steady while asks weaken on {v}"
    return "BOOK", f"{fam} on {v}"


class EventDetector:
    def __init__(self, ecfg: Dict[str, Any]):
        self.cfg = ecfg
        self.mem: Dict[str, Deque[Dict[str, Any]]] = {}
        self.st: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def _state(self, asset: str) -> Dict[str, Any]:
        return self.st.setdefault(asset, {
            "armed": {int(t): True for t in self.cfg["score_thresholds"]},
            "conf_emitted": None, "conf_cand": None, "conf_since": None,
            "breakout_armed": True, "breakdown_armed": True,
            "last_status_ts": 0.0, "fam_counts": {},
        })

    def _emit(self, out: List[Dict[str, Any]], asset: str, ts: float, category: str, etype: str,
              message: str, severity: int = 1, venue: Optional[str] = None, level: Optional[float] = None,
              evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._seq += 1
        ev = {"id": self._seq, "ts": ts, "asset": asset, "category": category, "event_type": etype,
              "severity": severity, "venue": venue, "level": level, "message": message,
              "evidence": evidence or {}}
        dq = self.mem.setdefault(asset, deque(maxlen=int(self.cfg.get("memory_per_asset", 300))))
        dq.append(ev)
        out.append(ev)
        return ev

    def add_external(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        """Register an event produced elsewhere (e.g. wallet intelligence)."""
        out: List[Dict[str, Any]] = []
        return self._emit(out, ev["asset"], ev["ts"], ev.get("category", "WALLET"), ev["event_type"],
                          ev["message"], ev.get("severity", 1), ev.get("venue"), ev.get("level"),
                          ev.get("evidence"))

    def recent(self, asset: str, since: float = 0.0) -> List[Dict[str, Any]]:
        return [e for e in self.mem.get(asset, ()) if e["ts"] >= since]

    def process(self, res: Dict[str, Any]) -> List[Dict[str, Any]]:
        asset, now = res["asset"], res["ts"]
        st = self._state(asset)
        out: List[Dict[str, Any]] = []
        score = res.get("premove")

        for t in res.get("transitions", []):
            k = t["kind"]
            if k == "status":
                to, frm = t["to"], t["from"]
                sev = SEVERITY.get(to, 0)
                escalation = SEVERITY.get(to, 0) > SEVERITY.get(frm, 0)
                gap_ok = now - st["last_status_ts"] >= float(self.cfg["status_min_gap_seconds"])
                if to in ("NO DATA", "WARMING") or frm in ("NO DATA",):
                    continue
                if escalation or gap_ok:
                    msg = f"Status {frm} → {to}"
                    if score is not None and to in SEVERITY:
                        msg += f" (Pre-Move {score:.0f})"
                    self._emit(out, asset, now, "STATUS", "status_change", msg, sev, level=score,
                               evidence={"from": frm, "to": to, "reason": res.get("reason")})
                    st["last_status_ts"] = now
            elif k == "onset":
                cat, msg = onset_message(t)
                self._emit(out, asset, t["ts"], cat, f"{t['family']}_onset", msg, 1, venue=t["venue"],
                           level=t.get("strength"), evidence=t.get("detail"))
            elif k == "venue_state":
                to, frm = t["to"], t["from"]
                if to in ("STALE", "DISCONNECTED", "UNAVAILABLE"):
                    self._emit(out, asset, now, "SYSTEM", "venue_" + to.lower(),
                               f"{t['venue']} {to.lower()}: {t.get('reason', '')}".strip(": "), 1, venue=t["venue"])
                elif frm in ("STALE", "DISCONNECTED") and to in ("LIVE", "RESYNCING", "WARMING"):
                    self._emit(out, asset, now, "SYSTEM", "venue_recovered", f"{t['venue']} recovered ({to.lower()})",
                               0, venue=t["venue"])
            elif k == "warm":
                self._emit(out, asset, now, "SYSTEM", "warmup_complete", "Baselines established (warm-up complete)", 0)

        # cross-venue family counts (e.g. "3/5 venues confirm thinning")
        counts: Dict[str, int] = {}
        for v in res.get("venues", []):
            for fam in v.get("active_families", []):
                counts[fam] = counts.get(fam, 0) + 1
        n_live = res.get("coverage") or 0
        for fam, n in counts.items():
            prev = st["fam_counts"].get(fam, 0)
            if n >= 2 and n > prev:
                label = {"thinning": "ask thinning", "no_replenish": "weak ask replenishment",
                         "buy_flow": "aggressive buying", "volume": "volume acceleration",
                         "bid_support": "bid support"}.get(fam, fam)
                self._emit(out, asset, now, "VENUE", "cross_venue_" + fam,
                           f"{n}/{n_live} venues confirm {label}", 2 if n >= 3 else 1, level=n)
        st["fam_counts"] = counts

        # score threshold crossings with hysteresis (v0.6 event types kept)
        if score is not None:
            hyst = float(self.cfg["score_hysteresis"])
            for thr in sorted(st["armed"]):
                if st["armed"][thr] and score >= thr:
                    st["armed"][thr] = False
                    self._emit(out, asset, now, "SCORE", "score_cross_up", f"Pre-Move score crossed above {thr}",
                               2 if thr >= 70 else 1, level=thr)
                elif not st["armed"][thr] and score < thr - hyst:
                    st["armed"][thr] = True
                    self._emit(out, asset, now, "SCORE", "score_cross_down", f"Pre-Move score fell below {thr}",
                               0, level=thr)

        # confirmed-venue count, debounced
        conf = res.get("confirmed")
        if conf is not None:
            if conf != st["conf_cand"]:
                st["conf_cand"], st["conf_since"] = conf, now
            elif (now - st["conf_since"] >= float(self.cfg["confirm_debounce_seconds"])
                  and conf != st["conf_emitted"]):
                if st["conf_emitted"] is not None:
                    self._emit(out, asset, now, "VENUE", "venue_confirmations",
                               f"Confirmed venues {st['conf_emitted']} → {conf}", 1, level=conf)
                st["conf_emitted"] = conf

        # price breakout / breakdown with precursors
        rets = res.get("returns") or {}
        r15, r60 = rets.get(15), rets.get(60)
        bp = self.cfg["breakout_pct"]
        up = (r15 is not None and r15 >= float(bp["15"])) or (r60 is not None and r60 >= float(bp["60"]))
        if up and st["breakout_armed"]:
            st["breakout_armed"] = False
            pre = self.precursors(asset, now)
            desc = f"{r15:+.1f}% (15m)" if (r15 is not None and r15 >= float(bp["15"])) else f"{r60:+.1f}% (1h)"
            msg = f"Price breakout {desc}"
            if pre:
                msg += ". Preceded by: " + "; ".join(f"{p['message']} ({p['minutes_before']} min before)" for p in pre[:4])
            self._emit(out, asset, now, "PRICE", "price_breakout", msg, 2, level=r15,
                       evidence={"precursors": pre, "r15": r15, "r60": r60})
        elif not st["breakout_armed"] and (r15 is None or r15 < float(bp["15"]) / 2) and (r60 is None or r60 < float(bp["60"]) / 2):
            st["breakout_armed"] = True
        down = (r15 is not None and r15 <= -float(bp["15"])) or (r60 is not None and r60 <= -float(bp["60"]))
        if down and st["breakdown_armed"]:
            st["breakdown_armed"] = False
            self._emit(out, asset, now, "PRICE", "price_breakdown",
                       f"Price breakdown {r15 if r15 is not None else r60:+.1f}%", 1, level=r15)
        elif not st["breakdown_armed"] and (r15 is None or r15 > -float(bp["15"]) / 2):
            st["breakdown_armed"] = True
        return out

    def precursors(self, asset: str, now: float) -> List[Dict[str, Any]]:
        look = float(self.cfg["precursor_lookback_minutes"]) * 60.0
        pre = []
        seen = set()
        for e in self.mem.get(asset, ()):
            if e["category"] not in PRECURSOR_CATEGORIES or now - e["ts"] > look or e["ts"] > now:
                continue
            if e["event_type"] == "score_cross_down":
                continue
            key = (e["event_type"], e.get("venue"))
            if key in seen:
                continue
            seen.add(key)
            pre.append({"id": e["id"], "ts": e["ts"], "category": e["category"], "event_type": e["event_type"],
                        "message": e["message"], "minutes_before": round((now - e["ts"]) / 60.0)})
        pre.sort(key=lambda p: p["ts"])
        return pre
