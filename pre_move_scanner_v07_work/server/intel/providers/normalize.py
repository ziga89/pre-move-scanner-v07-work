"""Shared normalisation of multi-party transactions into sender -> receiver transfers.

UTXO chains (Bitcoin, Cardano): a transaction spends inputs and creates outputs.
  * The monitored address SPENT (it is among the inputs): every output to an address that is not one
    of the inputs (not change, not a consolidation) is a transfer monitored -> output address.
  * The monitored address only RECEIVED: every output to it is a transfer from the largest input
    address (multi-input transactions are attributed to the dominant input and say so).
  * Outputs back to input addresses are change and never counted; a pure consolidation is internal.

Account chains with balance deltas (Solana, Hedera): the monitored account's delta is allocated to
the counterparties with an opposite-sign delta, largest first, until it is used up. Fees are removed
from the payer's delta before allocation.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

Input = Tuple[Optional[str], float]            # (address id, amount)
Output = Tuple[int, Optional[str], float]      # (output index, address id, amount)


def utxo_transfers(is_owner: Callable[[Optional[str]], bool], owner: str, inputs: Sequence[Input],
                   outputs: Sequence[Output]) -> List[Tuple[int, str, str, float, str]]:
    """[(output_index, from, to, amount, note)] for the monitored `owner`."""
    in_addrs = {a for a, _ in inputs if a}
    spent = sum(v for a, v in inputs if is_owner(a))
    out: List[Tuple[int, str, str, float, str]] = []
    if spent > 0:
        for idx, a, v in outputs:
            if a is None or v <= 0 or is_owner(a) or a in in_addrs:
                continue                      # change / consolidation / OP_RETURN
            out.append((idx, owner, a, v, ""))
        return out
    received = [(idx, v) for idx, a, v in outputs if is_owner(a) and v > 0]
    if not received:
        return out
    by_addr: Dict[str, float] = {}
    for a, v in inputs:
        if a:
            by_addr[a] = by_addr.get(a, 0.0) + v
    if not by_addr:
        return out                            # coinbase / no attributable input
    sender = max(by_addr.items(), key=lambda kv: (kv[1], kv[0]))[0]
    note = f"multi-input transaction ({len(by_addr)} input addresses): attributed to the largest input" \
        if len(by_addr) > 1 else ""
    for idx, v in received:
        out.append((idx, sender, owner, v, note))
    return out


def allocate_deltas(owner: str, deltas: Dict[str, float], ignore: Iterable[str] = (),
                    min_amount: float = 0.0) -> List[Tuple[str, str, float]]:
    """[(from, to, amount)] allocating the owner's delta to opposite-sign counterparties."""
    d = deltas.get(owner, 0.0)
    if not d:
        return []
    skip = set(ignore) | {owner}
    sign = 1.0 if d < 0 else -1.0                # counterparties need the opposite sign
    counter = sorted(((a, v) for a, v in deltas.items() if a not in skip and v * sign > 0),
                     key=lambda kv: (-abs(kv[1]), kv[0]))
    remaining = abs(d)
    out: List[Tuple[str, str, float]] = []
    for a, v in counter:
        if remaining <= 0:
            break
        amt = min(abs(v), remaining)
        remaining -= amt
        if amt <= min_amount:
            continue
        out.append((owner, a, amt) if d < 0 else (a, owner, amt))
    return out
