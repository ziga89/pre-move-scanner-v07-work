"""A local fake KuCoin (REST + websocket) for driving the REAL ccxt.pro KuCoin code offline.

Implements exactly what ccxt's KuCoin spot streams use:
  POST /api/v1/bullet-public                 -> websocket token + endpoint (negotiate)
  GET  /api/v1/market/orderbook/level2_100    -> REST snapshot for /market/level2
  WS   /ws                                    -> welcome, ack, ping/pong, trade.l2update, trade.l3match
Fault injection: drop every connection, fail the next N token requests, expire tokens.
Requires aiohttp (a ccxt dependency).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Dict, Optional, Set

from aiohttp import WSMsgType, web


class FakeKucoin:
    def __init__(self, depth_interval: float = 0.02, trade_interval: float = 0.05):
        self.depth_interval = depth_interval
        self.trade_interval = trade_interval
        self.seq: Dict[str, int] = {}
        self.conns: Set[Any] = set()
        self.bullet_calls = 0
        self.snapshot_calls = 0
        self.connections_total = 0
        self.subscribes = 0
        self.fail_bullets = 0
        self.drops = 0
        self.port: Optional[int] = None
        self._runner: Optional[web.AppRunner] = None
        self._trade_id = 0
        self._rng = random.Random(1)

    # ---- lifecycle
    async def start(self) -> "FakeKucoin":
        app = web.Application()
        app.router.add_post("/api/v1/bullet-public", self._bullet)
        app.router.add_get("/api/v1/market/orderbook/level2_100", self._snapshot)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        await self.drop_all()
        if self._runner:
            await self._runner.cleanup()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- fault injection
    async def drop_all(self) -> int:
        conns = list(self.conns)
        for c in conns:
            try:
                await c["ws"].close()
            except Exception:
                pass
        self.drops += len(conns)
        return len(conns)

    async def expire_tokens(self) -> None:
        for c in list(self.conns):
            try:
                await c["ws"].send_str(json.dumps({"id": "e", "type": "error", "code": 401, "data": "token is expired"}))
                await c["ws"].close()
            except Exception:
                pass

    # ---- REST
    async def _bullet(self, request: web.Request) -> web.Response:
        self.bullet_calls += 1
        if self.fail_bullets > 0:
            self.fail_bullets -= 1
            return web.json_response({"code": "500000", "msg": "internal error (injected)"}, status=500)
        data = {"token": f"tok{self.bullet_calls}",
                "instanceServers": [{"endpoint": f"ws://127.0.0.1:{self.port}/ws", "protocol": "websocket",
                                     "encrypt": False, "pingInterval": 18000, "pingTimeout": 10000}]}
        return web.json_response({"code": "200000", "data": data})

    async def _snapshot(self, request: web.Request) -> web.Response:
        self.snapshot_calls += 1
        mid = request.query.get("symbol", "")
        seq = self.seq.setdefault(mid, 1000)
        data = {"time": int(time.time() * 1000), "sequence": str(seq),
                "bids": [["99.0", "5"], ["98.0", "5"]], "asks": [["101.0", "5"], ["102.0", "5"]]}
        return web.json_response({"code": "200000", "data": data})

    # ---- websocket
    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=True)
        await ws.prepare(request)
        conn = {"ws": ws, "topics": set(), "token": request.query.get("token")}
        key = _Conn(conn)
        self.conns.add(key)
        self.connections_total += 1
        await ws.send_str(json.dumps({"id": f"welcome{self.connections_total}", "type": "welcome"}))
        feeder = asyncio.ensure_future(self._feed(conn))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("type")
                if t == "ping":
                    await ws.send_str(json.dumps({"id": m.get("id"), "type": "pong"}))
                elif t in ("subscribe", "unsubscribe"):
                    if t == "subscribe":
                        conn["topics"].add(m["topic"])
                        self.subscribes += 1
                    else:
                        conn["topics"].discard(m["topic"])
                    if m.get("response"):
                        await ws.send_str(json.dumps({"id": m.get("id"), "type": "ack"}))
        finally:
            feeder.cancel()
            self.conns.discard(key)
        return ws

    async def _feed(self, conn: Dict[str, Any]) -> None:
        ws = conn["ws"]
        next_trade = 0.0
        try:
            while not ws.closed:
                now = time.time()
                for topic in list(conn["topics"]):
                    channel, _, ids = topic.partition(":")
                    order = ids.split(",")
                    self._rng.shuffle(order)          # real KuCoin interleaves symbols
                    for mid in order:
                        if channel == "/market/level2":
                            s = self.seq[mid] = self.seq.get(mid, 1000) + 1
                            px = 101.0 + (s % 5) * 0.1
                            data = {"changes": {"asks": [[f"{px:.1f}", "3", str(s)]], "bids": [["99.5", "2", str(s)]]},
                                    "sequenceStart": s, "sequenceEnd": s, "symbol": mid, "time": int(now * 1000)}
                            await ws.send_str(json.dumps({"type": "message", "topic": f"{channel}:{mid}",
                                                          "subject": "trade.l2update", "data": data}))
                        elif channel == "/market/match" and now >= next_trade:
                            self._trade_id += 1
                            data = {"sequence": str(self._trade_id), "symbol": mid, "side": "buy", "size": "0.5",
                                    "price": "100.1", "takerOrderId": "t", "makerOrderId": "m",
                                    "tradeId": f"id{self._trade_id}", "time": str(int(now * 1e9)), "type": "match"}
                            await ws.send_str(json.dumps({"type": "message", "topic": f"{channel}:{mid}",
                                                          "subject": "trade.l3match", "data": data}))
                if now >= next_trade:
                    next_trade = now + self.trade_interval
                await asyncio.sleep(self.depth_interval)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass


class _Conn:
    """Hashable wrapper around a connection dict."""

    def __init__(self, d: Dict[str, Any]):
        self.d = d

    def __getitem__(self, k):
        return self.d[k]


def local_kucoin_class(base_url: str):
    """ccxt.pro.kucoin subclass whose REST calls go to the fake server (websocket URL comes from it)."""
    import ccxt.pro as pro  # type: ignore

    class LocalKucoin(pro.kucoin):
        def __init__(self, config=None):
            super().__init__(config or {})
            self.urls["api"]["public"] = base_url

    return LocalKucoin


def kucoin_markets(bases=("QNT", "XDC", "LINK"), quote: str = "USDT") -> Dict[str, Dict[str, Any]]:
    """Minimal unified spot markets, as the catalog would share them via set_markets()."""
    import ccxt.pro as pro  # type: ignore
    k = pro.kucoin()
    out = {}
    for b in bases:
        out[f"{b}/{quote}"] = k.safe_market_structure({
            "id": f"{b}-{quote}", "symbol": f"{b}/{quote}", "base": b, "quote": quote, "baseId": b, "quoteId": quote,
            "type": "spot", "spot": True, "margin": False, "swap": False, "future": False, "option": False,
            "contract": False, "active": True, "precision": {"amount": 0.0001, "price": 0.0001}})
    return out
