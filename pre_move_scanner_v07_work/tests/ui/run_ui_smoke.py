"""Start the stdlib dev server in SIM mode and run the Playwright UI smoke test.

    python tests/ui/run_ui_smoke.py [screenshot_dir]
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.config import deep_merge, load_config  # noqa: E402
from server.devserver import serve  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".test_tmp" / "ui"
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pms_ui_"))
    cfg = load_config(ROOT, path=tmp / "none.json", create_if_missing=False)
    cfg = deep_merge(cfg, {"mode": "sim", "feeds": {"backend": "sim", "resync_grace_seconds": 5},
                           "storage": {"path": str(tmp / "ui.db")},
                           "engine": {"min_baseline_minutes": 1, "baseline_lag_minutes": 0},
                           "sim": {"cycle_seconds": 240}})
    cfg["_meta"] = {"root": str(ROOT), "warnings": []}
    port = free_port()
    httpd, runner = serve(cfg, "127.0.0.1", port)
    time.sleep(float(os.environ.get("UI_WARMUP_SECONDS", "8")))
    env = dict(os.environ)
    chromium = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    if chromium.exists() and "PW_CHROMIUM" not in env:
        env["PW_CHROMIUM"] = str(chromium)
    try:
        r = subprocess.run(["node", str(ROOT / "tests" / "ui" / "smoke.mjs"), f"http://127.0.0.1:{port}", str(shots)],
                           env=env, timeout=240)
        return r.returncode
    finally:
        httpd.shutdown()
        httpd.server_close()
        runner.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
