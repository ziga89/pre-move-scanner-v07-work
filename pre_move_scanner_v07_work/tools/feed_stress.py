"""Live feed stress test: repeatedly start / stop / disrupt real exchange subscriptions.

    python tools/feed_stress.py                                   # KuCoin, QNT/XDC/LINK, 12 cycles
    python tools/feed_stress.py --exchange kucoin --assets QNT,XDC,LINK --cycles 20 --hold 20

Uses the production path: the exchange catalog is loaded over REST and its markets are
shared into every stream client (this is what exposed the KuCoin
"AttributeError: 'NoneType' object has no attribute 'create_task'"), then the real
FeedManager streams the markets. Each cycle applies one operation and then requires every
market to be STREAMING again and to deliver order books:

  start         fresh feed manager, subscribe everything
  resubscribe   drop some markets, then subscribe them again
  stop_start    unsubscribe the exchange completely, then subscribe again
  drop_sockets  close the live websocket connections underneath ccxt (server-side drop)
  rebuild       force the partition to replace its ccxt instance
  new_manager   stop the whole feed manager and create a new one

Failures are never masked: a cycle FAILs if a market does not stream again within the
timeout, delivers no books, or ANY client-level error occurs (AttributeError, TypeError,
closed / wrong-loop client). Transient disconnects caused by drop_sockets are expected and
reported. Step 0 probes the raw ccxt behaviour (shared markets without open()).
Writes data/feed_stress_report.md and data/feed_stress_report.json; exit code 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import __version__  # noqa: E402
from server.config import load_config  # noqa: E402
from server.feeds.ccxt_adapter import CcxtAdapter  # noqa: E402
from server.feeds.manager import FeedManager  # noqa: E402

OPS = ["start", "resubscribe", "stop_start", "drop_sockets", "rebuild", "new_manager"]
CLIENT_ERROR_MARKERS = ("AttributeError", "TypeError", "ExchangeClosedByUser", "StreamClientClosed",
                        "StreamClientLoopError", "create_task", "Event loop")
QUOTES = ("USDT", "USDC", "USD", "EUR", "BTC")


class Sink:
    def __init__(self):
        self.books: Dict[str, int] = {}
        self.trades: Dict[str, int] = {}
        self.events: List[Dict[str, Any]] = []

    def on_book(self, ex, sym, bids, asks, ts, resync):
        self.books[sym] = self.books.get(sym, 0) + 1

    def on_trades(self, ex, sym, trades, ts):
        self.trades[sym] = self.trades.get(sym, 0) + len(trades)

    def on_market_status(self, ex, sym, status, reason, ts):
        self.events.append({"t": round(ts, 2), "symbol": sym, "status": status, "reason": reason})


def pick_symbols(cat, assets: List[str]) -> Dict[str, str]:
    out = {}
    for a in assets:
        best = None
        for s in cat.by_base.get(a, []):
            q = str(cat.markets[s].get("quote", "")).upper()
            t = cat.tickers.get(s) or {}
            if q not in QUOTES or not t.get("last"):
                continue
            vol = float(t.get("quoteVolume") or 0.0) * (1.0 if q != "BTC" else 60000.0)
            if best is None or vol > best[1]:
                best = (s, vol)
        if best:
            out[a] = best[0]
    return out


async def raw_ccxt_probe(ad: CcxtAdapter, symbols: List[str]) -> Dict[str, Any]:
    """Step 0: the raw ccxt instance with shared markets, first WITHOUT open(), then via our client."""
    _, pro = ad._modules()
    res: Dict[str, Any] = {}
    ex = getattr(pro, ad.ccxt_id)(ad._options())
    ex.set_markets(ad._markets, ad._currencies)
    try:
        await asyncio.wait_for(ex.watch_order_book_for_symbols(symbols[:1]) if ex.has.get("watchOrderBookForSymbols")
                               else ex.watch_order_book(symbols[0]), 20)
        res["raw_without_open"] = "no error"
    except Exception as exc:
        res["raw_without_open"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        await ex.close()
    cl = ad.new_stream_client()
    try:
        ob = await asyncio.wait_for(cl.watch_book(symbols[0], None), 30)
        res["scanner_client"] = f"ok: first book for {ob.get('symbol')} ({len(ob.get('bids') or [])} bids)"
    except Exception as exc:
        res["scanner_client"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        await cl.close()
    return res


async def wait_streaming(fm: FeedManager, ex: str, symbols: List[str], sink: "Sink", books0: Dict[str, int],
                         t0: float, timeout: float) -> float:
    """Seconds from the operation until every market is STREAMING and has delivered a NEW book."""
    while time.time() - t0 < timeout:
        if all(fm.market_status.get((ex, s), {}).get("status") == "STREAMING" and sink.books.get(s, 0) > books0.get(s, 0)
               for s in symbols):
            return time.time() - t0
        await asyncio.sleep(0.05)
    return -1.0


def generations(fm: FeedManager, ex: str) -> List[int]:
    rt = fm.exchanges.get(ex)
    return [p.generation for p in rt.partitions] if rt else []


async def drop_sockets(fm: FeedManager, ex: str) -> int:
    n = 0
    rt = fm.exchanges.get(ex)
    for p in rt.partitions if rt else []:
        inst = getattr(p.client, "ex", None)
        for c in list((getattr(inst, "clients", None) or {}).values()):
            conn = getattr(c, "connection", None)
            if conn is not None and not getattr(conn, "closed", True):
                await conn.close()   # like a server-side close: ccxt sees the socket end
                n += 1
    return n


async def run(args) -> int:
    cfg = load_config(ROOT, create_if_missing=False)
    fcfg = dict(cfg["feeds"])
    ex = args.exchange
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    report: Dict[str, Any] = {"version": __version__, "exchange": ex, "platform": f"{platform.system()} {platform.release()}",
                              "python": platform.python_version(), "cycles": [], "started": time.time()}
    try:
        import ccxt  # type: ignore
        report["ccxt"] = ccxt.__version__
    except Exception as exc:
        print(f"ccxt not installed: {exc!r}")
        return 2
    ad = CcxtAdapter(ex)
    t0 = time.time()
    cat = await ad.load_catalog()
    if cat.error:
        report["catalog"] = f"FAIL: {cat.error}"
        print(f"[FAIL] catalog {ex}: {cat.error}")
        write(report)
        await ad.close()
        return 1
    report["catalog"] = f"{len(cat.markets)} spot pairs, {len(cat.tickers)} tickers in {time.time() - t0:.1f}s"
    print(f"[PASS] catalog {ex}: {report['catalog']}")
    chosen = pick_symbols(cat, assets)
    symbols = list(chosen.values())
    report["symbols"] = chosen
    missing = [a for a in assets if a not in chosen]
    if missing:
        print(f"[WARN] not listed on {ex}: {missing}")
    if not symbols:
        write(report)
        await ad.close()
        return 1
    report["probe"] = await raw_ccxt_probe(ad, symbols)
    print(f"[INFO] 0 raw ccxt, shared markets, no open(): {report['probe']['raw_without_open']}")
    print(f"[INFO] 0 scanner stream client:               {report['probe']['scanner_client']}")

    rng = random.Random(args.seed)
    sink = Sink()
    fm = FeedManager({ex: ad}, sink, fcfg)
    failures = 0
    for i in range(args.cycles):
        op = OPS[i % len(OPS)]
        c: Dict[str, Any] = {"cycle": i + 1, "op": op}
        ev0 = len(sink.events)
        gen0 = generations(fm, ex)
        t_op = time.time()
        if op == "start":
            await fm.set_desired({ex: symbols})
        elif op == "resubscribe":
            keep = rng.sample(symbols, max(1, len(symbols) - 1))
            await fm.set_desired({ex: keep})
            await asyncio.sleep(2.0)
            await fm.set_desired({ex: symbols})
        elif op == "stop_start":
            await fm.set_desired({})
            await asyncio.sleep(1.0)
            await fm.set_desired({ex: symbols})
        elif op == "drop_sockets":
            await fm.set_desired({ex: symbols})
            c["sockets_closed"] = await drop_sockets(fm, ex)
        elif op == "rebuild":
            await fm.set_desired({ex: symbols})
            gen0 = generations(fm, ex)
            for p in fm.exchanges[ex].partitions:
                p._restart.set()
            t_wait = time.time()   # the supervisor replaces the ccxt instance at its next watchdog tick
            while generations(fm, ex) == gen0 and time.time() - t_wait < fm.watchdog_interval + 15:
                await asyncio.sleep(0.05)
        elif op == "new_manager":
            await fm.stop()
            fm = FeedManager({ex: ad}, sink, fcfg)
            await fm.set_desired({ex: symbols})
        books_at_op = dict(sink.books)
        c["recovery_s"] = round(await wait_streaming(fm, ex, symbols, sink, books_at_op, t_op, args.timeout), 1)
        c["generations"] = f"{gen0} -> {generations(fm, ex)}"
        b0 = dict(sink.books)
        tr0 = dict(sink.trades)
        await asyncio.sleep(args.hold)
        c["books"] = {s: sink.books.get(s, 0) - b0.get(s, 0) for s in symbols}
        c["trades"] = {s: sink.trades.get(s, 0) - tr0.get(s, 0) for s in symbols}
        h = fm.health().get(ex, {})
        parts = h.get("partitions", [])
        c["health"] = {k: h.get(k) for k in ("state", "streaming", "markets", "errors", "reconnects")}
        c["rebuilds"] = sum(p.get("rebuilds", 0) for p in parts)
        c["breaker_trips"] = sum(p.get("breaker_trips", 0) for p in parts)
        c["last_errors"] = sorted({p["last_error"] for p in parts if p.get("last_error")})
        c["incidents"] = [e for e in sink.events[ev0:] if e["status"] not in ("SUBSCRIBING", "STREAMING")]
        texts = [e["reason"] for e in c["incidents"]] + c["last_errors"]
        client_errors = [t for t in texts if any(m in t for m in CLIENT_ERROR_MARKERS)]
        c["client_errors"] = client_errors[:5]
        problems = []
        if c["recovery_s"] < 0:
            problems.append(f"not all markets STREAMING within {args.timeout:.0f}s")
        silent = [s for s, n in c["books"].items() if n == 0]
        if silent:
            problems.append(f"no order books during hold: {silent}")
        if client_errors:
            problems.append(f"client-level errors: {client_errors[:2]}")
        if op == "rebuild" and generations(fm, ex) == gen0:
            problems.append("partition did not replace its ccxt instance")
        expected_incident = op in ("drop_sockets",)
        if problems:
            c["result"] = "FAIL"
            failures += 1
        elif c["incidents"] and not expected_incident:
            c["result"] = "WARN"
        else:
            c["result"] = "PASS"
        c["problems"] = problems
        c["elapsed_s"] = round(time.time() - t_op, 1)
        report["cycles"].append(c)
        inc = f", incidents {len(c['incidents'])}" + (" (expected: sockets dropped)" if expected_incident else "")
        print(f"[{c['result']}] cycle {i + 1:2d} {op:12s} streaming again after {c['recovery_s']}s; "
              f"books {sum(c['books'].values())}, trades {sum(c['trades'].values())}, rebuilds {c['rebuilds']}, "
              f"breaker trips {c['breaker_trips']}, generations {c['generations']}{inc}" + (f" | {'; '.join(problems)}" if problems else ""), flush=True)
    await fm.stop()
    await ad.close()
    report["failures"] = failures
    report["result"] = "FAIL" if failures else "PASS"
    report["finished"] = time.time()
    write(report)
    counts = {r: sum(1 for c in report["cycles"] if c["result"] == r) for r in ("PASS", "WARN", "FAIL")}
    print(f"\n{ex}: {counts['PASS']} PASS, {counts['WARN']} WARN, {counts['FAIL']} FAIL of {len(report['cycles'])} cycles "
          f"-> {report['result']}. Report: data/feed_stress_report.md")
    return 1 if failures else 0


def write(report: Dict[str, Any]) -> None:
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    (out / "feed_stress_report.json").write_text(json.dumps(report, indent=1, default=str))
    lines = [f"# Feed stress test: {report['exchange']}", "",
             f"{time.strftime('%Y-%m-%d %H:%M:%S')} · {report.get('platform')} · Python {report.get('python')} · "
             f"ccxt {report.get('ccxt')} · scanner v{report.get('version')}", "",
             f"Catalog: {report.get('catalog')}  ", f"Symbols: {report.get('symbols')}  ",
             f"Raw ccxt with shared markets, no open(): `{(report.get('probe') or {}).get('raw_without_open')}`  ",
             f"Scanner stream client: `{(report.get('probe') or {}).get('scanner_client')}`", "",
             f"**Result: {report.get('result', 'INCOMPLETE')}**", "",
             "| # | Operation | Result | New books on every market after | Books (hold) | Trades (hold) | Client generations | Error rebuilds | Breaker trips | Incidents | Problems |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in report.get("cycles", []):
        lines.append(f"| {c['cycle']} | {c['op']} | {c['result']} | {c['recovery_s']} s | {sum(c['books'].values())} | "
                     f"{sum(c['trades'].values())} | {c['generations']} | {c['rebuilds']} | {c['breaker_trips']} | {len(c['incidents'])} | "
                     f"{'; '.join(c['problems']).replace('|', '/')} |")
    (out / "feed_stress_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exchange", default="kucoin")
    ap.add_argument("--assets", default="QNT,XDC,LINK")
    ap.add_argument("--cycles", type=int, default=12)
    ap.add_argument("--hold", type=float, default=15.0, help="seconds of streaming measured per cycle")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds allowed to get every market streaming again")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    sys.exit(asyncio.run(run(a)))


if __name__ == "__main__":
    main()
