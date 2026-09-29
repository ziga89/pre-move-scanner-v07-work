"""FastAPI application.

Run:  uvicorn server.app:app --host 0.0.0.0 --port 8000

WebSocket /ws protocol: the client sends {"subscribe": ["top", "coin:QNT", "health"]}
(and optionally "unsubscribe"). Default subscription is "top". Each client has
a latest-wins queue, so a slow phone drops frames instead of stalling others.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import load_config
from .service import ManualAssetError, ScannerService

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class _Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.topics: Set[str] = {"top"}
        self.q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.dropped = 0

    def offer(self, msg: str) -> None:
        if self.q.full():
            try:
                self.q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.q.put_nowait(msg)


class WsHub:
    def __init__(self, svc: ScannerService, cfg: Dict[str, Any]):
        self.svc = svc
        self.cfg = cfg
        self.clients: Dict[int, _Client] = {}

    async def run(self) -> None:
        top_s = float(self.cfg["server"].get("broadcast_seconds", 1.0))
        coin_s = float(self.cfg["server"].get("coin_broadcast_seconds", 2.0))
        n = 0
        while True:
            await asyncio.sleep(top_s)
            n += 1
            if not self.clients:
                continue
            try:
                if any("top" in c.topics for c in self.clients.values()):
                    msg = json.dumps({"topic": "top", "data": self.svc.top_payload()})
                    for c in self.clients.values():
                        if "top" in c.topics:
                            c.offer(msg)
                if n % max(1, int(round(coin_s / top_s))) == 0:
                    coins = {t for c in self.clients.values() for t in c.topics if t.startswith("coin:")}
                    for t in coins:
                        data = self.svc.coin_payload(t.split(":", 1)[1])
                        if data is None:
                            continue
                        msg = json.dumps({"topic": t, "data": data})
                        for c in self.clients.values():
                            if t in c.topics:
                                c.offer(msg)
                if n % 5 == 0 and any("health" in c.topics for c in self.clients.values()):
                    msg = json.dumps({"topic": "health", "data": self.svc.health_payload()})
                    for c in self.clients.values():
                        if "health" in c.topics:
                            c.offer(msg)
            except Exception as exc:  # never let a payload bug kill broadcasting
                self.svc.status["ws_error"] = repr(exc)[:200]

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        c = _Client(ws)
        self.clients[id(ws)] = c

        async def sender():
            while True:
                msg = await c.q.get()
                await ws.send_text(msg)
        st = asyncio.create_task(sender())
        try:
            c.offer(json.dumps({"topic": "top", "data": self.svc.top_payload()}))
            while True:
                raw = await ws.receive_text()
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue  # v0.6 clients send "hello"/"ping"
                if not isinstance(m, dict):
                    continue
                for t in m.get("subscribe", []) or []:
                    c.topics.add(str(t))
                    if str(t).startswith("coin:"):
                        data = self.svc.coin_payload(str(t).split(":", 1)[1])
                        if data is not None:
                            c.offer(json.dumps({"topic": str(t), "data": data}))
                for t in m.get("unsubscribe", []) or []:
                    c.topics.discard(str(t))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            st.cancel()
            self.clients.pop(id(ws), None)


def create_app(cfg: Optional[Dict[str, Any]] = None, service: Optional[ScannerService] = None,
               start_service: bool = True) -> FastAPI:
    cfg = cfg or load_config(ROOT)
    svc = service or ScannerService(cfg)
    hub = WsHub(svc, cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if start_service:
            await svc.start()
            task = asyncio.create_task(hub.run())
        try:
            yield
        finally:
            if task:
                task.cancel()
            if start_service:
                await svc.stop()

    app = FastAPI(title="Pre-Move Scanner", version=__version__, lifespan=lifespan)
    app.state.service = svc
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    async def home():
        return FileResponse(WEB / "index.html")

    @app.get("/manifest.json")
    async def manifest():
        return FileResponse(WEB / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def sw():
        # the cache name carries the canonical version (no hand-edited cache key)
        body = (WEB / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", __version__)
        return Response(body, media_type="application/javascript")

    @app.get("/api/version")
    async def version():
        return {"version": __version__, "mode": "sim" if svc.sim else "live"}

    @app.get("/api/top")
    async def top():
        return svc.top_payload()

    @app.get("/api/coin/{asset}")
    async def coin(asset: str):
        data = svc.coin_payload(asset)
        if data is None:
            raise HTTPException(404, f"{asset.upper()} is not in the scanned universe")
        return data

    @app.get("/api/history/{asset}")
    async def history(asset: str, hours: float = Query(6.0, ge=0.25, le=168.0),
                      max_points: int = Query(1500, ge=100, le=5000)):
        return await svc.history(asset, hours, max_points)

    @app.get("/api/timeline/{asset}")
    async def timeline(asset: str, hours: float = Query(24.0, ge=0.25, le=24 * 90)):
        return await svc.timeline(asset, hours)

    @app.get("/api/health")
    async def health():
        return svc.health_payload()

    @app.get("/api/universe")
    async def universe():
        return svc.universe_payload()

    @app.get("/api/outcomes")
    async def outcomes(days: float = Query(30.0, ge=1, le=365)):
        return await svc.outcomes(days)

    @app.get("/api/alerts")
    async def alerts():
        return svc.alerts_payload()

    @app.get("/api/radar")
    async def radar():
        return svc.radar_payload()

    @app.get("/api/alerts/history")
    async def alerts_history(days: float = Query(30.0, ge=1, le=365),
                             limit: int = Query(500, ge=1, le=5000)):
        return await svc.alert_history(days, limit)

    # ---- v0.8 assets / manual assets (search and resolve before the {symbol} route)
    def _err(exc: ManualAssetError) -> JSONResponse:
        return JSONResponse(exc.payload, status_code=exc.status)

    @app.get("/api/assets")
    async def assets():
        return svc.assets_payload()

    @app.get("/api/assets/search")
    async def assets_search(q: str = Query("", max_length=80)):
        try:
            return await svc.search_assets(q)
        except ManualAssetError as exc:
            return _err(exc)

    @app.get("/api/assets/resolve/{coingecko_id}")
    async def assets_resolve(coingecko_id: str):
        try:
            return await svc.resolve_asset(coingecko_id)
        except ManualAssetError as exc:
            return _err(exc)

    @app.get("/api/assets/{symbol}")
    async def asset(symbol: str):
        data = svc.asset_payload(symbol)
        if data is None:
            raise HTTPException(404, f"{symbol.upper()} is neither monitored nor a manual asset")
        return data

    @app.post("/api/assets/manual")
    async def manual_add(body: Dict[str, Any] = Body(default={})):
        try:
            return await svc.add_manual_asset(body.get("coingecko_id"), body.get("query"))
        except ManualAssetError as exc:
            return _err(exc)

    @app.delete("/api/assets/manual/{symbol}")
    async def manual_remove(symbol: str):
        try:
            return await svc.remove_manual_asset(symbol)
        except ManualAssetError as exc:
            return _err(exc)

    @app.put("/api/assets/{symbol}/override")
    async def asset_override(symbol: str, body: Optional[Dict[str, Any]] = Body(default=None)):
        try:
            return await svc.set_asset_override(symbol, body or None)
        except ManualAssetError as exc:
            return _err(exc)

    # ---- v0.8 wallet intelligence
    @app.get("/api/wallet/providers")
    async def wallet_providers():
        return svc.wallet_providers_payload()

    @app.get("/api/wallet/status")
    async def wallet_status():
        return svc.wallet_status_payload()

    @app.get("/api/wallet/{symbol}")
    async def wallet_asset(symbol: str):
        data = svc.wallet_asset_payload(symbol)
        if data is None:
            raise HTTPException(404, f"{symbol.upper()} is not monitored")
        return data

    @app.get("/api/crosscheck/{asset}")
    async def crosscheck(asset: str):
        return {"asset": asset.upper(), "unsupported_top": await svc.crosscheck(asset.upper())}

    @app.get("/api/state")
    async def state():
        return svc.state_compat()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.serve(ws)

    return app


_app: Optional[FastAPI] = None


def __getattr__(name: str):
    # `uvicorn server.app:app` — created lazily so importing this module in
    # tests does not start anything or touch config/database files.
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
