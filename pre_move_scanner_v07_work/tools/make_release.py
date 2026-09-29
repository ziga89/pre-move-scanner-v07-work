"""Build a CLEAN release ZIP - source only - and verify it.

    python tools/make_release.py                       -> dist/pre_move_scanner_v<version>.zip
    python tools/make_release.py --out C:\\releases
    python tools/make_release.py --verify path\\to.zip  verify an existing archive only

A release can be unpacked over an existing installation: it contains no runtime or user state, so it
can never overwrite data/scanner_v07.db (baselines, history, alerts, outcomes), config.json or .venv.

Included: an allow-list of source paths (server/, web/, tools/, tests/, labels/, docs/, ios/ and the
top-level project files). Never included, and the archive is rejected if any appears:
  data/ · .venv/ venv/ · config.json config.backup.json · *.db *.db-wal *.db-shm *.sqlite* · any file
  with the SQLite header · .env *.pem *.key secrets* · __pycache__/ *.pyc · node_modules/ · dist/ ·
  caches, reports and screenshots · the value of any API-key environment variable · CoinGecko-style keys.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INCLUDE_DIRS = ["server", "web", "tools", "tests", "labels", "docs", "ios"]
INCLUDE_FILES = ["README.md", "CHANGELOG.md", "config.example.json", "requirements.txt", "requirements-dev.txt",
                 "run_windows.bat", "run.sh", ".gitattributes", "eslint.config.mjs"]
# directory names that are never part of a release, at any depth
PROHIBITED_DIRS = {"data", ".venv", "venv", "__pycache__", "node_modules", "dist", ".test_tmp", "ui-screenshots",
                   ".pytest_cache", ".mypy_cache", "backups", ".git"}
PROHIBITED_NAMES = {"config.json", "config.backup.json", "universe_cache.json", "venue_discovery_cache.json", ".env"}
PROHIBITED_PATTERNS = [re.compile(p, re.I) for p in (
    r"\.db$", r"\.db-wal$", r"\.db-shm$", r"\.db-journal$", r"\.sqlite\d?$", r"\.sqlite\d?-(wal|shm)$", r"\.pyc$",
    r"\.pem$", r"\.key$", r"^secrets?(\.|$)", r"selftest_report", r"wallet_check_report", r"feed_stress_.*\.(md|json)$",
    r"\.env(\.|$)")]
# environment variables whose VALUES must never appear in a release
SECRET_ENV = ("ETHERSCAN_API_KEY", "COINGECKO_API_KEY", "TRONGRID_API_KEY", "KOIOS_API_TOKEN", "SOLANA_RPC_URL")
SECRET_PATTERNS = [re.compile(p) for p in (r"CG-[A-Za-z0-9]{20,}", r"(?i)apikey=[A-Za-z0-9]{16,}")]
SQLITE_MAGIC = b"SQLite format 3\x00"


def prohibited(rel: str) -> Optional[str]:
    """Why a path may not be in a release (None: allowed)."""
    parts = rel.replace("\\", "/").split("/")
    for d in parts[:-1]:
        if d in PROHIBITED_DIRS:
            return f"runtime / local directory '{d}/'"
    name = parts[-1]
    if name in PROHIBITED_NAMES:
        return f"user / runtime file '{name}'"
    for p in PROHIBITED_PATTERNS:
        if p.search(name):
            return f"prohibited file type ({p.pattern})"
    return None


def secret_values(env=None) -> List[Tuple[str, str]]:
    env = os.environ if env is None else env
    names = set(SECRET_ENV)
    try:     # also every *_env name the example config declares
        import json
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.endswith("_env") and isinstance(v, str):
                        names.add(v)
                    walk(v)
        walk(cfg)
    except Exception:
        pass
    return [(n, env[n]) for n in sorted(names) if len(str(env.get(n) or "")) >= 8]


def content_problem(name: str, data: bytes, secrets: Iterable[Tuple[str, str]]) -> Optional[str]:
    if data.startswith(SQLITE_MAGIC):
        return "SQLite database content"
    text = data.decode("utf-8", "ignore")
    for env_name, value in secrets:
        if value and value in text:
            return f"contains the value of ${env_name}"
    for p in SECRET_PATTERNS:
        if p.search(text):
            return f"looks like an API key ({p.pattern})"
    return None


def collect(root: Path = ROOT) -> Tuple[List[Path], List[Tuple[str, str]]]:
    files: List[Path] = []
    skipped: List[Tuple[str, str]] = []
    for f in INCLUDE_FILES:
        p = root / f
        if p.is_file():
            files.append(p)
    for d in INCLUDE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            why = prohibited(rel)
            if why:
                skipped.append((rel, why))
                continue
            files.append(p)
    return files, skipped


def verify(zip_path: Path, env=None) -> List[str]:
    """Problems in an archive (empty list: clean)."""
    problems = []
    secrets = secret_values(env)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        for n in names:
            why = prohibited(n)
            if why:
                problems.append(f"{n}: {why}")
                continue
            if n.endswith("/"):
                continue
            why = content_problem(n, z.read(n), secrets)
            if why:
                problems.append(f"{n}: {why}")
        for required in ("server/__init__.py", "config.example.json", "requirements.txt", "run_windows.bat",
                         "tools/bootstrap.py"):
            if required not in names:
                problems.append(f"missing required file {required}")
    return problems


def build(out_dir: Path, root: Path = ROOT, env=None) -> Tuple[Path, List[Tuple[str, str]]]:
    from tools.bootstrap import version
    ver = version(root)
    files, skipped = collect(root)
    secrets = secret_values(env)
    blocked = []
    for p in files:
        why = content_problem(p.name, p.read_bytes(), secrets)
        if why:
            blocked.append(f"{p.relative_to(root).as_posix()}: {why}")
    if blocked:
        raise RuntimeError("refusing to build a release:\n  " + "\n  ".join(blocked))
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"pre_move_scanner_v{ver}.zip"
    manifest = io.StringIO()
    manifest.write(f"Pre-Move Scanner v{ver} - release manifest (source only)\n")
    manifest.write(f"built {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
    manifest.write("never included: data/ config.json .venv/ *.db *.db-wal *.db-shm secrets caches\n\n")
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(root).as_posix()
            data = p.read_bytes()
            z.writestr(zipfile.ZipInfo(rel, date_time=time.localtime(p.stat().st_mtime)[:6]), data,
                       compress_type=zipfile.ZIP_DEFLATED)
            manifest.write(f"{hashlib.sha256(data).hexdigest()}  {rel}\n")
        z.writestr("RELEASE_MANIFEST.txt", manifest.getvalue())
    problems = verify(tmp, env)
    if problems:
        tmp.unlink()
        raise RuntimeError("release verification failed:\n  " + "\n  ".join(problems))
    tmp.replace(zpath)
    return zpath, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build / verify a clean release ZIP")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--verify", default=None, help="verify an existing ZIP only")
    a = ap.parse_args(argv)
    if a.verify:
        problems = verify(Path(a.verify))
        for p in problems:
            print("PROHIBITED", p)
        print("CLEAN" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    try:
        zpath, skipped = build(Path(a.out))
    except RuntimeError as exc:
        print(str(exc))
        return 1
    with zipfile.ZipFile(zpath) as z:
        n = len(z.namelist())
    print(f"release: {zpath} ({n} files, {zpath.stat().st_size / 1e6:.2f} MB)")
    print("verified: no data/, config.json, .venv/, database files, caches or secrets")
    by: dict = {}
    for rel, why in skipped:
        by[why] = by.get(why, 0) + 1
    for why, n in sorted(by.items()):
        print(f"  left out {n} file(s): {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
