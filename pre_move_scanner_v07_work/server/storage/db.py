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

from .schema import MIGRATIONS, RETENTION


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
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                "applied_ts REAL, description TEXT)")
    done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, desc, sql in MIGRATIONS:
        if version in done:
            continue
        con.executescript(sql)
        con.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, time.time(), desc))
        con.commit()
        applied.append(version)
    return applied


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
    def __init__(self, path: Path, scfg: Optional[Dict[str, Any]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = scfg or {}
        con = connect(self.path)
        self.applied = migrate(con)
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
                "last_error": w.last_error, "migrations_applied_now": self.applied}

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
