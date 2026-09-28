"""Explicit wallet-intelligence states per asset and per score (v0.7.3).

Instead of a generic "N/A", every wallet value is one of:

  OFF          wallet intelligence disabled in the config
  NO KEY       enabled, but no provider API key (ETHERSCAN_API_KEY) is set
  WARMING      token known / being looked up, first polls pending, or the
               transfer history window is still filling
  UNSUPPORTED  the asset lives on a chain no provider covers (BTC, XRP, SOL,
               native XDC, ...) or is a native gas coin (ETH, BNB, AVAX)
  N/A          supported, but no reliable attribution: no contract known, no
               labelled addresses of the kind a score needs, no USD reference
  <value>      a real 0-100 score (0 = covered and quiet)

Unknown wallets are never given an identity: without an independent, reliable
label an address stays UNKNOWN (large ones are listed as WHALE CANDIDATE and
are not counted anywhere).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .labels import CEX_TYPES, HOLDER_TYPES

OFF, NO_KEY, WARMING, UNSUPPORTED, NA, OK = "OFF", "NO_KEY", "WARMING", "UNSUPPORTED", "NA", "OK"
LABEL = {OFF: "OFF", NO_KEY: "NO KEY", WARMING: "WARMING", UNSUPPORTED: "UNSUPPORTED", NA: "N/A", OK: "ON"}
SCORES = ("mm", "whale", "cex_flow", "scarcity")
# label entity types each score needs (every group must have at least one reliable address)
SCORE_NEEDS = {"cex_flow": ((CEX_TYPES, "exchange"),), "mm": (({"MM"}, "market-maker"),),
               "whale": ((HOLDER_TYPES, "whale / custody / treasury"),),
               "scarcity": ((CEX_TYPES, "exchange"), (HOLDER_TYPES, "whale / custody / treasury"))}


def label_types(labels: Any, chain: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for lab in labels.monitored(chain):
        out[lab.entity_type] = out.get(lab.entity_type, 0) + 1
    return out


def _all(state: str, reason: str, **extra) -> Dict[str, Any]:
    return {"state": state, "label": LABEL[state], "reason": reason,
            "scores": {k: {"state": state, "label": LABEL[state], "value": None, "direction": None, "reason": reason}
                       for k in SCORES}, **extra}


def asset_status(asset: str, *, enabled: bool, keyed: bool, monitor: Any = None, discovery: Any = None,
                 labels: Any = None, scores: Optional[Dict[str, Any]] = None, now: float = 0.0,
                 warm_seconds: float = 3600.0) -> Dict[str, Any]:
    asset = asset.upper()
    if not enabled:
        return _all(OFF, "wallet intelligence is off (config intel.enabled = false)")
    if not keyed:
        return _all(NO_KEY, "set the ETHERSCAN_API_KEY environment variable (free key at etherscan.io)")
    tok = monitor.tokens.get(asset) if monitor is not None else None
    if tok is None:
        d = discovery.result(asset) if discovery is not None else None
        if d is None:
            if discovery is not None:
                return _all(WARMING, "looking up the token contract (CoinGecko platforms)")
            return _all(NA, "no EVM contract configured for this asset (intel.tokens)")
        if d.get("state") == "unsupported":
            return _all(UNSUPPORTED, d.get("reason") or "chain not supported", chain=d.get("chain"))
        if d.get("state") == "error":
            return _all(WARMING, f"contract lookup will be retried ({d.get('reason')})")
        return _all(NA, d.get("reason") or "no EVM contract found")
    chain, extra = tok["chain"], {"chain": tok["chain"], "contract": tok["contract"], "source": tok.get("source")}
    if not monitor.supports(chain):
        return _all(UNSUPPORTED, f"no wallet-data provider for {chain}", **extra)
    if str(tok.get("status", "")).startswith("invalid"):
        return _all(NA, f"contract rejected: {tok['status']}", **extra)
    cov = monitor.coverage(asset)
    types = label_types(labels, chain) if labels is not None else {}
    if labels is not None and not types:
        # token-wide polling alone cannot attribute anything: every score needs reliable labels
        return _all(NA, f"no reliable labelled exchange / MM / custody / whale addresses on {chain} "
                        "(labels/wallet_labels.csv)", **extra)
    if not cov.get("covered"):
        reason = cov.get("reason", "")
        if "no successful polls" in reason:
            return _all(WARMING, "first polls pending", **extra)
        if "labelled" in reason:
            return _all(NA, f"no reliable labelled exchange / MM / custody / whale addresses on {chain} "
                            "(labels/wallet_labels.csv)", **extra)
        return _all(NA, reason or "not covered", **extra)
    first = (getattr(monitor, "first_ok_ts", {}) or {}).get(chain)
    if first is None or now - first < warm_seconds:
        done = 0 if first is None else (now - first) / 60.0
        return _all(WARMING, f"collecting transfer history ({done:.0f}/{warm_seconds / 60:.0f} min)", **extra)
    per: Dict[str, Dict[str, Any]] = {}
    for k in SCORES:
        missing = [desc for group, desc in SCORE_NEEDS[k] if sum(types.get(t, 0) for t in group) == 0]
        if missing:
            r = f"no reliable labelled {' / '.join(missing)} addresses on {chain}"
            per[k] = {"state": NA, "label": LABEL[NA], "value": None, "direction": None, "reason": r}
        elif scores is None or scores.get(k) is None:
            r = (scores or {}).get("status") or "no USD price / volume reference"
            per[k] = {"state": NA, "label": LABEL[NA], "value": None, "direction": None, "reason": r}
        else:
            per[k] = {"state": OK, "label": LABEL[OK], "value": scores[k], "direction": scores.get(f"{k}_direction"),
                      "reason": (scores or {}).get("status", "ok")}
    ok = [k for k in SCORES if per[k]["state"] == OK]
    state = OK if ok else NA
    reason = "ok" if ok else next(iter(v["reason"] for v in per.values()), "no attribution")
    lagging = bool((cov or {}).get("lagging"))
    return {"state": state, "label": LABEL[state] if not lagging else "ON (partial)", "reason": reason,
            "scores": per, **extra}


def gate_scores(scores: Optional[Dict[str, Any]], status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scores passed to the engine / radar: only values whose state is OK survive (others None)."""
    if scores is None:
        return None
    out = dict(scores)
    for k in SCORES:
        if (status.get("scores") or {}).get(k, {}).get("state") != OK:
            out[k] = None
            out[f"{k}_direction"] = None
    return out


def coverage_summary(statuses: Dict[str, Dict[str, Any]], *, enabled: bool, keyed: bool, monitor: Any = None,
                     discovery: Any = None, labels: Any = None, chains: Iterable[str] = ()) -> Dict[str, Any]:
    by: Dict[str, int] = {}
    for st in statuses.values():
        by[st["state"]] = by.get(st["state"], 0) + 1
    n = len(statuses)
    ok = by.get(OK, 0)
    if not enabled:
        state, text = OFF, "Wallet intel OFF"
    elif not keyed:
        state, text = NO_KEY, "Wallet intel: NO KEY (set ETHERSCAN_API_KEY)"
    else:
        state = OK if ok else (WARMING if by.get(WARMING) else NA)
        text = f"Wallet intel ON · {ok}/{n} assets with data"
        if by.get(WARMING):
            text += f" · {by[WARMING]} warming"
        if by.get(UNSUPPORTED):
            text += f" · {by[UNSUPPORTED]} unsupported chains"
    labelled: Dict[str, Dict[str, int]] = {}
    if labels is not None:
        for c in chains:
            t = label_types(labels, c)
            if t:
                labelled[c] = t
    toks: Dict[str, int] = {}
    if monitor is not None:
        for t in monitor.tokens.values():
            toks[t.get("source") or "config"] = toks.get(t.get("source") or "config", 0) + 1
    last_poll = None
    if monitor is not None:
        polls = [s.get("last_ok") for s in monitor.addr_state.values() if isinstance(s, dict) and s.get("last_ok")]
        last_poll = max(polls) if polls else None
    return {"state": state, "label": LABEL[state] if state != OK else "ON", "text": text, "assets": n,
            "by_state": by, "with_data": ok, "labelled_addresses": labelled, "tokens": toks,
            "discovery": discovery.stats() if discovery is not None else None, "last_poll": last_poll,
            "supported_chains": list(chains)}


def status_list(statuses: Dict[str, Dict[str, Any]], states: List[str]) -> List[str]:
    return sorted(a for a, s in statuses.items() if s["state"] in states)
