import json
import shutil
import socket
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import cfg, scratch_dir
from server.devserver import serve


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class DevServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = scratch_dir("devserver")
        c = cfg(mode="sim", feeds={"backend": "sim"}, sim={"assets": 4, "venues_per_asset": 3, "scenarios": False},
                storage={"path": str(cls.d / "dev.db")})
        cls.port = free_port()
        cls.httpd, cls.runner = serve(c, "127.0.0.1", cls.port)
        time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.runner.stop()
        cls.runner.thread.join(10)
        shutil.rmtree(cls.d, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_api_routes(self):
        for p in ("/api/top", "/api/health", "/api/universe", "/api/state", "/api/version", "/api/outcomes"):
            code, ctype, body = self.get(p)
            self.assertEqual(code, 200, p)
            self.assertIn("json", ctype)
            json.loads(body)
        top = json.loads(self.get("/api/top")[2])
        a = top["rows"][0]["asset"]
        for p in (f"/api/coin/{a}", f"/api/history/{a}?hours=1", f"/api/timeline/{a}"):
            self.assertEqual(self.get(p)[0], 200, p)

    def test_radar_and_alert_routes(self):
        radar = json.loads(self.get("/api/radar")[2])
        self.assertIn(radar["state"], ("NONE", "WATCH", "CONFIRMING", "HIGH_CONVICTION", "INVALIDATED"))
        self.assertEqual(radar["wallet"]["state"], "OFF")
        al = json.loads(self.get("/api/alerts")[2])
        self.assertIn("active", al)
        self.assertIn("radar", al)
        hist = json.loads(self.get("/api/alerts/history?days=7&limit=50")[2])
        self.assertEqual(hist["days"], 7.0)
        self.assertIsInstance(hist["alerts"], list)
        self.assertIsInstance(hist["events"], list)
        top = json.loads(self.get("/api/top")[2])
        self.assertIn("radar", top)
        self.assertEqual(top["wallet_intel"]["text"], "Wallet intel OFF")

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_v08_asset_and_wallet_routes(self):
        from server import __version__
        for p in ("/api/assets", "/api/wallet/providers", "/api/wallet/status", "/api/assets/search?q=bravo"):
            code, _, body = self.get(p)
            self.assertEqual(code, 200, p)
            json.loads(body)
        top = json.loads(self.get("/api/top")[2])
        a = top["rows"][0]["asset"]
        self.assertEqual(json.loads(self.get(f"/api/assets/{a}")[2])["symbol"], a)
        self.assertEqual(json.loads(self.get(f"/api/wallet/{a}")[2])["asset"], a)
        prov = json.loads(self.get("/api/wallet/providers")[2])
        self.assertEqual([c["label"] for c in prov["chains"]],
                         ["Ethereum / EVM", "Bitcoin", "Solana", "XRPL", "TRON", "XDC", "Hedera", "Cardano"])
        self.assertIn("Sui", prov["not_implemented"])
        # manual asset lifecycle over HTTP (SIM coin ids)
        code, body = self.req("POST", "/api/assets/manual", {"query": "delta"})
        self.assertEqual(code, 409)                                   # a ticker alone: candidates, nothing added
        self.assertEqual([c["coingecko_id"] for c in body["candidates"]], ["delta"])
        code, body = self.req("POST", "/api/assets/manual", {"coingecko_id": "delta"})
        self.assertEqual((code, body["status"]), (200, "added"))
        self.assertIn("DELTA", [m["symbol"] for m in json.loads(self.get("/api/universe")[2])["manual"]])
        self.assertEqual(self.req("DELETE", "/api/assets/manual/DELTA")[0], 200)
        self.assertEqual(self.req("DELETE", "/api/assets/manual/DELTA")[0], 404)
        self.assertEqual(self.req("POST", "/api/assets/manual", {"coingecko_id": "nope-id"})[0], 404)
        self.assertEqual(self.req("PUT", f"/api/assets/{a}/override", {"chain": "xdc", "native": True})[0], 400)  # SIM
        # one canonical version: /api/version, payloads and the service-worker cache name
        self.assertEqual(json.loads(self.get("/api/version")[2])["version"], __version__)
        sw = self.get("/sw.js")[2].decode()
        self.assertIn(f'premove-v{__version__}', sw)
        self.assertNotIn("__VERSION__", sw)
        for p in ("/api/assets/NOPE", "/api/wallet/NOPE"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(p)
            self.assertEqual(cm.exception.code, 404, p)

    def test_static_and_errors(self):
        code, ctype, body = self.get("/")
        self.assertIn(b"Pre", body)
        self.assertEqual(self.get("/static/js/main.js")[1], "application/javascript")
        for bad in ("/api/coin/NOPE", "/static/../server/config.py", "/nope"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(bad)
            self.assertEqual(cm.exception.code, 404, bad)


if __name__ == "__main__":
    unittest.main()
