"""Live self-test — run this on the machine that will run the scanner.

    python tools/selftest.py                       # full check (~2–4 minutes)
    python tools/selftest.py --assets QNT,XDC,LINK,BTC,ETH --seconds 90
    python tools/selftest.py --quick               # skip websocket streaming
    python tools/selftest.py --save-fixtures       # also save live snapshots for offline tests

Checks, in order (each PASS / WARN / FAIL):
  1  Python + packages (fastapi, uvicorn, httpx, ccxt)
  2  config.json loads (and what was mapped from v0.6)
  3  SQLite: create, WAL mode, write/read (temporary file)
  4  CoinGecko: top-500 markets, exclusion categories
  5  exchange catalogs: load_markets + fetch_tickers per exchange
  6  capability matrix: ccxt websocket support per exchange -> modes used
  7  Top-100 universe build (back-fill past rank 100, exclusions, pinned)
  8  per-coin venue discovery for example assets (XDC must not get Binance, etc.)
  9  realtime: stream the selected markets through the real feed manager +
     engine; books/trades per market, first-book latency, errors
  10 Etherscan (only if intel is enabled / a key is set)
Writes data/selftest_report.md and data/selftest_report.json.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import platform
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import __version__  # noqa: E402
from server.config import load_config, resolve_path  # noqa: E402

RESULTS: List[Dict[str, Any]] = []


def rec(step: str, status: str, detail: str = "", data: Any = None) -> None:
    RESULTS.append({"step": step, "status": status, "detail": detail, "data": data})
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print(f"[{mark}] {step}: {detail}", flush=True)


def check_packages() -> None:
    rec("1 Python", "PASS" if sys.version_info >= (3, 10) else "FAIL",
        f"{platform.python_version()} on {platform.system()} {platform.release()}")
    for mod, critical in (("fastapi", True), ("uvicorn", True), ("httpx", False), ("ccxt", True), ("websockets", False)):
        try:
            m = importlib.import_module(mod)
            rec(f"1 package {mod}", "PASS", getattr(m, "__version__", "installed"))
        except Exception as exc:
            rec(f"1 package {mod}", "FAIL" if critical else "WARN", f"not importable: {exc!r}")


def check_db(cfg) -> None:
    from server.storage.db import Database
    p = resolve_path(cfg, "data/selftest.db")
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    try:
        db = Database(p, cfg["storage"])
        db.insert("events", ["ts", "asset", "category", "event_type", "severity", "venue", "level", "message", "evidence"],
                  [(time.time(), "SELFTEST", "SYSTEM", "selftest", 0, None, None, "ok", "{}")], mode="")
        db.flush()
        n = db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        mode = db.read_sync(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        db.close()
        rec("3 SQLite", "PASS" if n == 1 and mode.lower() == "wal" else "FAIL", f"write/read ok, journal_mode={mode}")
    except Exception as exc:
        rec("3 SQLite", "FAIL", repr(exc))
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)


async def run(args) -> int:
    cfg = load_config(ROOT)
    rec("2 config", "PASS", f"{cfg['_meta']['path']}; " + ("; ".join(cfg["_meta"]["warnings"]) or "no warnings"))
    check_db(cfg)
    from server.feeds.capabilities import resolve_caps
    from server.feeds.ccxt_adapter import CcxtAdapter, ccxt_available
    from server.universe.coingecko import CoinGeckoClient
    from server.universe.fx import FxService
    from server.universe.http import HttpClient
    from server.universe.universe import UniverseManager
    from server.universe.venues import AssetInfo, VenueSelector

    http = HttpClient()
    cg = CoinGeckoClient(http, cfg["universe"])
    rows: List[Dict[str, Any]] = []
    cats: Dict[str, set] = {}
    if args.sim:
        return await run_sim(cfg, args, http)
    try:
        for page in (1, 2):
            rows += await cg.markets(page=page, per_page=250)
        rec("4 CoinGecko markets", "PASS" if len(rows) >= 400 else "WARN", f"{len(rows)} coins (top 500 requested)")
        for c in cfg["universe"]["exclude_categories"]:
            try:
                cats[c] = {r["id"] for r in await cg.markets(category=c, per_page=250)}
            except Exception as exc:
                rec(f"4 CoinGecko category {c}", "WARN", repr(exc))
        rec("4 CoinGecko categories", "PASS" if cats else "WARN",
            ", ".join(f"{k}={len(v)}" for k, v in cats.items()) or "none (static exclusion lists will be used)")
    except Exception as exc:
        rec("4 CoinGecko markets", "FAIL", f"{exc!r} - check internet access / add a free Demo API key (COINGECKO_API_KEY)")

    if not ccxt_available():
        rec("5 exchange catalogs", "FAIL", "ccxt not installed (pip install -r requirements.txt)")
        await http.close()
        return finish()
    adapters = {ex: CcxtAdapter(ex) for ex in cfg["discovery"]["exchanges"]}
    catalogs = {}

    async def load(ex, ad):
        t0 = time.time()
        cat = await ad.load_catalog()
        return ex, cat, time.time() - t0
    for ex, cat, dt in await asyncio.gather(*(load(ex, ad) for ex, ad in adapters.items())):
        catalogs[ex] = cat
        if cat.error:
            rec(f"5 catalog {ex}", "WARN", f"{cat.error} ({dt:.1f}s)")
        else:
            rec(f"5 catalog {ex}", "PASS", f"{len(cat.markets)} spot pairs, {len(cat.tickers)} tickers in {dt:.1f}s")
    good = {k: c for k, c in catalogs.items() if not c.error}
    caps_rows = []
    for ex, ad in adapters.items():
        try:
            has = ad.has()
            caps = resolve_caps(ex, has, cfg["feeds"].get("exchange_overrides"))
            caps_rows.append(caps.to_dict())
            st = "PASS" if caps.book_mode != "none" else "WARN"
            rec(f"6 capabilities {ex}", st, f"book={caps.book_mode} trades={caps.trade_mode} "
                f"<={caps.max_symbols_per_connection} symbols/conn, {caps.max_symbols_per_call}/call; ccxt has={caps.runtime_has}")
        except Exception as exc:
            rec(f"6 capabilities {ex}", "FAIL", repr(exc))

    fx = FxService()
    fx.update_from_markets(rows)
    fx.update_from_catalogs(good.values())
    vs = VenueSelector(cfg["discovery"])
    now = time.time()
    if rows:
        def usable(r):
            s = vs.preview(AssetInfo.from_row(r), good.values(), fx, now)
            ok = bool(s.selected) and s.total_volume() >= float(cfg["universe"]["min_usable_volume_usd"])
            return ok, "" if ok else "no usable realtime venue / insufficient volume"
        u = UniverseManager(cfg["universe"]).build(rows, cats, usable, [], now)
        excl: Dict[str, int] = {}
        for e in u["excluded"]:
            k = e["reason"].split(" (")[0].split(":")[0]
            excl[k] = excl.get(k, 0) + 1
        rec("7 universe", "PASS" if len(u["members"]) == cfg["universe"]["target_size"] else "WARN",
            f"{len(u['members'])} coins; cutoff rank {u['cutoff_rank']}; excluded: {excl}; pinned: "
            + ", ".join(f"{p.get('symbol', '').upper()}={p.get('status')}" for p in u["pinned"]),
            data={"members": [m["symbol"].upper() for m in u["members"]], "excluded": u["excluded"][:80]})
    by_sym = {}
    for r in rows:
        by_sym.setdefault(str(r.get("symbol", "")).upper(), r)
    examples = [s.strip().upper() for s in args.assets.split(",") if s.strip()]
    selections = {}
    for sym in examples:
        r = by_sym.get(sym)
        if r is None:
            rec(f"8 venues {sym}", "WARN", "not in CoinGecko top 500 (add universe.coingecko_ids to pin it)")
            continue
        sel = vs.select(AssetInfo.from_row(r), good.values(), fx, now)
        selections[sym] = sel
        listed = [ex for ex, c in good.items() if c.by_base.get(sym)]
        wrong = [m for m in sel.selected if m["exchange"] not in listed]
        detail = "; ".join(f"{m['exchange']} {m['symbol']} ${m['volume_24h_usd']:,.0f}" for m in sel.selected)
        rej = {}
        for x in sel.rejected:
            rej[x["reason"].split(":")[0]] = rej.get(x["reason"].split(":")[0], 0) + 1
        rec(f"8 venues {sym}", "FAIL" if wrong else ("PASS" if sel.selected else "WARN"),
            (detail or "no usable venue") + (f" | rejected: {rej}" if rej else "") +
            (f" | not listed on: {', '.join(sorted(set(good) - set(listed)))}" if len(listed) < len(good) else ""))

    if not args.quick:
        only = [x.strip().lower() for x in args.exchanges.split(",") if x.strip()]
        markets = stream_markets(selections, good, fx, examples, only)
        await stream_check(cfg, adapters, markets, fx, args.seconds, args.repeat)
    if cfg["intel"].get("enabled") or os.getenv(cfg["intel"].get("etherscan_api_key_env", "ETHERSCAN_API_KEY")):
        await etherscan_check(cfg, http)
    else:
        rec("10 Etherscan", "SKIP", "wallet intelligence not enabled and no API key set")
    if args.save_fixtures and rows:
        out = ROOT / "tests" / "fixtures" / "live"
        out.mkdir(parents=True, exist_ok=True)
        (out / "coingecko_markets.json").write_text(json.dumps(rows))
        (out / "exchange_catalogs.json").write_text(json.dumps([c.to_dict() for c in good.values()]))
        rec("fixtures", "PASS", f"saved to {out}")
    for ad in adapters.values():
        await ad.close()
    await http.close()
    return finish()


async def run_sim(cfg, args, http) -> int:
    """Same checks against the synthetic SIM exchanges — validates this script offline."""
    from server.feeds.capabilities import resolve_caps
    from server.feeds.registry import build_adapters
    from server.universe.fx import FxService
    from server.universe.universe import UniverseManager
    from server.universe.venues import AssetInfo, VenueSelector
    cfg = dict(cfg, mode="sim")
    adapters, driver = build_adapters(cfg)
    dtask = asyncio.ensure_future(driver.run())
    rows = [{"id": a.lower(), "symbol": a.lower(), "name": a, "market_cap_rank": i + 1,
             "current_price": driver.world.markets_for(a)[0].mid(), "market_cap": 1e9 / (i + 1)}
            for i, a in enumerate(driver.world.assets())]
    rec("4 CoinGecko markets", "SKIP", f"--sim: {len(rows)} synthetic coins")
    good = {ex: await ad.load_catalog() for ex, ad in adapters.items()}
    for ex, c in good.items():
        rec(f"5 catalog {ex}", "PASS", f"{len(c.markets)} spot pairs")
        caps = resolve_caps(ex, adapters[ex].has())
        rec(f"6 capabilities {ex}", "PASS", f"book={caps.book_mode} trades={caps.trade_mode}")
    fx = FxService()
    vs = VenueSelector(cfg["discovery"])
    now = time.time()
    u = UniverseManager(dict(cfg["universe"], pinned_assets=[])).build(
        rows, {}, lambda r: (bool(vs.preview(AssetInfo.from_row(r), good.values(), fx, now).selected), ""), [], now)
    rec("7 universe", "PASS" if u["members"] else "FAIL", f"{len(u['members'])} coins")
    selections = {}
    for r in rows[:3]:
        sel = vs.select(AssetInfo.from_row(r), good.values(), fx, now)
        selections[r["symbol"].upper()] = sel
        rec(f"8 venues {r['symbol'].upper()}", "PASS" if sel.selected else "FAIL",
            "; ".join(f"{m['exchange']} {m['symbol']}" for m in sel.selected))
    if not args.quick:
        markets = stream_markets(selections, good, fx, list(selections), [])
        await stream_check(cfg, adapters, markets, fx, min(args.seconds, 10.0), args.repeat)
    dtask.cancel()
    await http.close()
    return finish()


INCIDENT_STATES = ("DISCONNECTED", "RECONNECTING", "CIRCUIT_OPEN", "UNAVAILABLE")
RANK = {"PASS": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}


def stream_markets(selections, good, fx, examples, only) -> List[tuple]:
    """(asset, exchange, symbol, quote, volume_usd, why) to stream.

    The per-coin venue selection, restricted to `only` exchanges when given. With `only`, each
    example asset's best pair on each listed exchange is added even when that exchange is not
    among the coin's top venues (or CoinGecko was unavailable), so the exchange is always tested.
    """
    out = []
    for sym, sel in selections.items():
        for m in sel.selected:
            if not only or m["exchange"] in only:
                out.append((sym, m["exchange"], m["symbol"], m["quote"], m["volume_24h_usd"], "selected"))
    have = {(a, e) for a, e, *_ in out}
    for sym in examples if only else []:
        for ex in only:
            cat = good.get(ex)
            if cat is None or (sym, ex) in have:
                continue
            best = None
            for s in cat.by_base.get(sym, []):
                q = str(cat.markets[s].get("quote", "")).upper()
                rate, t = fx.rate(q), cat.tickers.get(s) or {}
                if not rate or not t.get("last"):
                    continue
                vol = float(t.get("quoteVolume") or 0.0) * rate
                if best is None or vol > best[4]:
                    best = (sym, ex, s, q, round(vol, 2), "forced")
            if best:
                out.append(best)
            else:
                rec(f"9 stream {ex} {sym}", "WARN", f"{sym} is not listed on {ex} (or has no USD-convertible ticker)")
    return out


async def stream_check(cfg, adapters, markets, fx, seconds: float, repeat: int = 1) -> None:
    if not markets:
        rec("9 realtime", "WARN", "no markets to stream")
        return
    results = []
    for rnd in range(1, max(1, repeat) + 1):
        if repeat > 1:
            print(f"... round {rnd}/{repeat}", flush=True)
        results.append(await stream_round(cfg, adapters, markets, fx, seconds, "9" if repeat <= 1 else f"9.{rnd}"))
    if repeat > 1:
        worst = max(results, key=RANK.get)
        rec("9 repeat summary", worst, f"{results.count('PASS')}/{len(results)} rounds clean (every market streaming, "
            f"no feed errors); rounds: {', '.join(results)}")


async def stream_round(cfg, adapters, markets, fx, seconds: float, tag: str) -> str:
    """Stream the markets through the real feed manager + engine. Every feed incident is recorded:
    a market PASSes only if it streamed books and trades without a single incident."""
    from server.engine.host import LocalEngineHost, MarketSpec
    specs = [MarketSpec(a, ex, s, q, v) for a, ex, s, q, v, _ in markets]
    why = {(ex, s): w for _, ex, s, _, _, w in markets}
    host = LocalEngineHost(cfg, adapters, fx.rate)
    stats: Dict[tuple, Dict[str, Any]] = {(s.exchange, s.symbol): {"books": 0, "trades": 0, "first_book": None, "asset": s.asset,
                                                                    "status": [], "incidents": []} for s in specs}
    t0 = time.time()
    orig_book, orig_trades = host.on_book, host.on_trades
    orig_status = host.feeds._orig_sink_status   # the feed manager binds its status sink at construction

    def on_book(ex, sym, b, a, ts, resync):
        st = stats.get((ex, sym))
        if st is not None:
            st["books"] += 1
            st["first_book"] = st["first_book"] or round(ts - t0, 1)
        orig_book(ex, sym, b, a, ts, resync)

    def on_trades(ex, sym, trades, ts):
        st = stats.get((ex, sym))
        if st is not None:
            st["trades"] += len(trades)
        orig_trades(ex, sym, trades, ts)

    def on_status(ex, sym, status, reason, ts):
        st = stats.get((ex, sym))
        if st is not None:
            if not st["status"] or st["status"][-1] != status:
                st["status"].append(status)
            if status in INCIDENT_STATES:
                st["incidents"].append(f"{status}: {reason}"[:200])
        orig_status(ex, sym, status, reason, ts)
    host.on_book, host.on_trades = on_book, on_trades
    host.feeds._orig_sink_status = on_status
    await host.set_markets(specs)
    print(f"... streaming {len(specs)} markets for {seconds:.0f}s", flush=True)
    await asyncio.sleep(seconds)
    feats = host.tick(time.time())
    health = host.feeds.health()
    await host.stop()
    for (ex, sym), st in stats.items():
        f = next((x for x in feats.get(st["asset"], []) if x["exchange"] == ex and x["symbol"] == sym), {})
        status = "FAIL" if not st["books"] else ("WARN" if st["incidents"] or not st["trades"] else "PASS")
        inc = ""
        if st["incidents"]:
            inc = f"; INCIDENTS {len(st['incidents'])}: " + " | ".join(list(dict.fromkeys(st["incidents"]))[:3])
        rec(f"{tag} stream {ex} {sym}", status,
            f"books {st['books']}, trades {st['trades']}, first book after {st['first_book']}s, "
            f"state {f.get('state')}, depth +/-1% ${(f.get('bid_depth_1') or 0):,.0f}/${(f.get('ask_depth_1') or 0):,.0f}, "
            f"book coverage {f.get('book_coverage_pct')}%, statuses {st['status'][-4:]}{inc}"
            + (" [added by --exchanges; not a top venue]" if why.get((ex, sym)) == "forced" else ""))
    worst, bad_ex = "PASS", []
    for ex in sorted({k[0] for k in stats}):
        mk = [st for k, st in stats.items() if k[0] == ex]
        n_ok = sum(1 for st in mk if st["books"])
        h = health.get(ex, {})
        parts = h.get("partitions", [])
        errs, incidents = int(h.get("errors", 0)), sum(len(st["incidents"]) for st in mk)
        last = "; ".join(sorted({p["last_error"] for p in parts if p.get("last_error")}))[:300]
        status = "FAIL" if n_ok < len(mk) else ("WARN" if errs or incidents else "PASS")
        rec(f"{tag} exchange {ex}", status,
            f"{n_ok}/{len(mk)} markets streaming; feed errors {errs}, incidents {incidents}, "
            f"client rebuilds {sum(p.get('rebuilds', 0) for p in parts)}, "
            f"breaker trips {sum(p.get('breaker_trips', 0) for p in parts)}, reconnects {h.get('reconnects', 0)}"
            + (f"; last error: {last}" if last else ""), data={"health": h})
        if status != "PASS":
            bad_ex.append(f"{ex}={status}")
        worst = max(worst, status, key=RANK.get)
    ok = sum(1 for st in stats.values() if st["books"])
    rec(f"{tag} realtime summary", worst, f"{ok}/{len(stats)} markets delivered order books"
        + (f"; not clean: {', '.join(bad_ex)}" if bad_ex else "; no feed errors"))
    return worst


async def etherscan_check(cfg, http) -> None:
    from server.intel.etherscan import EtherscanClient
    c = EtherscanClient(http, cfg["intel"])
    tok = (cfg["intel"].get("tokens") or {}).get("QNT") or next(iter((cfg["intel"].get("tokens") or {}).values()), None)
    if not tok:
        rec("10 Etherscan", "WARN", "no tokens configured in intel.tokens")
        return
    try:
        rows = await c.tokentx(tok.get("chain", "ethereum"), contract=tok["contract"], offset=5, startblock=0)
        sym = rows[0].get("tokenSymbol") if rows else None
        rec("10 Etherscan", "PASS", f"tokentx ok ({len(rows)} rows, token symbol {sym}); calls today {c.used_today()}")
    except Exception as exc:
        rec("10 Etherscan", "FAIL", repr(exc))


def finish() -> int:
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    (out / "selftest_report.json").write_text(json.dumps({"version": __version__, "ts": time.time(),
                                                          "results": RESULTS}, indent=1, default=str))
    lines = [f"# Pre-Move Scanner v{__version__} self-test", "",
             f"{time.strftime('%Y-%m-%d %H:%M:%S')} · {platform.system()} · Python {platform.python_version()}", "",
             f"**{len(fails)} FAIL · {len(warns)} WARN · {sum(r['status'] == 'PASS' for r in RESULTS)} PASS**", "",
             "| Status | Step | Detail |", "|---|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['status']} | {r['step']} | {str(r['detail']).replace('|', '/')} |")
    (out / "selftest_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{len(fails)} FAIL, {len(warns)} WARN. Report: {out / 'selftest_report.md'}")
    return 1 if fails else 0


def main() -> None:
    try:  # Windows consoles (cp1252) cannot print every character; never crash on output
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="QNT,XDC,LINK,BTC,ETH,SOL,HBAR")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--save-fixtures", action="store_true")
    ap.add_argument("--repeat", type=int, default=1, help="repeat the realtime streaming check N times (fresh feed manager each round)")
    ap.add_argument("--exchanges", default="", help="stream only markets on these exchanges, e.g. kucoin (adds each asset's "
                    "best pair there even if it is not a top venue)")
    ap.add_argument("--sim", action="store_true", help="run against the synthetic SIM exchanges (offline check of this tool)")
    a = ap.parse_args()
    check_packages()
    try:
        code = asyncio.run(run(a))
    except Exception:
        traceback.print_exc()
        rec("selftest", "FAIL", "unexpected error (traceback above)")
        code = finish()
    sys.exit(code)


if __name__ == "__main__":
    main()
