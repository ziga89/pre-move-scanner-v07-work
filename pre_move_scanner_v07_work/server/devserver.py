"""Dependency-free dev server (standard library only).

Serves the same UI and JSON API as the FastAPI app (no WebSocket — the UI falls
back to polling). Useful where FastAPI/uvicorn cannot be installed, and used by
the test-suite for end-to-end UI checks.

    python -m server.devserver --sim --port 8001
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .config import deep_merge, load_config
from .service import ScannerService

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class _Runner:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.svc: Optional[ScannerService] = None
        self.ready = threading.Event()
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="scanner-loop")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def boot():
            self.svc = ScannerService(self.cfg)
            await self.svc.start()
        try:
            self.loop.run_until_complete(boot())
        except BaseException as exc:  # surface startup failures to the caller
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    def call(self, fn, *a, timeout: float = 30.0):
        async def wrap():
            r = fn(*a)
            if asyncio.iscoroutine(r):
                r = await r
            return r
        return asyncio.run_coroutine_threadsafe(wrap(), self.loop).result(timeout)

    def stop(self) -> None:
        if self.svc is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.svc.stop(), self.loop).result(30)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


def make_handler(runner: _Runner):
    class H(BaseHTTPRequestHandler):
        server_version = "PreMoveDev/0.7"

        def log_message(self, fmt, *args):  # quiet
            return

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path) -> None:
            try:
                path = path.resolve()
                if WEB.resolve() not in path.parents and path != WEB.resolve():
                    raise FileNotFoundError
                body = path.read_bytes()
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                self._json({"detail": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if path.suffix == ".js":
                ctype = "application/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
            svc = runner.svc
            try:
                if p == "/":
                    return self._file(WEB / "index.html")
                if p == "/manifest.json":
                    return self._file(WEB / "manifest.json")
                if p == "/sw.js":
                    return self._file(WEB / "sw.js")
                if p.startswith("/static/"):
                    return self._file(WEB / p[len("/static/"):])
                if p == "/api/version":
                    return self._json({"version": __version__, "mode": "sim" if svc.sim else "live", "server": "devserver"})
                if p == "/api/top":
                    return self._json(runner.call(svc.top_payload))
                if p.startswith("/api/coin/"):
                    data = runner.call(svc.coin_payload, p.rsplit("/", 1)[1])
                    return self._json(data if data is not None else {"detail": "not in universe"}, 200 if data else 404)
                if p.startswith("/api/history/"):
                    hours = min(168.0, max(0.25, float(q.get("hours", 6))))
                    return self._json(runner.call(svc.history, p.rsplit("/", 1)[1], hours,
                                                  int(q.get("max_points", 1500))))
                if p.startswith("/api/timeline/"):
                    return self._json(runner.call(svc.timeline, p.rsplit("/", 1)[1], float(q.get("hours", 24))))
                if p == "/api/health":
                    return self._json(runner.call(svc.health_payload))
                if p == "/api/universe":
                    return self._json(runner.call(svc.universe_payload))
                if p == "/api/outcomes":
                    return self._json(runner.call(svc.outcomes, float(q.get("days", 30))))
                if p == "/api/state":
                    return self._json(runner.call(svc.state_compat))
                if p == "/api/alerts":
                    return self._json(runner.call(svc.alerts_payload))
                if p == "/api/alerts/history":
                    return self._json(runner.call(svc.alert_history, min(365.0, max(1.0, float(q.get("days", 30)))),
                                                  min(5000, max(1, int(q.get("limit", 500))))))
                if p == "/api/radar":
                    return self._json(runner.call(svc.radar_payload))
                if p == "/ws":
                    return self._json({"detail": "websocket not available on the dev server; poll /api/top"}, 426)
                return self._json({"detail": "not found"}, 404)
            except Exception as exc:
                return self._json({"detail": repr(exc)}, 500)
    return H


def serve(cfg: Dict[str, Any], host: str = "127.0.0.1", port: int = 8001):
    runner = _Runner(cfg)
    runner.thread.start()
    runner.ready.wait(120)
    if runner.error:
        raise runner.error
    httpd = ThreadingHTTPServer((host, port), make_handler(runner))
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    t.start()
    return httpd, runner


def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-Move Scanner dev server (stdlib only)")
    ap.add_argument("--sim", action="store_true", help="run the synthetic SIM market world")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    a = ap.parse_args()
    cfg = load_config(ROOT)
    if a.sim:
        cfg = deep_merge(cfg, {"mode": "sim", "feeds": {"backend": "sim"}, "storage": {"path": "data/scanner_sim.db"}})
    httpd, runner = serve(cfg, a.host, a.port)
    print(f"Pre-Move Scanner dev server on http://{a.host}:{a.port}  (mode: {'sim' if a.sim else cfg.get('mode')})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        runner.stop()


if __name__ == "__main__":
    main()
