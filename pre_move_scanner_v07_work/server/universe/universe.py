"""Top-N universe with back-fill, manual assets and membership hysteresis.

Definition (v0.7 amendment 7): walk down the market-cap ranking — past rank
100 if necessary — until there are `target_size` (100) coins that are both
*eligible* (not stable / wrapped / staked / bridged / tokenised gold / config
excluded) and *usable* (at least one verified-available spot market with
enough volume). Manual assets (v0.8; v0.7 "pinned" coins such as QNT, XDC,
LINK) are monitored in addition to those 100 and never count toward them:
monitored universe = Top-N ∪ manual, deduplicated by CoinGecko id. A manual
asset that is already a Top-N member is not duplicated, and a manual asset
whose ticker belongs to a *different* Top-N coin is reported, not merged.

Manual assets are identified by CoinGecko id, never by ticker alone. A seeded
v0.7 ticker without an id is resolved only when exactly one fetched coin uses
it; an ambiguous ticker waits for your selection on the Universe page.

Hysteresis: members stay while they remain eligible+usable and their rank is
within `exit_rank_buffer` of the cutoff; a newcomer replaces an existing
member only after `entry_confirmations` consecutive refreshes (free slots are
filled immediately). This avoids subscription churn around rank ~100.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .filters import classify_exclusion


class UniverseManager:
    def __init__(self, ucfg: Dict[str, Any]):
        self.cfg = ucfg
        self.members: Dict[str, Dict[str, Any]] = {}     # coin_id -> row
        self.pending: Dict[str, int] = {}
        self.last: Dict[str, Any] = {}

    def build(self, rows: List[Dict[str, Any]], category_ids: Dict[str, Set[str]],
              usable: Callable[[Dict[str, Any]], Tuple[bool, str]],
              manual_rows: Optional[List[Dict[str, Any]]] = None, now: float = 0.0,
              manual: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        n = int(self.cfg.get("target_size", 100))
        buffer = int(self.cfg.get("exit_rank_buffer", 15))
        need = int(self.cfg.get("entry_confirmations", 2))
        pinned_syms = [s.upper() for s in (self.cfg.get("pinned_assets") or self.cfg.get("manual_assets") or [])]
        ids_override = {k.upper(): v for k, v in (self.cfg.get("coingecko_ids") or {}).items()}

        rows = sorted((r for r in rows if r.get("market_cap_rank")), key=lambda r: r["market_cap_rank"])
        seen_sym: Set[str] = set()
        eligible: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        usable_reason: Dict[str, str] = {}
        for r in rows:
            sym = str(r.get("symbol", "")).upper()
            reason = classify_exclusion(r, category_ids, self.cfg)
            if not reason and sym in seen_sym:
                reason = "duplicate ticker symbol (lower-ranked coin)"
            if reason:
                excluded.append(_brief(r, reason))
                continue
            ok, why = usable(r)
            usable_reason[r["id"]] = why
            if not ok:
                excluded.append(_brief(r, why or "no usable realtime venue"))
                continue
            seen_sym.add(sym)
            eligible.append(r)

        top = eligible[:n]
        cutoff = top[-1]["market_cap_rank"] if len(top) >= n else (top[-1]["market_cap_rank"] if top else 0)
        eligible_ids = {r["id"]: r for r in eligible}
        top_ids = {r["id"] for r in top}

        if not self.members:
            new_members = {r["id"]: r for r in top}
            self.pending.clear()
        else:
            keep = {cid: eligible_ids[cid] for cid in self.members
                    if cid in eligible_ids and eligible_ids[cid]["market_cap_rank"] <= cutoff + buffer}
            new_members = dict(keep)
            pending = {}
            for r in top:
                if r["id"] in new_members:
                    continue
                pending[r["id"]] = self.pending.get(r["id"], 0) + 1
                if len(new_members) < n:
                    new_members[r["id"]] = r
                    pending.pop(r["id"], None)
                elif pending[r["id"]] >= need:
                    outside = [m for m in new_members.values() if m["id"] not in top_ids]
                    if outside:
                        weakest = max(outside, key=lambda m: m["market_cap_rank"])
                        del new_members[weakest["id"]]
                        new_members[r["id"]] = r
                        pending.pop(r["id"], None)
            self.pending = pending
            # trim if the target shrank
            if len(new_members) > n:
                for m in sorted(new_members.values(), key=lambda m: -m["market_cap_rank"])[: len(new_members) - n]:
                    del new_members[m["id"]]
        self.members = new_members

        # manual assets (in addition to the N; never duplicated)
        extra = list(manual_rows or [])
        by_id = {r["id"]: r for r in rows + extra}
        by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for r in list(by_id.values()):
            by_sym.setdefault(str(r.get("symbol", "")).upper(), []).append(r)
        member_ids = set(self.members)
        member_by_sym = {str(r.get("symbol", "")).upper(): r for r in self.members.values()}
        if manual is None:          # v0.7 call: symbols from the config, ids from coingecko_ids
            manual = [{"symbol": s, "coingecko_id": ids_override.get(s), "source": "config"} for s in pinned_syms]
        out_manual: List[Dict[str, Any]] = []
        for m in manual:
            sym = str(m.get("symbol", "")).upper()
            cid = m.get("coingecko_id")
            r = by_id.get(cid) if cid else None
            if r is not None:
                resolved = False
            elif cid and m.get("source") != "config":
                out_manual.append({"symbol": sym, "id": cid, "manual": True, "usable": False,
                                   "status": f"CoinGecko id '{cid}' not found"})
                continue
            else:       # seeded from the config without a (valid) id: unique ticker only, never a guess
                cands = by_sym.get(sym, [])
                if len(cands) != 1:
                    why = (f"ambiguous ticker: {len(cands)} CoinGecko coins use {sym} - select one on the Universe page"
                           if cands else f"{sym} not found in the CoinGecko ranking - search and add it on the Universe page")
                    out_manual.append({"symbol": sym, "id": None, "manual": True, "usable": False, "status": why,
                                       "candidates": [{"id": c["id"], "name": c.get("name"),
                                                       "rank": c.get("market_cap_rank")} for c in cands[:8]]})
                    continue
                r, resolved = cands[0], True
            rsym = str(r.get("symbol", "")).upper()
            base = {**r, "symbol": rsym, "manual": True, "pinned": True, "resolved_by_ticker": resolved}
            if r["id"] in member_ids:
                out_manual.append({**base, "usable": True, "in_top": True, "status": "in the Top-100 (not duplicated)"})
                continue
            clash = member_by_sym.get(rsym)
            if clash is not None:
                out_manual.append({**base, "usable": False, "in_top": False,
                                   "status": f"ticker {rsym} is already used by Top-100 member {clash.get('name')} "
                                             f"({clash['id']}); not monitored separately"})
                continue
            ok, why = usable(r)
            out_manual.append({**base, "usable": ok, "in_top": False, "status": "ok" if ok else (why or "no usable venue")})

        members = sorted(self.members.values(), key=lambda r: r["market_cap_rank"])
        self.last = {
            "ts": now, "target": n, "members": members, "manual": out_manual,
            "pinned": [m for m in out_manual if not m.get("in_top")],          # v0.7 view: extras only
            "excluded": excluded,
            "eligible_count": len(eligible), "cutoff_rank": cutoff, "deepest_rank_scanned": rows[-1]["market_cap_rank"] if rows else None,
            "short_by": max(0, n - len(members)), "pending": dict(self.pending),
        }
        return self.last


def _brief(r: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {"id": r.get("id"), "symbol": str(r.get("symbol", "")).upper(), "name": r.get("name"),
            "rank": r.get("market_cap_rank"), "market_cap": r.get("market_cap"), "reason": reason}
