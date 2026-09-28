from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .multivenue import MultiVenueScanner
from .onchain import EtherscanWatcher

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
CONFIG_PATH = ROOT / "config.json"

if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text((ROOT / "config.example.json").read_text())

config = json.loads(CONFIG_PATH.read_text())
scanner = MultiVenueScanner(config)
onchain = EtherscanWatcher(config.get("onchain", {}))

app = FastAPI(title="Pre-Move Scanner", version="0.4.0")
app.mount("/static", StaticFiles(directory=WEB), name="static")

clients: Set[WebSocket] = set()
tasks = []

@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(WEB / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def sw():
    return FileResponse(WEB / "sw.js", media_type="application/javascript")

@app.get("/api/state")
async def state():
    snap = scanner.snapshot()
    snap["onchain"] = onchain.snapshot()
    snap["server_ts"] = time.time()
    return snap

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)

async def broadcaster():
    while True:
        payload = scanner.snapshot()
        payload["onchain"] = onchain.snapshot()
        payload["server_ts"] = time.time()
        raw = json.dumps(payload)
        stale=[]
        for c in list(clients):
            try:
                await c.send_text(raw)
            except Exception:
                stale.append(c)
        for c in stale:
            clients.discard(c)
        await asyncio.sleep(config.get("sample_interval_seconds",1))

def init_db():
    con = sqlite3.connect(ROOT / "scanner.db")
    con.execute("""
      CREATE TABLE IF NOT EXISTS composite_history_v2 (
        ts REAL,
        asset TEXT,
        price REAL,
        score REAL,
        coverage INTEGER,
        confirmed_venues INTEGER,
        bid_depth_1 REAL,
        ask_depth_1 REAL,
        ask_depth_ratio REAL,
        buy_ratio_60s REAL,
        volume_60s REAL,
        volume_ratio REAL,
        price_change_5m_pct REAL,
        ask_replenishment REAL,
        spread_bps REAL,
        trade_confidence REAL,
        components_json TEXT
      )
    """)
    con.execute("""
      CREATE INDEX IF NOT EXISTS idx_composite_hist_asset_ts
      ON composite_history_v2(asset, ts)
    """)
    con.execute("""
      CREATE TABLE IF NOT EXISTS venue_history_v2 (
        ts REAL,
        asset TEXT,
        venue TEXT,
        symbol TEXT,
        confirmed INTEGER,
        flags INTEGER,
        bid_depth_1 REAL,
        ask_depth_1 REAL,
        ask_depth_ratio REAL,
        buy_ratio_60s REAL,
        volume_60s REAL,
        volume_ratio REAL,
        spread_bps REAL,
        ask_replenishment REAL,
        discovery_volume_24h_usd REAL
      )
    """)
    con.execute("""
      CREATE INDEX IF NOT EXISTS idx_venue_hist_asset_ts
      ON venue_history_v2(asset, ts)
    """)
    con.execute("""
      CREATE TABLE IF NOT EXISTS scanner_events_v2 (
        ts REAL,
        asset TEXT,
        event_type TEXT,
        level REAL,
        message TEXT
      )
    """)
    con.execute("""
      CREATE INDEX IF NOT EXISTS idx_scanner_events_asset_ts
      ON scanner_events_v2(asset, ts)
    """)
    con.commit()
    con.close()


_last_event_state = {}


async def persister():
    init_db()
    every = max(2, int(config.get("persist_interval_seconds", 5)))

    while True:
        snap = scanner.snapshot()
        composite_rows = []
        venue_rows = []
        event_rows = []

        for asset, m in snap.get("assets", {}).items():
            if m.get("score") is None:
                continue

            ts = float(m["ts"])
            composite_rows.append((
                ts, asset, m.get("price"), m.get("score"),
                m.get("coverage"), m.get("confirmed_venues"),
                m.get("bid_depth_1"), m.get("ask_depth_1"),
                m.get("ask_depth_ratio_vs_baseline"),
                m.get("buy_ratio_60s"), m.get("volume_60s"),
                m.get("volume_ratio_vs_baseline"),
                m.get("price_change_5m_pct"), m.get("ask_replenishment"),
                m.get("spread_bps"), m.get("trade_confidence"),
                json.dumps(m.get("components", {}))
            ))

            for v in m.get("venues", []):
                venue_rows.append((
                    ts, asset, v.get("venue"), v.get("symbol"),
                    1 if v.get("confirmed") else 0, v.get("flags", 0),
                    v.get("bid_depth_1"), v.get("ask_depth_1"),
                    v.get("ask_depth_ratio"), v.get("buy_ratio_60s"),
                    v.get("volume_60s"), v.get("volume_ratio"),
                    v.get("spread_bps"), v.get("ask_replenishment"),
                    v.get("volume_24h_discovery_usd")
                ))

            # Event markers: threshold crossings and confirmation-count changes.
            prev = _last_event_state.get(asset, {"score": None, "confirmed": None})
            score = float(m.get("score") or 0)
            confirmed = int(m.get("confirmed_venues") or 0)

            for threshold in (55, 70, 80):
                pscore = prev.get("score")
                if pscore is not None and pscore < threshold <= score:
                    event_rows.append((
                        ts, asset, "score_cross_up", threshold,
                        f"Score crossed above {threshold}"
                    ))
                elif pscore is not None and pscore >= threshold > score:
                    event_rows.append((
                        ts, asset, "score_cross_down", threshold,
                        f"Score fell below {threshold}"
                    ))

            pconf = prev.get("confirmed")
            if pconf is not None and confirmed != pconf:
                event_rows.append((
                    ts, asset, "venue_confirmations", confirmed,
                    f"Confirmed venues changed {pconf} → {confirmed}"
                ))

            _last_event_state[asset] = {"score": score, "confirmed": confirmed}

        con = sqlite3.connect(ROOT / "scanner.db")
        if composite_rows:
            con.executemany(
                "INSERT INTO composite_history_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                composite_rows
            )
        if venue_rows:
            con.executemany(
                "INSERT INTO venue_history_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                venue_rows
            )
        if event_rows:
            con.executemany(
                "INSERT INTO scanner_events_v2 VALUES (?,?,?,?,?)",
                event_rows
            )

        # Keep seven days of microstructure history.
        cutoff = time.time() - 7 * 86400
        con.execute("DELETE FROM composite_history_v2 WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM venue_history_v2 WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM scanner_events_v2 WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()

        await asyncio.sleep(every)


def _downsample(rows, max_points=1800):
    if len(rows) <= max_points:
        return rows
    step = max(1, len(rows) // max_points)
    sampled = rows[::step]
    if sampled[-1] != rows[-1]:
        sampled.append(rows[-1])
    return sampled


@app.get("/api/history/{asset}")
async def history(
    asset: str,
    hours: float = Query(6.0, ge=0.25, le=168.0),
    max_points: int = Query(1800, ge=200, le=5000)
):
    asset = asset.upper()
    since = time.time() - hours * 3600

    con = sqlite3.connect(ROOT / "scanner.db")
    con.row_factory = sqlite3.Row

    comp = con.execute("""
        SELECT * FROM composite_history_v2
        WHERE asset=? AND ts>=?
        ORDER BY ts ASC
    """, (asset, since)).fetchall()

    venue = con.execute("""
        SELECT * FROM venue_history_v2
        WHERE asset=? AND ts>=?
        ORDER BY ts ASC
    """, (asset, since)).fetchall()

    events = con.execute("""
        SELECT * FROM scanner_events_v2
        WHERE asset=? AND ts>=?
        ORDER BY ts ASC
    """, (asset, since)).fetchall()
    con.close()

    comp = _downsample([dict(x) for x in comp], max_points)

    # Venue data is grouped and downsampled per venue.
    by_venue = {}
    for r in venue:
        d = dict(r)
        by_venue.setdefault(d["venue"], []).append(d)
    by_venue = {
        k: _downsample(v, max(250, max_points // 2))
        for k, v in by_venue.items()
    }

    for r in comp:
        try:
            r["components"] = json.loads(r.pop("components_json") or "{}")
        except Exception:
            r["components"] = {}

    return {
        "asset": asset,
        "hours": hours,
        "composite": comp,
        "venues": by_venue,
        "events": [dict(x) for x in events],
    }


@app.on_event("startup")
async def startup():
    tasks.extend([
        asyncio.create_task(scanner.run()),
        asyncio.create_task(broadcaster()),
        asyncio.create_task(persister()),
        asyncio.create_task(onchain.run()),
    ])

@app.on_event("shutdown")
async def shutdown():
    await scanner.close()
    await onchain.close()
    for t in tasks:
        t.cancel()
