"""FastAPI route + WebSocket tests. Skipped when fastapi/httpx are not installed
(they run in CI, where requirements are installed)."""
import shutil
import unittest

from tests.helpers import cfg, scratch_dir

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:  # pragma: no cover
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi / httpx not installed")
class FastApiTest(unittest.TestCase):
    def test_routes_and_websocket(self):
        from server.app import create_app
        d = scratch_dir("fastapi")
        c = cfg(mode="sim", feeds={"backend": "sim"}, sim={"assets": 4, "venues_per_asset": 3, "scenarios": False},
                storage={"path": str(d / "api.db")})
        app = create_app(c)
        try:
            with TestClient(app) as client:
                import time
                time.sleep(3)
                r = client.get("/api/top")
                self.assertEqual(r.status_code, 200)
                rows = r.json()["rows"]
                self.assertEqual(len(rows), 4)
                a = rows[0]["asset"]
                for p in (f"/api/coin/{a}", f"/api/history/{a}?hours=1", f"/api/timeline/{a}", "/api/health",
                          "/api/universe", "/api/state", "/api/outcomes", "/api/alerts", "/api/alerts/history",
                          "/api/alerts/history?days=7&limit=10", "/api/radar",
                          "/api/version", "/", "/static/js/main.js",
                          "/manifest.json", "/sw.js"):
                    self.assertEqual(client.get(p).status_code, 200, p)
                self.assertIn("state", client.get("/api/radar").json())
                # v0.8 routes
                for p in ("/api/assets", f"/api/assets/{a}", "/api/assets/search?q=bravo", "/api/wallet/providers",
                          "/api/wallet/status", f"/api/wallet/{a}"):
                    self.assertEqual(client.get(p).status_code, 200, p)
                self.assertEqual(client.post("/api/assets/manual", json={"query": "delta"}).status_code, 409)
                r = client.post("/api/assets/manual", json={"coingecko_id": "delta"})
                self.assertEqual((r.status_code, r.json()["status"]), (200, "added"))
                self.assertEqual(client.delete("/api/assets/manual/DELTA").status_code, 200)
                self.assertEqual(client.delete("/api/assets/manual/DELTA").status_code, 404)
                self.assertEqual(client.get("/api/assets/NOPE").status_code, 404)
                from server import __version__
                self.assertIn(f"premove-v{__version__}", client.get("/sw.js").text)
                self.assertEqual(client.get("/api/version").json()["version"], __version__)
                self.assertIn("alerts", client.get("/api/alerts/history").json())
                self.assertEqual(client.get("/api/coin/NOPE").status_code, 404)
                self.assertEqual(client.get("/api/history/QNT?hours=1000").status_code, 422)
                with client.websocket_connect("/ws") as ws:
                    m = ws.receive_json()
                    self.assertEqual(m["topic"], "top")
                    ws.send_text("hello")                       # v0.6 client greeting is tolerated
                    ws.send_json({"subscribe": [f"coin:{a}"]})
                    seen = set()
                    for _ in range(10):
                        seen.add(ws.receive_json()["topic"])
                        if f"coin:{a}" in seen:
                            break
                    self.assertIn(f"coin:{a}", seen)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
