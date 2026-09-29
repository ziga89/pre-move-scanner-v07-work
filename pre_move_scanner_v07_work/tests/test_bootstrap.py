"""Clean install from a source-only unpack (the release ZIP has no .venv/, data/ or config.json):
the bootstrap creates what is missing, never overwrites what exists, a fresh database is created only
on a genuinely new install, and the application starts from the unpacked folder.

Set PMS_TEST_FULL_BOOTSTRAP=1 to also create the venv WITH pip and install requirements.txt
(needs network; CI runs it on one job)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import zipfile

from server import __version__
from server.storage.db import Database
from tests.helpers import ROOT, cfg, scratch_dir
from tools import bootstrap, make_release


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CleanInstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = scratch_dir("bootstrap")
        shutil.rmtree(cls.base, ignore_errors=True)
        cls.base.mkdir(parents=True)
        zpath, _ = make_release.build(cls.base / "dist", env={})
        cls.app = cls.base / "unpacked"
        with zipfile.ZipFile(zpath) as z:
            z.extractall(cls.app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def run_boot(self, *args):
        return subprocess.run([sys.executable, str(self.app / "tools" / "bootstrap.py"), *args],
                              cwd=str(self.app), capture_output=True, text=True, timeout=300)

    def test_1_source_only_unpack(self):
        for missing in (".venv", "data", "config.json"):
            self.assertFalse((self.app / missing).exists(), missing)
        self.assertTrue((self.app / "config.example.json").exists())

    def test_2_bootstrap_creates_config_data_and_venv(self):
        r = self.run_boot("--venv", "--no-install", "--without-pip")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("virtual environment: created", r.stdout)
        self.assertTrue(bootstrap.venv_python(self.app).exists())
        self.assertIn("config.json: created from config.example.json", r.stdout)
        self.assertEqual((self.app / "config.json").read_text(), (self.app / "config.example.json").read_text())
        self.assertTrue((self.app / "data").is_dir())
        self.assertIn("database: none yet", r.stdout)
        # a second run never overwrites what exists
        (self.app / "config.json").write_text(json.dumps({"mode": "live", "universe": {"target_size": 42}}))
        r = self.run_boot("--venv", "--no-install", "--without-pip")
        self.assertIn("virtual environment: present", r.stdout)
        self.assertIn("never overwritten", r.stdout)
        self.assertEqual(json.loads((self.app / "config.json").read_text())["universe"]["target_size"], 42)
        (self.app / "config.json").write_text((self.app / "config.example.json").read_text())

    def test_3_fresh_database_only_on_a_new_install(self):
        (self.app / "data").mkdir(exist_ok=True)
        p = self.app / "data" / "scanner_v07.db"
        if p.exists():
            p.unlink()
        db = Database(p, cfg()["storage"])
        self.assertTrue(db.new_install)
        self.assertEqual(db.applied, [1, 2, 3])
        db.insert("events", ["ts", "asset", "category", "event_type", "severity", "venue", "level", "message",
                             "evidence"], [(1.0, "QNT", "SYSTEM", "marker", 0, None, None, "kept", "{}")], mode="")
        db.flush()
        db.close()
        size = p.stat().st_size
        db = Database(p, cfg()["storage"])
        try:
            self.assertFalse(db.new_install)
            self.assertEqual(db.applied, [])
            self.assertEqual(db.read_sync(lambda c: c.execute("SELECT message FROM events").fetchone()[0]), "kept")
            self.assertGreaterEqual(p.stat().st_size, size)
        finally:
            db.close()
        r = self.run_boot("--status")
        self.assertIn("database: existing", r.stdout)
        self.assertIn(f"version {__version__}", r.stdout)

    def test_4_app_starts_from_the_unpacked_folder(self):
        port = free_port()
        env = dict(os.environ, PYTHONPATH=str(self.app))
        proc = subprocess.Popen([sys.executable, "-m", "server.devserver", "--sim", "--port", str(port)],
                                cwd=str(self.app), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            data = None
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=2) as resp:
                        data = json.loads(resp.read())
                    break
                except Exception:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.5)
            self.assertIsNotNone(data, (proc.stdout.read() if proc.poll() is not None else b"").decode(errors="replace"))
            self.assertEqual(data["version"], __version__)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
                h = json.loads(resp.read())
            self.assertEqual(h["version"], __version__)
            self.assertTrue((self.app / "data" / "scanner_sim.db").exists())       # created on first start
        finally:
            proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()

    @unittest.skipUnless(os.environ.get("PMS_TEST_FULL_BOOTSTRAP") == "1", "full venv + pip install needs network")
    def test_5_full_bootstrap_installs_requirements(self):
        shutil.rmtree(self.app / ".venv", ignore_errors=True)
        r = self.run_boot("--venv")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        py = bootstrap.venv_python(self.app)
        r = subprocess.run([str(py), "-c", "import fastapi, uvicorn, httpx, ccxt, google.protobuf; print('deps ok')"],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "deps ok", r.stderr)


class BootstrapUnitTest(unittest.TestCase):
    def test_version_is_read_without_importing(self):
        self.assertEqual(bootstrap.version(ROOT), __version__)

    def test_existing_config_never_overwritten(self):
        d = scratch_dir("bootstrap_unit")
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        (d / "config.example.json").write_text('{"a": 1}')
        (d / "config.json").write_text('{"mine": true}')
        self.assertEqual(bootstrap.ensure_config(d), "kept")
        self.assertEqual((d / "config.json").read_text(), '{"mine": true}')
        self.assertEqual(bootstrap.ensure_data_dir(d), "created")
        self.assertEqual(bootstrap.ensure_data_dir(d), "kept")
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
