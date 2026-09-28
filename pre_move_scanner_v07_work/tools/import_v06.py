"""Import v0.6 history into the v0.7 database — read-only on the v0.6 side.

    python tools/import_v06.py "C:\\path\\to\\pre_move_scanner\\scanner.db"

The v0.6 file is opened with SQLite mode=ro and hashed before and after; the
import aborts if the file changed. Safe to re-run (duplicates are ignored).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_config, resolve_path  # noqa: E402
from server.storage.db import Database  # noqa: E402
from server.storage.import_v06 import import_v06  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).expanduser()
    cfg = load_config(ROOT)
    db = Database(resolve_path(cfg, cfg["storage"]["path"]), cfg["storage"])
    try:
        res = import_v06(src, db)
    finally:
        db.close()
    print(f"Imported from {res['source']} (sha256 {res['sha256'][:16]}…, unchanged: {res['unchanged']})")
    for t, n in res["rows_read"].items():
        print(f"  {t}: {n} rows read")
    print(f"into {resolve_path(cfg, cfg['storage']['path'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
