"""Wallet label registry and the normalised entity taxonomy.

Labels come from `labels/wallet_labels.csv` (you edit it), v0.6 config watch-wallets, and anything
stored in the DB. Nothing is inferred from memory or scraped. Each label carries a confidence;
LOW-confidence, WATCH, WHALE_CANDIDATE and UNKNOWN rows are displayed but never drive
classification or scores.

Taxonomy (v0.8, one list for every chain):
    CEX_HOT  CEX_COLD  CEX_DEPOSIT  CEX_CUSTODY  CUSTODY_INSTITUTIONAL  MARKET_MAKER
    DEX_POOL  BRIDGE  TREASURY  WHALE  WHALE_CANDIDATE  UNKNOWN
plus DEX_ROUTER (a DEX contract), WATCH (display only) and the built-in BURN (mint / burn address).
v0.7 names are still accepted: MM -> MARKET_MAKER, PROTOCOL_TREASURY -> TREASURY.

Addresses are normalised per chain family: EVM addresses are lower-cased (XDC `xdc...` becomes
`0x...`); base58 / bech32 / Hedera ids keep their exact form, because base58 is case-sensitive.
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .addr import normalize_address
from .chains import family

ENTITY_TYPES = {
    "CEX_HOT", "CEX_COLD", "CEX_DEPOSIT", "CEX_CUSTODY", "CUSTODY_INSTITUTIONAL", "MARKET_MAKER",
    "DEX_POOL", "DEX_ROUTER", "BRIDGE", "TREASURY", "WHALE", "WHALE_CANDIDATE", "UNKNOWN", "BURN", "WATCH",
}
ALIASES = {"MM": "MARKET_MAKER", "PROTOCOL_TREASURY": "TREASURY", "MARKETMAKER": "MARKET_MAKER"}
CEX_TYPES = {"CEX_HOT", "CEX_COLD", "CEX_DEPOSIT", "CEX_CUSTODY"}
CUSTODY_TYPES = {"CEX_CUSTODY", "CUSTODY_INSTITUTIONAL"}
HOLDER_TYPES = {"WHALE", "TREASURY"}                 # accumulation / distribution-capable holders
DEX_TYPES = {"DEX_POOL", "DEX_ROUTER"}
MM_TYPES = {"MARKET_MAKER"}
UNRELIABLE_TYPES = {"WATCH", "WHALE_CANDIDATE", "UNKNOWN"}
MONITORED_TYPES = CEX_TYPES | MM_TYPES | HOLDER_TYPES | {"CUSTODY_INSTITUTIONAL"}
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


def entity_type(t: Optional[str]) -> Optional[str]:
    """Canonical v0.8 entity type (accepts the v0.7 names)."""
    if t is None:
        return None
    t = str(t).strip().upper()
    return ALIASES.get(t, t)


def norm_address(chain: str, address: str) -> str:
    """Canonical address form on `chain` (unknown chains: stripped, unchanged)."""
    a = str(address or "").strip()
    fam = family(chain)
    if fam:
        n = normalize_address(fam, a)
        if n:
            return n
        if fam == "evm":
            return a.lower()
    return a


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
        return self.confidence.upper() in ("HIGH", "MEDIUM") and self.entity_type not in UNRELIABLE_TYPES

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LabelRegistry:
    def __init__(self):
        self.labels: Dict[tuple, Label] = {}
        self.errors: List[str] = []

    def add(self, lab: Label) -> None:
        t = entity_type(lab.entity_type)
        if t not in ENTITY_TYPES:
            self.errors.append(f"{lab.address}: unknown entity_type {lab.entity_type}")
            return
        lab.entity_type = t
        lab.chain = lab.chain.strip().lower()
        fam = family(lab.chain)
        addr = norm_address(lab.chain, lab.address)
        if fam and fam != "other" and normalize_address(fam, addr) is None:
            self.errors.append(f"{lab.address}: not a valid {lab.chain} address")
            return
        lab.address = addr
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
        a = norm_address(chain, address)
        if family(chain) == "evm" and a in (ZERO, DEAD):
            return Label(chain, a, "mint/burn", "BURN", "HIGH", "built-in")
        return self.labels.get((chain.lower(), a))

    def monitored(self, chain: str) -> List[Label]:
        return [l for (c, _), l in self.labels.items() if c == chain.lower() and l.reliable
                and l.entity_type in MONITORED_TYPES]

    def chains(self) -> List[str]:
        return sorted({c for c, _ in self.labels})

    def all(self) -> List[Dict[str, Any]]:
        return [l.to_dict() for l in self.labels.values()]
