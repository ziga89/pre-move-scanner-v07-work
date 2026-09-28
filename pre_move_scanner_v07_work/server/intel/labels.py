"""Wallet label registry.

Labels come from `labels/wallet_labels.csv` (you edit it), v0.6 config
watch-wallets, and anything stored in the DB. Nothing is inferred from memory
or scraped. Each label carries a confidence; LOW-confidence or WATCH labels
are displayed but never drive classification or scores.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ENTITY_TYPES = {
    "CEX_HOT", "CEX_COLD", "CEX_DEPOSIT", "CEX_CUSTODY", "MM", "WHALE", "CUSTODY_INSTITUTIONAL",
    "PROTOCOL_TREASURY", "DEX_POOL", "DEX_ROUTER", "BRIDGE", "BURN", "WATCH",
}
CEX_TYPES = {"CEX_HOT", "CEX_COLD", "CEX_DEPOSIT", "CEX_CUSTODY"}
HOLDER_TYPES = {"WHALE", "CUSTODY_INSTITUTIONAL", "PROTOCOL_TREASURY"}
DEX_TYPES = {"DEX_POOL", "DEX_ROUTER"}
MONITORED_TYPES = CEX_TYPES | {"MM"} | HOLDER_TYPES
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


@dataclass
class Label:
    chain: str
    address: str
    entity: str
    entity_type: str
    confidence: str = "MEDIUM"
    source: str = ""
    notes: str = ""

    @property
    def reliable(self) -> bool:
        return self.confidence.upper() in ("HIGH", "MEDIUM") and self.entity_type != "WATCH"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LabelRegistry:
    def __init__(self):
        self.labels: Dict[tuple, Label] = {}
        self.errors: List[str] = []

    def add(self, lab: Label) -> None:
        t = lab.entity_type.upper()
        if t not in ENTITY_TYPES:
            self.errors.append(f"{lab.address}: unknown entity_type {lab.entity_type}")
            return
        lab.entity_type = t
        lab.chain = lab.chain.lower()
        lab.address = lab.address.lower()
        lab.confidence = lab.confidence.upper()
        self.labels[(lab.chain, lab.address)] = lab

    def load_csv(self, path: Path) -> int:
        if not Path(path).exists():
            return 0
        n = 0
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        for r in csv.DictReader(rows):
            try:
                self.add(Label(chain=r["chain"].strip(), address=r["address"].strip(), entity=r["entity"].strip(),
                               entity_type=r["entity_type"].strip(), confidence=(r.get("confidence") or "MEDIUM").strip(),
                               source=(r.get("source") or "labels csv").strip(), notes=(r.get("notes") or "").strip()))
                n += 1
            except (KeyError, AttributeError) as exc:
                self.errors.append(f"bad label row {r}: {exc!r}")
        return n

    def load_dicts(self, items: Iterable[Dict[str, Any]]) -> int:
        n = 0
        for d in items or []:
            self.add(Label(**{k: d.get(k, "") for k in ("chain", "address", "entity", "entity_type", "confidence",
                                                        "source", "notes")}))
            n += 1
        return n

    def lookup(self, chain: str, address: str) -> Optional[Label]:
        a = (address or "").lower()
        if a in (ZERO, DEAD):
            return Label(chain, a, "mint/burn", "BURN", "HIGH", "built-in")
        return self.labels.get((chain.lower(), a))

    def monitored(self, chain: str) -> List[Label]:
        return [l for (c, _), l in self.labels.items() if c == chain.lower() and l.reliable
                and l.entity_type in MONITORED_TYPES]

    def all(self) -> List[Dict[str, Any]]:
        return [l.to_dict() for l in self.labels.values()]
