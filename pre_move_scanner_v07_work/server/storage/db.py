"""SQLite access: WAL mode, one dedicated writer thread, thread-local readers.

Nothing here runs SQL on the asyncio event loop: writes are queued to the
writer thread (batched into transactions), reads run via `asyncio.to_thread`
on their own connections. In WAL mode readers never block the writer.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import LATEST_VERSION, MIGRATIONS, RETENTION


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10.0,
                              check_same_thread=False)
    else:
        con = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


def migrate(con: sqlite3.Connection) -> List[int]:
    """Apply pending migrations in place (forward-only, additive, idempotent). Never recreates a table."""
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                "applied_ts REAL, description TEXT)")
    done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, desc, step in MIGRATIONS:
        if version in done:
            continue
        if callable(step):
            con.commit()
            con.execute("BEGIN")
            try:
                step(con)
                con.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, time.time(), desc))
                con.commit()
            except Exception:
                con.rollback()
                raise
        else:
            con.executescript(step)
            con.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, time.time(), desc))
            con.commit()
        applied.append(version)
    return applied


def schema_version(path: Path) -> Optional[int]:
    """Highest applied migration of an existing database file (None: no file / not a scanner DB yet)."""
    if not Path(path).exists() or Path(path).stat().st_size == 0:
        return None
    con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    try:
        return con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def record_app_version(con: sqlite3.Connection, version: str, schema_from: Optional[int],
                       applied: List[int], now: Optional[float] = None) -> Optional[str]:
    """Keep `app_meta` current: the running app version, the version installed first, and one
    `upgrade_history` entry per version change or schema migration. Returns the previous app version
    (None on a new database or one last opened by v0.7, which did not record it)."""
    now = time.time() if now is None else now
    rows = dict(con.execute("SELECT key, value FROM app_meta").fetchall())
    prev = rows.get("app_version")
    history = json.loads(rows.get("upgrade_history") or "[]")
    if prev != version or applied:
        history.append({"ts": now, "from_version": prev, "to_version": version,
                        "from_schema": schema_from, "migrations": applied})
    upd = {"app_version": version, "upgrade_history": json.dumps(history[-50:])}
    if "installed_version" not in rows:
        upd["installed_version"] = version if schema_from is None else f"before {version} (schema {schema_from})"
        upd["installed_ts"] = str(now)
    if prev is not None and prev != version:
        upd["previous_app_version"] = prev
    con.executemany("INSERT OR REPLACE INTO app_meta (key, value, updated_ts) VALUES (?,?,?)",
                    [(k, v, now) for k, v in upd.items()])
    con.commit()
    return prev


def backup_before_migration(path: Path, label: str, min_free_factor: float = 2.0) -> Dict[str, Any]:
    """Consistent copy (SQLite online backup, WAL-safe) of an existing database before migrating it.

    Skipped - and reported - when free disk space is below `min_free_factor` x the database size."""
    import shutil
    path = Path(path)
    size = path.stat().st_size + sum(p.stat().st_size for p in (Path(str(path) + "-wal"),) if p.exists())
    dest_dir = path.parent / "backups"
    free = shutil.disk_usage(path.parent).free
    if free < min_free_factor * size + 50e6:
        return {"backup": None, "skipped": f"not enough free disk space ({free / 1e9:.1f} GB free, "
                                          f"database {size / 1e9:.2f} GB)"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.stem}.before-{label}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
    src = sqlite3.connect(str(path), timeout=30.0)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return {"backup": str(dest), "bytes": dest.stat().st_size}


class Writer(threading.Thread):
    """Single writer thread. Items: (sql, rows) or (callable, None)."""

    def __init__(self, path: Path, maxsize: int = 20000):
        super().__init__(name="sqlite-writer", daemon=True)
        self.path = path
        self.q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self.written_rows = 0
        self.batches = 0
        self.errors = 0
        self.last_error = ""
        self._stop_evt = threading.Event()

    def submit(self, sql: str, rows: Sequence[Sequence[Any]], critical: bool = False) -> bool:
        if not rows:
            return True
        try:
            self.q.put_nowait((sql, list(rows)))
            return True
        except queue.Full:
            if critical:
                try:
                    self.q.put((sql, list(rows)), timeout=2.0)
                    return True
                except queue.Full:
                    pass
            self.dropped += len(rows)
            return False

    def call(self, fn: Callable[[sqlite3.Connection], Any]) -> "queue.Queue":
        """Run fn(con) on the writer thread; returns a queue that receives the result."""
        result: "queue.Queue" = queue.Queue(maxsize=1)
        self.q.put((fn, result))
        return result

    def run(self) -> None:
        con = connect(self.path)
        while True:
            item = self.q.get()
            if item is None:
                break
            batch = [item]
            while len(batch) < 500:
                try:
                    nxt = self.q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._stop_evt.set()
                    break
                batch.append(nxt)
            for sql_or_fn, payload in batch:
                if callable(sql_or_fn):
                    try:
                        res = sql_or_fn(con)
                        con.commit()
                        payload.put(("ok", res))
                    except Exception as exc:  # report to the caller
                        con.rollback()
                        self.errors += 1
                        self.last_error = repr(exc)
                        payload.put(("error", exc))
                    continue
                try:
                    con.executemany(sql_or_fn, payload)
                    self.written_rows += len(payload)
                except Exception as exc:
                    self.errors += 1
                    self.last_error = f"{exc!r} in {sql_or_fn[:60]}"
            try:
                con.commit()
                self.batches += 1
            except Exception as exc:
                self.errors += 1
                self.last_error = repr(exc)
            if self._stop_evt.is_set():
                break
        con.close()

    def stop(self, timeout: float = 10.0) -> None:
        try:
            self.q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self.join(timeout)


class Database:
    def __init__(self, path: Path, scfg: Optional[Dict[str, Any]] = None, backup_label: Optional[str] = None,
                 app_version: Optional[str] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = scfg or {}
        existing = schema_version(self.path)
        self.new_install = existing is None
        self.upgraded_from = existing
        self.backup: Dict[str, Any] = {}
        if existing is not None and existing < LATEST_VERSION and self.cfg.get("backup_before_migration", True):
            try:
                self.backup = backup_before_migration(self.path, backup_label or f"schema{LATEST_VERSION}")
            except Exception as exc:          # a failed backup must never block the scanner; it is reported
                self.backup = {"backup": None, "error": repr(exc)[:200]}
        con = connect(self.path)
        self.applied = migrate(con)
        self.previous_app_version = record_app_version(con, app_version, existing, self.applied) if app_version else None
        con.close()
        self._local = threading.local()
        self._readers: List[sqlite3.Connection] = []   # every thread-local reader, so close() can close them all
        self._readers_lock = threading.Lock()
        self.writer = Writer(self.path, int(self.cfg.get("writer_queue", 20000)))
        self.writer.start()

    # ---- reads
    def _reader(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = connect(self.path)
            self._local.con = con
            with self._readers_lock:
                self._readers.append(con)
        return con

    def read_sync(self, fn: Callable[..., Any], *args, **kw) -> Any:
        return fn(self._reader(), *args, **kw)

    async def read(self, fn: Callable[..., Any], *args, **kw) -> Any:
        return await asyncio.to_thread(self.read_sync, fn, *args, **kw)

    # ---- writes
    def insert(self, table: str, cols: Sequence[str], rows: Iterable[Sequence[Any]], mode: str = "OR REPLACE",
               critical: bool = False) -> bool:
        rows = list(rows)
        if not rows:
            return True
        sql = f"INSERT {mode} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        return self.writer.submit(sql, rows, critical=critical)

    def call(self, fn: Callable[[sqlite3.Connection], Any], timeout: float = 60.0) -> Any:
        status, res = self.writer.call(fn).get(timeout=timeout)
        if status == "error":
            raise res
        return res

    def flush(self, timeout: float = 30.0) -> None:
        self.call(lambda con: None, timeout=timeout)

    def stats(self) -> Dict[str, Any]:
        w = self.writer
        return {"path": str(self.path), "queue": w.q.qsize(), "dropped_rows": w.dropped,
                "written_rows": w.written_rows, "batches": w.batches, "errors": w.errors,
                "last_error": w.last_error, "migrations_applied_now": self.applied,
                "new_install": self.new_install, "upgraded_from_schema": self.upgraded_from,
                "previous_app_version": self.previous_app_version,
                "schema_version": LATEST_VERSION, "pre_migration_backup": self.backup}

    def close(self) -> None:
        self.writer.stop()
        # Readers are opened lazily in whichever thread reads (incl. asyncio.to_thread
        # pool threads). Close them all: open handles block file deletion on Windows.
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for con in readers:
            try:
                con.close()
            except Exception:
                pass
        self._local = threading.local()


def prune(con: sqlite3.Connection, scfg: Dict[str, Any], now: float) -> Dict[str, int]:
    out = {}
    for table, key, unit, *col in RETENTION:
        keep = float(scfg.get(key, 0) or 0) * unit
        if keep <= 0:
            continue
        cur = con.execute(f"DELETE FROM {table} WHERE {col[0] if col else 'ts'} < ?", (now - keep,))
        out[table] = cur.rowcount
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)
