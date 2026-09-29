"""Persistent manual assets (v0.8; replaces v0.7 "pinned" coins).

A manual asset is monitored in addition to the CoinGecko Top-100 until you remove it. It is stored in
SQLite (`manual_assets`) with its CoinGecko id, so a ticker is never re-guessed. Manual status never
changes a score or the ranking - it only guarantees the asset stays in the monitored universe.

Seeding: the config list (`universe.manual_assets`, or the v0.7 `universe.pinned_assets`) is only an
initial seed. A symbol is seeded once; after that the Universe page / API manage it, so removing QNT
in the UI is not undone by the config at the next start. Removal keeps the row (with `removed_ts`)
and all history; re-adding reactivates it.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from ..storage.schema import MANUAL_ASSET_COLS


class ManualAssets:
    def __init__(self, db: Any, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        self.rows: Dict[str, Dict[str, Any]] = {}
        if db is not None:
            try:
                for r in db.read_sync(lambda c: [dict(x) for x in c.execute("SELECT * FROM manual_assets")]):
                    self.rows[str(r["symbol"]).upper()] = r
            except Exception:
                pass

    def _save(self, row: Dict[str, Any]) -> None:
        self.rows[row["symbol"]] = row
        if self.db is not None:
            self.db.insert("manual_assets", MANUAL_ASSET_COLS, [tuple(row.get(c) for c in MANUAL_ASSET_COLS)],
                           critical=True)

    def active(self) -> List[Dict[str, Any]]:
        return sorted((r for r in self.rows.values() if not r.get("removed_ts")),
                      key=lambda r: (r.get("added_ts") or 0.0, r["symbol"]))

    def symbols(self) -> List[str]:
        return [r["symbol"] for r in self.active()]

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        r = self.rows.get(symbol.upper())
        return r if r and not r.get("removed_ts") else None

    def known(self, symbol: str) -> bool:
        return symbol.upper() in self.rows

    def seed(self, symbols: List[str], ids: Dict[str, str]) -> List[str]:
        """Add config symbols never seen before (removed ones stay removed)."""
        now = self.clock()
        added = []
        for s in symbols:
            sym = str(s).upper().strip()
            if not sym or sym in self.rows:
                continue
            self._save({"symbol": sym, "coingecko_id": ids.get(sym), "name": None, "added_ts": now,
                        "removed_ts": None, "source": "config", "note": "seeded from config (v0.7 pinned / manual list)"})
            added.append(sym)
        return added

    def add(self, symbol: str, coingecko_id: str, name: Optional[str] = None, source: str = "user") -> Dict[str, Any]:
        sym = symbol.upper()
        old = self.rows.get(sym) or {}
        row = {"symbol": sym, "coingecko_id": coingecko_id, "name": name or old.get("name"),
               "added_ts": self.clock(), "removed_ts": None, "source": source,
               "note": "re-added" if old.get("removed_ts") else ("added" if not old else old.get("note"))}
        self._save(row)
        return row

    def resolve(self, symbol: str, coingecko_id: str, name: Optional[str]) -> None:
        """Store the CoinGecko id found for a seeded symbol (unique match only - see universe.py)."""
        r = self.rows.get(symbol.upper())
        if r and (r.get("coingecko_id") != coingecko_id or (name and r.get("name") != name)):
            self._save({**r, "coingecko_id": coingecko_id, "name": name or r.get("name")})

    def remove(self, symbol: str) -> bool:
        r = self.get(symbol)
        if r is None:
            return False
        self._save({**r, "removed_ts": self.clock(), "note": "removed (history kept)"})
        return True
