"""SQLite persistence for wallet intelligence (events, balances, cursors, provider budgets)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

TRANSFER_COLS = ["chain", "tx_hash", "log_index", "ts", "block", "token", "asset", "from_addr", "to_addr", "amount",
                 "usd_value", "from_entity", "from_type", "to_entity", "to_type", "classification",
                 "class_confidence", "explanation", "source", "event_type", "attribution_confidence", "entity_type",
                 "direction", "provider"]
BALANCE_COLS = ["chain", "token", "address", "ts", "asset", "balance", "source"]


class DbIntelStore:
    def __init__(self, db):
        self.db = db
        self.cursors: Dict[str, Any] = {}
        self.budget: Dict[str, int] = {}
        try:
            rows = db.read_sync(lambda c: c.execute("SELECT key, value FROM intel_cursors").fetchall())
            for k, v in rows:
                if k.startswith("budget:"):
                    self.budget[k] = int(v)
                elif str(v).startswith("{"):
                    self.cursors[k] = json.loads(v)
                else:
                    self.cursors[k] = int(v)            # v0.7 block cursors ("addr:..." / "token:...")
        except Exception:
            pass

    def save_transfers(self, rows: List[Dict[str, Any]]) -> None:
        self.db.insert("onchain_transfers", TRANSFER_COLS, [tuple(r.get(c) for c in TRANSFER_COLS) for r in rows],
                       mode="OR IGNORE")

    def save_balances(self, rows: List[Dict[str, Any]]) -> None:
        self.db.insert("wallet_balances", BALANCE_COLS, [tuple(r.get(c) for c in BALANCE_COLS) for r in rows])

    def save_cursors(self, cursors: Dict[str, Any]) -> None:
        now = time.time()
        rows = [(k, json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v), now)
                for k, v in cursors.items()]
        rows += [(k, str(v), now) for k, v in self.budget.items()]
        self.db.insert("intel_cursors", ["key", "value", "updated_ts"], rows)

    def load_transfers(self, since: float) -> List[Dict[str, Any]]:
        return self.db.read_sync(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM onchain_transfers WHERE ts>=? ORDER BY ts", (since,))])

    def load_balances(self, since: float) -> List[Dict[str, Any]]:
        return self.db.read_sync(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM wallet_balances WHERE ts>=? ORDER BY ts", (since,))])
