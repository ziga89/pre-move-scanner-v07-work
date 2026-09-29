"""The normalised wallet-data model shared by every provider.

Providers turn raw chain data (ERC-20 logs, Bitcoin UTXOs, XRPL payments, Solana balance diffs,
Hedera transfer lists, Cardano UTXOs, ...) into `RawTransfer`s: one sender, one receiver, one
amount in asset units. The monitor then attributes both sides with the label registry and
classifies the transfer into a `WalletEvent`. Scoring only ever sees `WalletEvent`s (as rows), so
it never cares which chain or provider the data came from.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

NATIVE = "native"


def stable_index(*parts: Any) -> int:
    """Deterministic 31-bit index for events that have no log index (UTXO outputs are numbered
    by the chain; account-model balance diffs use this so both sides derive the same key)."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


@dataclass
class RawTransfer:
    chain: str
    asset: str                       # scanner symbol (e.g. "QNT")
    token: str                       # "native" or the token identifier on the chain
    tx_hash: str
    index: int                       # log index / output index / stable_index(...)
    ts: float
    from_address: str
    to_address: str
    amount: float                    # asset units (decimals applied)
    block: Optional[int] = None
    symbol: Optional[str] = None     # token symbol as reported on-chain (contract verification)
    decimals: Optional[int] = None
    provider: str = ""
    note: str = ""                   # normalisation note (e.g. "multi-input UTXO: largest input")


@dataclass
class TrackedAsset:
    """One asset on one chain, as the wallet monitor tracks it."""
    asset: str
    chain: str
    native: bool
    token: Optional[str] = None      # contract / mint / token id; None for native coins
    decimals: Optional[int] = None
    provider: Optional[str] = None
    source: str = "discovered"       # config | override | discovered
    status: str = "pending"          # pending | ok | invalid: ...
    verified_symbol: Optional[str] = None
    token_wide: str = "off"          # EVM only: off | auto | on
    token_wide_status: Optional[str] = None
    token_wide_reason: Optional[str] = None

    @property
    def key(self) -> str:
        return NATIVE if self.native else str(self.token)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["contract"] = self.token        # v0.7 field name, kept for the UI / API
        return d


@dataclass
class FetchResult:
    transfers: List[RawTransfer] = field(default_factory=list)
    cursor: Dict[str, Any] = field(default_factory=dict)
    complete: bool = True            # False: more data pending than one poll could page through
    note: str = ""


@dataclass
class WalletEvent:
    """A classified, attributed transfer (the internal event model of the specification)."""
    asset: str
    chain: str
    timestamp: float
    tx_hash: str
    event_type: str
    amount_native: float
    amount_usd: Optional[float]
    from_address: str
    to_address: str
    from_entity: Optional[str]
    to_entity: Optional[str]
    entity_type: str
    attribution_confidence: str
    classification_confidence: str
    index: int = 0
    block: Optional[int] = None
    token: str = NATIVE
    from_type: Optional[str] = None
    to_type: Optional[str] = None
    direction: Optional[str] = None
    explanation: str = ""
    provider: str = ""
    source: str = "address"

    def row(self) -> Dict[str, Any]:
        """The storage / scoring row (column names of `onchain_transfers`)."""
        return {"chain": self.chain, "tx_hash": self.tx_hash, "log_index": self.index, "ts": self.timestamp,
                "block": self.block, "token": self.token, "asset": self.asset, "from_addr": self.from_address,
                "to_addr": self.to_address, "amount": self.amount_native, "usd_value": self.amount_usd,
                "from_entity": self.from_entity, "from_type": self.from_type, "to_entity": self.to_entity,
                "to_type": self.to_type, "classification": self.event_type, "event_type": self.event_type,
                "class_confidence": self.classification_confidence,
                "attribution_confidence": self.attribution_confidence, "entity_type": self.entity_type,
                "direction": self.direction, "explanation": self.explanation, "provider": self.provider,
                "source": self.source}
