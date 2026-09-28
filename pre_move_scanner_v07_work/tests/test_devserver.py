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
