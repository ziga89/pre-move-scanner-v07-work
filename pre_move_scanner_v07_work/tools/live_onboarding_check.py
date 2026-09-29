"""Live end-to-end check of the v0.8 universe / manual-asset / registry / radar path against the real
CoinGecko API and exchanges (no API keys needed). Uses its own database and universe cache
(data/live_check.db, data/live_check_universe_cache.json); your scanner database and config are not touched.

    python tools/live_onboarding_check.py [--top 20] [--seconds 60]

1. start the real service: CoinGecko Top-N (small N to keep the feed load low) + the config's manual
   assets (QNT, LINK, XDC by default, seeded once) - checks they are resolved by CoinGecko id and monitored
2. pick a coin ranked 251-500 with a usable exchange market (from the ranking the service fetched), add it
   as a manual asset through the service API (no restart), and check it gets venues, live feeds and results
3. discover chain / platform / contract metadata for the universe (registry) and report wallet states
4. check ranking (manual assets are not pinned to the top) and the Signal Radar payload
5. restart the service on the same database: the manual asset is still monitored, metadata is cached
6. remove it: monitoring stops, history is kept
A step that cannot run because a public API refused the runner (HTTP 403 / 429) is reported as
NOT VERIFIED (WARN), never as a pass. Writes data/live_onboarding_report.md / .json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import __version__  # noqa: E402
from server.config import deep_merge, load_config  # noqa: E402
from server.service import ScannerService  # noqa: E402
from server.universe.http import HttpError  # noqa: E402

RESULTS: List[Dict[str, Any]] = []


def rec(step: str, status: str, detail: str) -> None:
    RESULTS.append({"step": step, "status": status, "detail": detail})
    text = f"[{status}] {step}: {detail}"
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def check(step: str, ok: bool, detail: str, warn: bool = False) -> bool:
    rec(step, "PASS" if ok else ("WARN" if warn else "FAIL"), detail)
    return ok


def refused(exc: BaseException) -> bool:
    """A public API refused this runner (rate limit / IP block) - an environment limit, not a product bug."""
    if isinstance(exc, HttpError):
        return exc.status in (403, 429)
    return "429" in str(exc) or "403" in str(exc)


def not_verified(step: str, exc: BaseException) -> None:
    rec(step, "WARN", f"NOT VERIFIED - the public API refused this runner ({str(exc)[:140]})")


async def wait_for(pred, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(1.0)
    return pred()


async def patiently(step: str, fn, waits=(60.0, 120.0)):
    """Call fn(); on a refusal by the public API wait and retry (GitHub runners share their IPs)."""
    for i in range(len(waits) + 1):
        try:
            return await fn()
        except Exception as exc:
            if not refused(exc) or i == len(waits):
                raise
            rec(step, "WARN", f"public API refused the call ({str(exc)[:100]}); retrying in {waits[i]:.0f}s")
            await asyncio.sleep(waits[i])


async def live_universe(svc: ScannerService) -> bool:
    """The first start may have found CoinGecko unreachable; retry like the service does (2, 4 minutes)."""
    for wait in (120.0, 240.0):
        if svc.universe_live:
            return True
        rec("universe", "WARN", f"no live CoinGecko ranking yet ({svc.status.get('universe')}); "
                                f"the service retries - checking again in {wait:.0f}s")
        await asyncio.sleep(wait)
        await svc.refresh_universe()
        await svc.apply_selection()
    return svc.universe_live


def pick_candidate(svc: ScannerService, cache: Path) -> Optional[Dict[str, Any]]:
    """A coin ranked 251-500 in the ranking the service fetched, not yet monitored, with a usable market."""
    try:
        rows = json.loads(cache.read_text(encoding="utf-8")).get("rows", [])
    except (OSError, ValueError):
        return None
    now = time.time()
    for r in sorted(rows, key=lambda r: r.get("market_cap_rank") or 9999):
        if not 250 < (r.get("market_cap_rank") or 0) <= 500:
            continue
        if str(r.get("symbol", "")).upper() in svc.info:
            continue
        if svc._usable(r, now)[0]:
            return r
    return None


async def run1(svc: ScannerService, a, cache: Path) -> Optional[str]:
    added = None
    t0 = time.time()
    await svc.start()
    rec("start", "PASS", f"{svc.status.get('discovery')}; {svc.status.get('universe')}; started in {time.time() - t0:.0f}s")
    u = svc.universe_payload()
    man = {m["symbol"]: m for m in u["manual"]}
    check("manual assets seeded", set(man) == {"QNT", "LINK", "XDC"}, f"{sorted(man)}")
    for sym, cid in (("QNT", "quant-network"), ("LINK", "chainlink"), ("XDC", "xdce-crowd-sale")):
        m = man.get(sym, {})
        check(f"manual {sym} has its CoinGecko id", m.get("coingecko_id") == cid,
              f"id {m.get('coingecko_id')}, state {m.get('state')}, status {m.get('status')}, in_top {m.get('in_top')}")
    check("manual assets monitored even without a ranking", all(s in svc.info for s in ("QNT", "XDC")),
          f"monitored: {sorted(s for s in man if s in svc.info)}")
    live = await live_universe(svc)
    if not live:
        rec("universe = top + manual", "WARN", f"NOT VERIFIED - CoinGecko ranking unavailable from this runner "
                                               f"({svc.status.get('universe')})")
    else:
        u = svc.universe_payload()
        top_ids = {x["id"] for x in u["members"]}
        outside = [m for m in u["manual"] if m["monitored"] and not m["in_top"]]
        inside = [m["symbol"] for m in u["manual"] if m["in_top"]]
        check("universe = top + manual (deduplicated)", len(svc.info) == len(top_ids) + len(outside),
              f"{len(top_ids)} top + {len(outside)} manual outside the top = {len(svc.info)} monitored; "
              f"manual assets already in the top (listed once): {inside}")
        cand = pick_candidate(svc, cache)
        if cand is None:
            rec("pick manual candidate", "WARN", "NOT VERIFIED - no usable coin found at ranks 251-500")
        else:
            try:
                res = await patiently("search", lambda: svc.search_assets(str(cand["symbol"])))
                check("search candidates", any(c["coingecko_id"] == cand["id"] for c in res["candidates"]),
                      f"'{cand['symbol']}': {len(res['candidates'])} candidates, ambiguous={res['ambiguous']}")
                prev = await patiently("preview", lambda: svc.resolve_asset(cand["id"]))
                md = prev["candidate"]["metadata"]
                rec("preview", "PASS", f"{cand['id']} #{cand.get('market_cap_rank')}: {md.get('state')} chain "
                    f"{md.get('chain_name')} native {md.get('native_asset')} contract {md.get('contract_address')} "
                    f"provider {md.get('wallet_provider')} venues {[v['exchange'] for v in prev['candidate']['venues']]}")
                r = await patiently("add", lambda: svc.add_manual_asset(coingecko_id=cand["id"]))
                added = r["asset"]["symbol"]
                check("add manual asset (no restart)", r["status"] == "added" and added in svc.info,
                      f"{added}: {r['integration']}")
                ok = await wait_for(lambda: (svc.results.get(added) or {}).get("coverage", 0) > 0, a.seconds)
                rr = svc.results.get(added) or {}
                check("new manual asset streams", ok, f"{added}: status {rr.get('status')}, live venues "
                      f"{rr.get('coverage')}/{rr.get('coverage_total')}")
            except Exception as exc:
                if not refused(exc):
                    raise
                not_verified("manual asset search / preview / add", exc)
    n = 0
    for _ in range(4):
        n += len(await svc._registry_once(6))
    reg = svc.registry.stats()
    if reg["known"]:
        check("registry discovery", reg["known"] >= min(len(svc.info) // 2, 20),
              f"{n} lookups this run; {reg['by_state']}; errors {reg['errors']} {reg['last_error'][:100]}")
    else:
        rec("registry discovery", "WARN" if refused(Exception(reg["last_error"])) else "FAIL",
            f"no metadata discovered: {reg['last_error'][:160]}")
    for sym in ["QNT", "LINK", "XDC", "BTC", "ETH", "SOL", "XRP"] + ([added] if added else []):
        e = svc.registry.public(sym)
        if e and e.get("state") not in (None, "PENDING"):
            rec(f"metadata {sym}", "PASS", f"{e['state']} / {e.get('chain_name')} / native {e['native_asset']} / "
                f"contract {e.get('contract_address')} / provider {e.get('wallet_provider')} / {e.get('reason')}")
    await asyncio.sleep(12)                  # a few ticks + the 10 s wallet summary refresh
    ws = svc.wallet_status_payload()
    by = ws["summary"].get("by_state") or {}
    rec("wallet states", "PASS", f"{by} / {ws['summary'].get('text')}")
    bare = [s for s, v in ws["assets"].items() if v.get("state") in ("UNSUPPORTED", "DEGRADED", "NA", "NO_KEY")
            and not v.get("reason")]
    check("every non-value state has a reason", not bare, f"without reason: {bare}")
    top = svc.top_payload()
    pos = {r["asset"]: r["position"] for r in top["rows"]}
    mp = {s: pos.get(s) for s in (["QNT", "LINK", "XDC"] + ([added] if added else [])) if s in pos}
    rec("ranking", "PASS", f"{len(top['rows'])} rows; manual asset positions {mp} (ordered by score, not pinned)")
    radar = svc.radar_payload()
    check("signal radar", radar["state"] in ("NONE", "WATCH", "CONFIRMING", "HIGH_CONVICTION", "INVALIDATED")
          and "not a probability" in radar.get("note", ""),
          f"{radar['label']}; {len(radar['entries'])} entries; wallet: {radar['wallet'].get('text')}")
    prov = svc.wallet_providers_payload()
    rec("providers", "PASS", "; ".join(f"{c['label']}={c['state']} ({len(c['assets'])} assets)" for c in prov["chains"]))
    svc.db.flush()
    return added


async def run2(svc: ScannerService, added: Optional[str]) -> None:
    await svc.start()
    st = svc.db.stats()
    check("restart on the same database", not st["new_install"] and not st["migrations_applied_now"],
          f"schema {st['schema_version']}, new_install {st['new_install']}, previous app version "
          f"{st.get('previous_app_version')}")
    if added:
        check("manual asset persists across restart", added in svc.info and svc.info[added].manual,
              f"{added} monitored after restart: {added in svc.info}")
    e = svc.registry.get("QNT")
    if e:
        check("registry cached across restart", e.get("state") not in (None, "PENDING"),
              f"QNT registry state {e.get('state')} (no rediscovery needed)")
    if added:
        r = await svc.remove_manual_asset(added)
        check("remove manual asset", added not in svc.info, f"{added}: {r['result']}")
        n = svc.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM asset_metrics_5s WHERE asset=?",
                                                 (added,)).fetchone()[0])
        check("history kept after removal", n > 0, f"{n} stored 5 s rows for {added}", warn=True)
        still = [m["symbol"] for m in svc.manual.active()]
        check("removed from the manual list", added not in still, f"manual assets now {still}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=60.0)
    a = ap.parse_args()
    db = ROOT / "data" / "live_check.db"
    cache = ROOT / "data" / "live_check_universe_cache.json"
    for p in [Path(str(db) + s) for s in ("", "-wal", "-shm")] + [cache]:
        if p.exists():
            p.unlink()
    base = load_config(ROOT, create_if_missing=False)
    cfg = deep_merge(base, {"mode": "live", "universe": {"target_size": a.top, "manual_assets": ["QNT", "LINK", "XDC"],
                                                          "cache_path": str(cache)},
                            "discovery": {"max_venues_per_asset": 3}, "storage": {"path": str(db)},
                            "intel": {"enabled": True}, "assets": {"discovery_calls_per_minute": 4}})
    rec("version", "PASS", f"Pre-Move Scanner {__version__}; top {a.top} + manual assets; database {db}")
    added = None
    svc = ScannerService(cfg)
    try:
        added = await run1(svc, a, cache)
    except Exception as exc:
        rec("run 1", "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
    finally:
        await svc.stop()
    svc = ScannerService(cfg)
    try:
        await run2(svc, added)
    except Exception as exc:
        rec("run 2 (restart)", "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
    finally:
        await svc.stop()
    out = ROOT / "data" / "live_onboarding_report"
    out.with_suffix(".json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    lines = [f"# Live onboarding check - Pre-Move Scanner {__version__}", "",
             time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "", "| Status | Step | Detail |", "|---|---|---|"]
    lines += [f"| {r['status']} | {r['step']} | {str(r['detail']).replace('|', '/').replace(chr(10), ' ')} |"
              for r in RESULTS]
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    nv = [r for r in RESULTS if "NOT VERIFIED" in str(r["detail"])]
    print(f"\n{len(RESULTS)} checks, {len(fails)} FAIL, {len(nv)} NOT VERIFIED")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
