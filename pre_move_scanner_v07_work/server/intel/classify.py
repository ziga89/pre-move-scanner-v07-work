"""Conservative transfer classification into the normalised event types (v0.8).

  CEX_IN            unattributed address -> exchange (deposit; not proof of a sale)
  CEX_OUT           exchange -> unattributed address (withdrawal; NOT proof of a purchase)
  ACCUMULATION      exchange -> labelled whale / treasury (accumulation-side evidence)
  DISTRIBUTION      labelled whale / treasury -> exchange (distribution-side evidence)
  INTERNAL_SHIFT    same-entity move, or exchange <-> exchange routing
  MM_ROUTING        market maker <-> exchange / market maker
  CUSTODY_SHIFT     any move into / out of exchange custody or institutional custody
                    (e.g. Coinbase Hot -> Coinbase Prime) - never buying
  BRIDGE            either side a bridge (cross-chain move)
  DEX_FLOW          one side a DEX pool / router (swap; direction kept, not counted as accumulation)
  UNKNOWN_TRANSFER  attribution insufficient (unlabelled sides, LOW / WATCH / WHALE_CANDIDATE labels,
                    market maker <-> holder (possible OTC), holder <-> holder, mint / burn)

Only ACCUMULATION / DISTRIBUTION carry directional holder evidence. Internal exchange moves,
market-maker routing and custody transfers can never become buying evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .labels import CEX_TYPES, CUSTODY_TYPES, DEX_TYPES, HOLDER_TYPES, MM_TYPES, Label

CEX_IN = "CEX_IN"
CEX_OUT = "CEX_OUT"
ACCUMULATION = "ACCUMULATION"
DISTRIBUTION = "DISTRIBUTION"
INTERNAL_SHIFT = "INTERNAL_SHIFT"
MM_ROUTING = "MM_ROUTING"
CUSTODY_SHIFT = "CUSTODY_SHIFT"
BRIDGE = "BRIDGE"
DEX_FLOW = "DEX_FLOW"
UNKNOWN_TRANSFER = "UNKNOWN_TRANSFER"
EVENT_TYPES = (CEX_IN, CEX_OUT, ACCUMULATION, DISTRIBUTION, INTERNAL_SHIFT, MM_ROUTING, CUSTODY_SHIFT, BRIDGE,
               DEX_FLOW, UNKNOWN_TRANSFER)
# Reserve-neutral routing: excluded from CEX net flow and never counted as buying or selling.
ROUTING_TYPES = {INTERNAL_SHIFT, MM_ROUTING, CUSTODY_SHIFT, BRIDGE}

# v0.7 class names (rows stored by v0.7.x): reclassified from the stored labels when loaded.
LEGACY_CLASSES = {"BUY", "SELL", "ACCUMULATION-SIDE", "DISTRIBUTION-SIDE", "SHIFT", "UNKNOWN"}


@dataclass
class Classification:
    event_type: str
    confidence: str                  # classification confidence
    explanation: str
    attribution: str                 # HIGH / MEDIUM / LOW / NONE
    entity_type: str                 # the attributed entity type that defines the event
    direction: Optional[str] = None  # DEX_FLOW: "into_token" / "out_of_token"

    def astuple(self) -> Tuple[str, str, str]:
        return self.event_type, self.confidence, self.explanation


def _usable(l: Optional[Label]) -> Optional[Label]:
    return l if (l is not None and l.reliable) else None


def _name(l: Optional[Label], addr: str) -> str:
    return f"{l.entity} ({l.entity_type})" if l else f"unlabelled {addr[:10]}…"


def _attribution(f: Optional[Label], t: Optional[Label]) -> str:
    if f and t:
        return "HIGH" if f.confidence == "HIGH" and t.confidence == "HIGH" else "MEDIUM"
    return "LOW" if (f or t) else "NONE"


def classify(frm: Optional[Label], to: Optional[Label], from_addr: str = "", to_addr: str = "") -> Classification:
    f, t = _usable(frm), _usable(to)
    route = f"{_name(f, from_addr)} → {_name(t, to_addr)}"
    att = _attribution(f, t)
    if frm is not None and frm.entity_type == "BURN":
        return Classification(UNKNOWN_TRANSFER, "HIGH", f"mint ({route})", att, "BURN")
    if to is not None and to.entity_type == "BURN":
        return Classification(UNKNOWN_TRANSFER, "HIGH", f"burn ({route})", att, "BURN")
    if f is None and t is None:
        note = ""
        if (frm and not frm.reliable) or (to and not to.reliable):
            note = " (label present but LOW confidence / watch-only / candidate)"
        return Classification(UNKNOWN_TRANSFER, "LOW", f"no reliable attribution: {route}{note}", att, "UNKNOWN")
    ft = f.entity_type if f else None
    tt = t.entity_type if t else None
    same = bool(f and t and f.entity.strip().lower() == t.entity.strip().lower())

    if "BRIDGE" in (ft, tt):
        return Classification(BRIDGE, "MEDIUM", f"bridge transfer (cross-chain move, not buying): {route}", att, "BRIDGE")
    if (ft in DEX_TYPES) != (tt in DEX_TYPES):
        into = ft in DEX_TYPES
        return Classification(DEX_FLOW, "MEDIUM",
                              f"DEX swap {'into' if into else 'out of'} the token (not counted as accumulation): {route}",
                              att, ft if into else tt, "into_token" if into else "out_of_token")
    custodial = [x for x in (ft, tt) if x in CUSTODY_TYPES]
    if custodial and f and t:
        # both sides attributed, one of them custody: a custody move, never buying (or selling)
        other = tt if ft in CUSTODY_TYPES else ft
        return Classification(CUSTODY_SHIFT, "HIGH" if same or other not in HOLDER_TYPES else "MEDIUM",
                              f"custody transfer (exchange / institutional custody; never buying): {route}", att,
                              custodial[0])
    if same:
        return Classification(INTERNAL_SHIFT, "HIGH", f"same-entity internal move: {route}", att, ft)
    if (ft in MM_TYPES and (tt in CEX_TYPES or tt in MM_TYPES)) or (tt in MM_TYPES and ft in CEX_TYPES):
        return Classification(MM_ROUTING, "HIGH", f"market-maker inventory routing (not buying): {route}", att,
                              "MARKET_MAKER")
    if ft in CEX_TYPES and tt in CEX_TYPES:
        return Classification(INTERNAL_SHIFT, "HIGH", f"exchange ↔ exchange routing (reserve shift, not buying): {route}",
                              att, ft)
    if ft in CEX_TYPES and tt in HOLDER_TYPES:
        return Classification(ACCUMULATION, "MEDIUM",
                              f"exchange → labelled holder (withdrawal; accumulation-side, not proof of a purchase): {route}",
                              att, tt)
    if ft in HOLDER_TYPES and tt in CEX_TYPES:
        return Classification(DISTRIBUTION, "MEDIUM",
                              f"labelled holder → exchange (deposit; distribution-side, not proof of a sale): {route}",
                              att, ft)
    if ft in CEX_TYPES and t is None:
        return Classification(CEX_OUT, "LOW", f"exchange withdrawal to an unattributed address (not a purchase): {route}",
                              att, ft)
    if tt in CEX_TYPES and f is None:
        return Classification(CEX_IN, "LOW", f"deposit into an exchange from an unattributed address: {route}", att, tt)
    if (ft in MM_TYPES and tt in HOLDER_TYPES) or (ft in HOLDER_TYPES and tt in MM_TYPES):
        return Classification(UNKNOWN_TRANSFER, "LOW", f"MM ↔ holder (possible OTC settlement; direction not inferred): {route}",
                              att, "MARKET_MAKER")
    if ft in HOLDER_TYPES and tt in HOLDER_TYPES:
        return Classification(UNKNOWN_TRANSFER, "LOW", f"holder ↔ holder (possible OTC; direction not inferred): {route}",
                              att, ft)
    return Classification(UNKNOWN_TRANSFER, "LOW", f"one side unattributed: {route}", att, ft or tt or "UNKNOWN")


def classify_transfer(frm: Optional[Label], to: Optional[Label], from_addr: str = "", to_addr: str = ""
                      ) -> Tuple[str, str, str]:
    """(event_type, confidence, explanation) - the v0.7 call signature."""
    return classify(frm, to, from_addr, to_addr).astuple()
