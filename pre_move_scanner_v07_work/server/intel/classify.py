"""Conservative transfer classification.

  BUY                swap: labelled DEX pool/router -> address (token received from a swap)
  SELL               swap: address -> labelled DEX pool/router
  ACCUMULATION-SIDE  exchange -> labelled whale / institutional custody / treasury
  DISTRIBUTION-SIDE  labelled whale / custody / treasury -> exchange
  SHIFT              exchange -> exchange (incl. same-entity hot/cold/custody moves,
                     e.g. Coinbase Hot -> Coinbase Prime), MM <-> exchange,
                     same-entity internal moves, bridges
  UNKNOWN            attribution insufficient (unlabelled side, LOW-confidence or
                     WATCH label, MM <-> holder (possible OTC), etc.)

Deliberately NOT inferred: a CEX withdrawal is not a purchase; Coinbase Hot ->
Coinbase Prime is custody/internal routing, not an institution buying.
"""
from __future__ import annotations

from typing import Optional, Tuple

from .labels import CEX_TYPES, DEX_TYPES, HOLDER_TYPES, Label

BUY = "BUY"
SELL = "SELL"
ACC = "ACCUMULATION-SIDE"
DIST = "DISTRIBUTION-SIDE"
SHIFT = "SHIFT"
UNKNOWN = "UNKNOWN"
CLASSES = (BUY, SELL, ACC, DIST, SHIFT, UNKNOWN)


def _usable(l: Optional[Label]) -> Optional[Label]:
    return l if (l is not None and l.reliable) else None


def _name(l: Optional[Label], addr: str) -> str:
    return f"{l.entity} ({l.entity_type})" if l else f"unlabelled {addr[:10]}…"


def classify_transfer(frm: Optional[Label], to: Optional[Label], from_addr: str = "", to_addr: str = ""
                      ) -> Tuple[str, str, str]:
    """Return (classification, confidence, explanation)."""
    f, t = _usable(frm), _usable(to)
    route = f"{_name(f, from_addr)} → {_name(t, to_addr)}"
    if frm is not None and frm.entity_type == "BURN":
        return UNKNOWN, "HIGH", f"mint ({route})"
    if to is not None and to.entity_type == "BURN":
        return UNKNOWN, "HIGH", f"burn ({route})"
    if f is None and t is None:
        note = ""
        if (frm and not frm.reliable) or (to and not to.reliable):
            note = " (label present but LOW confidence / watch-only)"
        return UNKNOWN, "LOW", f"no reliable attribution: {route}{note}"
    ft = f.entity_type if f else None
    tt = t.entity_type if t else None

    if ft in DEX_TYPES and tt not in DEX_TYPES:
        return BUY, "MEDIUM", f"received from DEX pool/router (swap into token): {route}"
    if tt in DEX_TYPES and ft not in DEX_TYPES:
        return SELL, "MEDIUM", f"sent to DEX pool/router (swap out of token): {route}"
    if "BRIDGE" in (ft, tt):
        return SHIFT, "MEDIUM", f"bridge transfer (cross-chain move): {route}"
    if f and t and f.entity.strip().lower() == t.entity.strip().lower():
        return SHIFT, "HIGH", f"same-entity internal move: {route}"
    if ft in CEX_TYPES and tt in CEX_TYPES:
        return SHIFT, "HIGH", f"exchange ↔ exchange / custody routing: {route}"
    if (ft == "MM" and tt in CEX_TYPES) or (ft in CEX_TYPES and tt == "MM"):
        return SHIFT, "HIGH", f"market-maker inventory routing: {route}"
    if ft in CEX_TYPES and tt in HOLDER_TYPES:
        return ACC, "MEDIUM", f"exchange → holder/custody (withdrawal, not proof of a purchase): {route}"
    if ft in HOLDER_TYPES and tt in CEX_TYPES:
        return DIST, "MEDIUM", f"holder/custody → exchange (deposit, not proof of a sale): {route}"
    if (ft == "MM" and tt in HOLDER_TYPES) or (ft in HOLDER_TYPES and tt == "MM"):
        return UNKNOWN, "LOW", f"MM ↔ holder (possible OTC settlement; direction not inferred): {route}"
    if ft in HOLDER_TYPES and tt in HOLDER_TYPES:
        return SHIFT, "MEDIUM", f"holder/custody reorganisation: {route}"
    return UNKNOWN, "LOW", f"one side unattributed: {route}"
