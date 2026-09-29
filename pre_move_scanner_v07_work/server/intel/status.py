"""Explicit wallet-intelligence states per asset and per score (v0.8).

  OFF          wallet intelligence disabled (globally, or this chain's provider is disabled)
  NO KEY       the chain's provider needs credentials that are not set (e.g. ETHERSCAN_API_KEY)
  DISCOVERING  chain / platform / contract metadata is still being looked up
  WARMING      the provider works, but the first polls or the history window are not complete
  ACTIVE       usable wallet intelligence exists (at least one score has a real value)
  UNSUPPORTED  no working provider exists for the asset's chain (the reason says why, e.g.
               "Sui: provider not implemented")
  DEGRADED     the provider is temporarily unavailable or incomplete (rate-limited, failing, out of
               budget, or the API plan does not cover the chain)
  N/A          the provider works but reliable attribution is unavailable (no labelled addresses of
               the needed kind, rejected / unverifiable contract, no USD reference)

A real 0 is a value ("covered and quiet"), never N/A. Only ACTIVE values reach scoring and the radar.
Unknown wallets are never given an identity: without an independent, reliable label an address stays
UNKNOWN (large ones are listed as WHALE CANDIDATE and are not counted anywhere).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .chains import chain_name
from .labels import CEX_TYPES, HOLDER_TYPES, MM_TYPES
from .registry import ERROR, NEEDS_VERIFICATION, NOT_FOUND, PENDING, READY, UNSUPPORTED as REG_UNSUPPORTED

OFF, NO_KEY, DISCOVERING, WARMING, ACTIVE, UNSUPPORTED, DEGRADED, NA = (
    "OFF", "NO_KEY", "DISCOVERING", "WARMING", "ACTIVE", "UNSUPPORTED", "DEGRADED", "NA")
STATES = (OFF, NO_KEY, DISCOVERING, WARMING, ACTIVE, UNSUPPORTED, DEGRADED, NA)
LABEL = {OFF: "OFF", NO_KEY: "NO KEY", DISCOVERING: "DISCOVERING", WARMING: "WARMING", ACTIVE: "ACTIVE",
         UNSUPPORTED: "UNSUPPORTED", DEGRADED: "DEGRADED", NA: "N/A"}
OK = ACTIVE                                   # v0.7 name
SCORES = ("mm", "whale", "cex_flow", "scarcity")
# label entity types each score needs (every group must have at least one reliable address)
SCORE_NEEDS = {"cex_flow": ((CEX_TYPES, "exchange"),), "mm": ((MM_TYPES, "market-maker"),),
               "whale": ((HOLDER_TYPES, "whale / treasury"),),
               "scarcity": ((CEX_TYPES, "exchange"), (HOLDER_TYPES, "whale / treasury"))}


def label_types(labels: Any, chain: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for lab in labels.monitored(chain):
        out[lab.entity_type] = out.get(lab.entity_type, 0) + 1
    return out


def _all(state: str, reason: str, **extra) -> Dict[str, Any]:
    label = LABEL[state]
    return {"state": state, "label": label, "reason": reason, "short": f"{label} · {reason}" if state in
            (UNSUPPORTED, DEGRADED, NA, NO_KEY) else label,
            "scores": {k: {"state": state, "label": label, "value": None, "direction": None, "reason": reason}
                       for k in SCORES}, **extra}


def _meta(ta: Any = None, entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if entry:
        out.update(chain=entry.get("native_chain"), contract=entry.get("contract_address"),
                   native=bool(entry.get("native_asset")), provider=entry.get("wallet_provider"),
                   registry_state=entry.get("state"), verified=bool(entry.get("verified")),
                   source=entry.get("source"))
    if ta is not None:
        out.update(chain=ta.chain, contract=ta.token, native=ta.native, source=ta.source,
                   token_status=ta.status)
    if out.get("chain"):
        out["chain_name"] = chain_name(out["chain"])
    return out


def asset_status(asset: str, *, enabled: bool, monitor: Any = None, registry: Any = None, providers: Any = None,
                 labels: Any = None, scores: Optional[Dict[str, Any]] = None, now: float = 0.0,
                 warm_seconds: float = 3600.0) -> Dict[str, Any]:
    asset = asset.upper()
    if not enabled:
        return _all(OFF, "wallet intelligence is off (config intel.enabled = false)")
    ta = monitor.assets.get(asset) if monitor is not None else None
    entry = registry.get(asset) if registry is not None else None
    if ta is None:
        meta = _meta(None, entry)
        if entry is None:
            if registry is None:
                return _all(NA, "no chain / contract metadata for this asset", **meta)
            return _all(DISCOVERING, "looking up chain / platform / contract (CoinGecko)", **meta)
        st = entry.get("state")
        if st in (None, PENDING):
            return _all(DISCOVERING, "looking up chain / platform / contract (CoinGecko)", **meta)
        if st == ERROR:
            return _all(DISCOVERING, entry.get("reason") or "metadata lookup will be retried", **meta)
        if st == REG_UNSUPPORTED:
            return _all(UNSUPPORTED, entry.get("reason") or "provider not implemented", **meta)
        if st in (NOT_FOUND, NEEDS_VERIFICATION):
            return _all(NA, entry.get("reason") or "no reliable contract", **meta)
        if st == READY:
            return _all(DISCOVERING, "metadata ready; starting the wallet provider", **meta)
        return _all(NA, entry.get("reason") or "not tracked", **meta)
    meta = _meta(ta, entry)
    chain = ta.chain
    cname = chain_name(chain)
    if providers is not None:
        cs = providers.chain_state(chain)
        meta.update(provider=cs.get("provider"), provider_label=cs.get("provider_label"))
        state = cs.get("state")
        if state == "unsupported":
            return _all(UNSUPPORTED, cs.get("reason") or f"{cname}: provider not implemented", **meta)
        if state == "off":
            return _all(OFF, cs.get("reason") or "provider disabled", **meta)
        if state == "no_key":
            return _all(NO_KEY, cs.get("reason") or "provider credentials missing", **meta)
        if state == "degraded":
            return _all(DEGRADED, cs.get("reason") or "provider temporarily unavailable", **meta)
    elif monitor is not None and not monitor.supports(chain):
        return _all(UNSUPPORTED, f"{cname}: provider not implemented", **meta)
    if str(ta.status).startswith("invalid"):
        return _all(NA, f"contract rejected: {ta.status}", **meta)
    cov = monitor.coverage(asset)
    types = label_types(labels, chain) if labels is not None else {}
    if labels is not None and not types:
        # token-wide polling alone cannot attribute anything: every score needs reliable labels
        return _all(NA, f"no reliable labelled exchange / MM / custody / whale addresses on {cname} "
                        "(labels/wallet_labels.csv)", **meta)
    if not cov.get("covered"):
        reason = cov.get("reason", "")
        if "no successful polls" in reason:
            return _all(WARMING, "first polls pending", **meta)
        if "labelled" in reason:
            return _all(NA, f"no reliable labelled exchange / MM / custody / whale addresses on {cname} "
                            "(labels/wallet_labels.csv)", **meta)
        return _all(NA, reason or "not covered", **meta)
    first = (getattr(monitor, "first_ok_ts", {}) or {}).get(chain)
    if first is None or now - first < warm_seconds:
        done = 0 if first is None else (now - first) / 60.0
        return _all(WARMING, f"collecting transfer history ({done:.0f}/{warm_seconds / 60:.0f} min)", **meta)
    per: Dict[str, Dict[str, Any]] = {}
    for k in SCORES:
        missing = [desc for group, desc in SCORE_NEEDS[k] if sum(types.get(t, 0) for t in group) == 0]
        if missing:
            r = f"no reliable labelled {' / '.join(missing)} addresses on {cname}"
            per[k] = {"state": NA, "label": LABEL[NA], "value": None, "direction": None, "reason": r}
        elif scores is None or scores.get(k) is None:
            r = (scores or {}).get("status") or "no USD price / volume reference"
            per[k] = {"state": NA, "label": LABEL[NA], "value": None, "direction": None, "reason": r}
        else:
            per[k] = {"state": ACTIVE, "label": LABEL[ACTIVE], "value": scores[k],
                      "direction": scores.get(f"{k}_direction"), "reason": (scores or {}).get("status", "ok")}
    ok = [k for k in SCORES if per[k]["state"] == ACTIVE]
    state = ACTIVE if ok else NA
    reason = "ok" if ok else next(iter(v["reason"] for v in per.values()), "no attribution")
    lagging = bool((cov or {}).get("lagging"))
    label = LABEL[state] if not (lagging and state == ACTIVE) else "ACTIVE (partial)"
    note = (cov or {}).get("note")
    return {"state": state, "label": label, "reason": reason + (f" · {note}" if note and ok else ""),
            "short": label if ok else f"{label} · {reason}", "scores": per, "lagging": lagging, **meta}


def gate_scores(scores: Optional[Dict[str, Any]], status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scores passed to the engine / radar: only values whose state is ACTIVE survive (others None)."""
    if scores is None:
        return None
    out = dict(scores)
    for k in SCORES:
        if (status.get("scores") or {}).get(k, {}).get("state") != ACTIVE:
            out[k] = None
            out[f"{k}_direction"] = None
    return out


def coverage_summary(statuses: Dict[str, Dict[str, Any]], *, enabled: bool, monitor: Any = None,
                     registry: Any = None, labels: Any = None, providers: Any = None,
                     chains: Iterable[str] = ()) -> Dict[str, Any]:
    by: Dict[str, int] = {}
    for st in statuses.values():
        by[st["state"]] = by.get(st["state"], 0) + 1
    n = len(statuses)
    active = by.get(ACTIVE, 0)
    if not enabled:
        state, text = OFF, "Wallet intel OFF"
    else:
        if active:
            state = ACTIVE
        elif n and by.get(NO_KEY, 0) == n:
            state = NO_KEY
        elif by.get(DEGRADED):
            state = DEGRADED
        elif by.get(DISCOVERING) or by.get(WARMING):
            state = WARMING
        elif n and by.get(UNSUPPORTED, 0) == n:
            state = UNSUPPORTED
        else:
            state = NA
        parts = [f"{active}/{n} active"]
        for s, word in ((DISCOVERING, "discovering"), (WARMING, "warming"), (DEGRADED, "degraded"),
                        (NO_KEY, "no key"), (UNSUPPORTED, "unsupported"), (NA, "N/A")):
            if by.get(s):
                parts.append(f"{by[s]} {word}")
        text = "Wallet intel · " + " · ".join(parts)
    labelled: Dict[str, Dict[str, int]] = {}
    if labels is not None:
        for c in sorted(set(chains) | set(labels.chains() if hasattr(labels, "chains") else [])):
            t = label_types(labels, c)
            if t:
                labelled[c] = t
    toks: Dict[str, int] = {}
    if monitor is not None:
        for t in monitor.assets.values():
            toks[t.source or "discovered"] = toks.get(t.source or "discovered", 0) + 1
    last_poll = None
    if monitor is not None:
        polls = [s.get("last_ok") for s in monitor.addr_state.values() if isinstance(s, dict) and s.get("last_ok")]
        last_poll = max(polls) if polls else None
    return {"state": state, "label": LABEL[state], "text": text, "assets": n, "by_state": by, "with_data": active,
            "labelled_addresses": labelled, "tokens": toks,
            "discovery": registry.stats() if registry is not None else None, "last_poll": last_poll,
            "supported_chains": list(chains)}


def status_list(statuses: Dict[str, Dict[str, Any]], states: List[str]) -> List[str]:
    return sorted(a for a, s in statuses.items() if s["state"] in states)
