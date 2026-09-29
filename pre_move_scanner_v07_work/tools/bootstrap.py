"""First-run / every-run bootstrap (standard library only - runs before any dependency is installed).

    python tools/bootstrap.py                  ensure config.json (only if missing) and data/
    python tools/bootstrap.py --venv           also create .venv if missing and install requirements.txt
    python tools/bootstrap.py --version        print the canonical version (server/__init__.py)
    python tools/bootstrap.py --status         config / data / database state (read-only)

Guarantees (tested in tests/test_bootstrap.py):
* config.json is created from config.example.json ONLY when it does not exist - never overwritten;
* data/ is created when missing; an existing database is never replaced: the scanner migrates it in
  place on start (with a pre-migration backup), and a genuinely new install gets a fresh database;
* nothing here writes secrets anywhere: API keys stay in environment variables.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]


def version(root: Path = ROOT) -> str:
    """The canonical version, read from server/__init__.py without importing the package."""
    text = (Path(root) / "server" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError("no __version__ in server/__init__.py")
    return m.group(1)


def ensure_config(root: Path = ROOT) -> str:
    cfg, example = Path(root) / "config.json", Path(root) / "config.example.json"
    if cfg.exists():
        return "kept"
    if not example.exists():
        return "missing example"
    shutil.copyfile(example, cfg)
    return "created"


def ensure_data_dir(root: Path = ROOT) -> str:
    d = Path(root) / "data"
    if d.is_dir():
        return "kept"
    d.mkdir(parents=True)
    return "created"


def venv_python(root: Path = ROOT) -> Path:
    v = Path(root) / ".venv"
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_venv(root: Path = ROOT, install: bool = True, with_pip: bool = True) -> Dict[str, Any]:
    py = venv_python(root)
    created = False
    if not py.exists():
        venv.EnvBuilder(with_pip=with_pip, upgrade_deps=False).create(Path(root) / ".venv")
        created = True
    out: Dict[str, Any] = {"python": str(py), "created": created, "installed": False}
    if install:
        r = subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                            str(Path(root) / "requirements.txt")], cwd=str(root))
        out["installed"] = r.returncode == 0
        out["pip_rc"] = r.returncode
    return out


def db_status(root: Path = ROOT, rel: Optional[str] = None) -> Dict[str, Any]:
    """Read-only look at the configured database: new install or schema version of an existing one."""
    rel = rel or "data/scanner_v07.db"
    try:
        import json
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        rel = ((cfg.get("storage") or {}).get("path")) or rel
    except Exception:
        pass
    p = Path(rel) if Path(rel).is_absolute() else Path(root) / rel
    if not p.exists():
        return {"path": str(p), "exists": False, "new_install": True, "schema_version": None}
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    try:
        v = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        v = None
    finally:
        con.close()
    return {"path": str(p), "exists": True, "new_install": False, "schema_version": v,
            "size_mb": round(p.stat().st_size / 1e6, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-Move Scanner bootstrap")
    ap.add_argument("--version", action="store_true", help="print the canonical version and exit")
    ap.add_argument("--venv", action="store_true", help="create .venv if missing and install requirements.txt")
    ap.add_argument("--no-install", action="store_true", help="with --venv: skip pip install")
    ap.add_argument("--without-pip", action="store_true", help="with --venv: create the venv without pip (tests)")
    ap.add_argument("--status", action="store_true", help="print config / data / database state")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.version:
        print(version(root))
        return 0
    if a.venv:
        info = ensure_venv(root, install=not a.no_install, with_pip=not a.without_pip)
        print(f"[setup] virtual environment: {'created' if info['created'] else 'present'} ({info['python']})")
        if not a.no_install:
            print(f"[setup] dependencies: {'installed / up to date' if info['installed'] else 'pip FAILED'}")
            if not info["installed"]:
                return 2
    c = ensure_config(root)
    print("[setup] config.json: " + {"created": "created from config.example.json",
                                     "kept": "present - never overwritten",
                                     "missing example": "missing (no config.example.json) - defaults are used"}[c])
    d = ensure_data_dir(root)
    print(f"[setup] data folder: {'created' if d == 'created' else 'present'}")
    s = db_status(root)
    if s["new_install"]:
        print(f"[setup] database: none yet - a fresh one is created on first start ({s['path']})")
    else:
        print(f"[setup] database: existing {s['path']} (schema {s['schema_version']}, {s.get('size_mb')} MB) - "
              "kept and migrated in place (backup first)")
    if a.status:
        print(f"[setup] version {version(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
