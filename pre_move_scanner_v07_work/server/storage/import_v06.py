"""Read-only import of v0.6 history into the v0.7 database.

The v0.6 `scanner.db` is opened with SQLite `mode=ro` and hashed before and
after; the import reports failure if the source file changed in any way.
Rows go into same-named legacy tables in the v0.7 DB (INSERT OR IGNORE, so
re-importing later only adds new rows).
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict

from .db import Database, connect
from .schema import V06_COMPOSITE_COLS, V06_EVENT_COLS, V06_VENUE_COLS


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_v06(src: Path, db: Database, chunk: int = 5000) -> Dict[str, Any]:
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    # WAL/journal side files are also part of the source state; hash what exists.
    side = [p for p in (src.with_name(src.name + "-wal"), src.with_name(src.name + "-journal")) if p.exists()]
    before = sha256_file(src)
    side_before = {p.name: sha256_file(p) for p in side}
    ro = connect(src, readonly=True)
    tables = {r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {"composite_history_v2": 0, "venue_history_v2": 0, "scanner_events_v2": 0}
    plan = [("composite_history_v2", V06_COMPOSITE_COLS), ("venue_history_v2", V06_VENUE_COLS),
            ("scanner_events_v2", V06_EVENT_COLS)]
    try:
        for table, cols in plan:
            if table not in tables:
                continue
            src_cols = {r[1] for r in ro.execute(f"PRAGMA table_info({table})")}
            use = [c for c in cols if c in src_cols]
            cur = ro.execute(f"SELECT {','.join(use)} FROM {table}")
            while True:
                rows = cur.fetchmany(chunk)
                if not rows:
                    break
                db.insert(table, use, [tuple(r) for r in rows], mode="OR IGNORE", critical=True)
                counts[table] += len(rows)
    finally:
        ro.close()
    db.flush()
    after = sha256_file(src)
    side_after = {p.name: sha256_file(p) for p in side}
    unchanged = before == after and side_before == side_after
    db.call(lambda con: con.execute("INSERT INTO legacy_import VALUES (?,?,?,?,?,?,?)",
                                    (time.time(), str(src), before, after, counts["composite_history_v2"],
                                     counts["venue_history_v2"], counts["scanner_events_v2"])))
    if not unchanged:
        raise RuntimeError(f"v0.6 database changed during import (sha256 {before} -> {after}); aborting")
    return {"source": str(src), "sha256": before, "unchanged": unchanged, "rows_read": counts,
            "tables_found": sorted(t for t in tables if t in counts)}
