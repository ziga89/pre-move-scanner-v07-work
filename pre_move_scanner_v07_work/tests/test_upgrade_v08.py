"""Upgrade a v0.7.x installation to v0.8 in place: the existing data/scanner_v07.db is migrated (never
recreated) with every row kept - market history, baselines (1-minute rows), alerts, signal outcomes,
wallet history, cursors and budgets - a consistent backup is taken first, the migration is idempotent
and atomic, and a v0.7 config.json keeps working without being rewritten."""
import asyncio
import json
import shutil
import sqlite3
import unittest

from server.config import load_config
from server.intel.store import DbIntelStore
from server.service import ScannerService
from server.storage.db import Database, migrate, schema_version
from server.storage.history import load_market_minutes, load_registry
from server.storage.schema import (ALERT_COLS, ASSET_1M_COLS, ASSET_5S_COLS, MARKET_1M_COLS, MIGRATIONS,
                                   migration_3)
from tests.helpers import cfg, scratch_dir

TABLES = ["asset_metrics_5s", "asset_metrics_1m", "market_metrics_1m", "events", "alerts", "signal_outcomes",
          "onchain_transfers", "wallet_balances", "intel_cursors", "token_contracts", "universe_snapshots",
          "venue_selection"]


def v073_database(path, now):
    """A database exactly as v0.7.3 / v0.7.4 leave it (migrations 1 and 2) with data in every table."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_ts REAL, description TEXT)")
    for v, desc, sql in MIGRATIONS[:2]:
        con.executescript(sql)
        con.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (v, now - 86400 * 30, desc))

    def ins(table, cols, row):
        con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", row)
    for i in range(120):
        d = {c: None for c in ASSET_5S_COLS}
        d.update(asset="QNT", ts=int(now - 3600 + i * 5), price=100.0 + i * 0.01, premove=20.0 + i % 7, status="NORMAL",
                 status_rank=4, coverage=4, confirmed=1)
        ins("asset_metrics_5s", ASSET_5S_COLS, tuple(d[c] for c in ASSET_5S_COLS))
    for i in range(60):
        d = {c: None for c in ASSET_1M_COLS}
        d.update(asset="QNT", ts=int(now - 3600 + i * 60), price_close=100.0, premove_avg=20.0)
        ins("asset_metrics_1m", ASSET_1M_COLS, tuple(d[c] for c in ASSET_1M_COLS))
        m = {c: 1.0 for c in MARKET_1M_COLS}
        m.update(asset="QNT", exchange="kucoin", ts=int(now - 3600 + i * 60), symbol="QNT/USDT")
        ins("market_metrics_1m", MARKET_1M_COLS, tuple(m[c] for c in MARKET_1M_COLS))
    ins("events", ["ts", "asset", "category", "event_type", "severity", "message", "evidence"],
        (now - 100, "QNT", "SYSTEM", "note", 0, "v0.7 event", "{}"))
    a = {c: None for c in ALERT_COLS}
    a.update(id="QNT-1-1", asset="QNT", state="INVALIDATED", started_ts=now - 7200, fired_ts=now - 7000,
             ended_ts=now - 6000, confirmed_venues='["kucoin"]', reasons='["x"]', checks="{}")
    ins("alerts", ALERT_COLS, tuple(a[c] for c in ALERT_COLS))
    ins("signal_outcomes", ["ts", "asset", "status", "score", "price", "fwd_1h", "filled"],
        (now - 7000, "QNT", "HIGH_CONVICTION", 91.0, 100.0, 2.5, 1))
    ins("onchain_transfers", ["chain", "tx_hash", "log_index", "ts", "block", "token", "asset", "from_addr", "to_addr",
                              "amount", "usd_value", "from_entity", "from_type", "to_entity", "to_type",
                              "classification", "class_confidence", "explanation", "source"],
        ("ethereum", "0xabc", 1, now - 500, 1, "0x4a220e6096b25eadb88358cb44068a3248254675", "QNT",
         "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa", "0x" + "ee" * 20, 10.0, 1000.0, "Wintermute", "MM", None, None,
         "UNKNOWN", "LOW", "one side unattributed", "address"))
    ins("wallet_balances", ["chain", "token", "address", "ts", "asset", "balance", "source"],
        ("ethereum", "0x4a22", "0xf819", now - 500, "QNT", 5.0, "tokenbalance"))
    for k, v in (("addr:ethereum:0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa", "19000000"), ("budget:20990101", "321")):
        ins("intel_cursors", ["key", "value", "updated_ts"], (k, v, now))
    ins("token_contracts", ["asset", "coingecko_id", "state", "chain", "contract", "decimals", "reason", "source",
                            "checked_ts"],
        ("QNT", "quant-network", "supported", "ethereum", "0x4a220e6096b25eadb88358cb44068a3248254675", 18, "ok",
         "coingecko", now - 86400))
    ins("token_contracts", ["asset", "coingecko_id", "state", "chain", "contract", "decimals", "reason", "source",
                            "checked_ts"],
        ("BTC", "bitcoin", "unsupported", None, None, None, "native coin", "coingecko", now - 86400))
    ins("universe_snapshots", ["ts", "coin_id", "symbol", "in_universe", "pinned"], (now, "xdce-crowd-sale", "XDC", 1, 1))
    ins("venue_selection", ["ts", "asset", "exchange", "symbol", "selected"], (now, "QNT", "kucoin", "QNT/USDT", 1))
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    con.close()
    return counts


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("upgrade_v08")
        shutil.rmtree(self.d, ignore_errors=True)
        (self.d / "data").mkdir(parents=True)
        self.db_path = self.d / "data" / "scanner_v07.db"
        import time
        self.now = time.time()
        self.counts = v073_database(self.db_path, self.now)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def counts_now(self, db):
        return {t: db.read_sync(lambda c, t=t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in TABLES}

    def test_in_place_migration_keeps_every_row_and_backs_up_first(self):
        self.assertEqual(schema_version(self.db_path), 2)
        db = Database(self.db_path, cfg()["storage"], backup_label="v0.8.0")
        try:
            self.assertFalse(db.new_install)
            self.assertEqual((db.upgraded_from, db.applied), (2, [3]))
            self.assertEqual(self.counts_now(db), self.counts)                      # no historical-data loss
            reg = db.read_sync(load_registry)
            self.assertEqual((reg["QNT"]["state"], reg["QNT"]["native_chain"], reg["QNT"]["contract_address"]),
                             ("READY", "ethereum", "0x4a220e6096b25eadb88358cb44068a3248254675"))
            self.assertNotIn("BTC", reg)            # v0.7 "unsupported" decisions are re-made with v0.8 providers
            cols = {r[1] for r in db.read_sync(lambda c: c.execute("PRAGMA table_info(onchain_transfers)").fetchall())}
            self.assertTrue({"event_type", "attribution_confidence", "entity_type", "direction", "provider"} <= cols)
            # baselines rehydrate from the kept 1-minute rows
            self.assertEqual(len(db.read_sync(load_market_minutes, "QNT", "kucoin", self.now - 7200)), 60)
            # v0.7 cursors / budget counters are still read
            store = DbIntelStore(db)
            self.assertEqual(store.cursors["addr:ethereum:0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa"], 19000000)
            self.assertEqual(store.budget["budget:20990101"], 321)
            b = db.backup
            self.assertTrue(b.get("backup"), b)
            bcon = sqlite3.connect(b["backup"])
            self.assertEqual(bcon.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)
            self.assertEqual(bcon.execute("SELECT COUNT(*) FROM asset_metrics_5s").fetchone()[0],
                             self.counts["asset_metrics_5s"])
            bcon.close()
        finally:
            db.close()
        db = Database(self.db_path, cfg()["storage"])               # second start: nothing to do, no new backup
        try:
            self.assertEqual((db.applied, db.backup), ([], {}))
            self.assertEqual(self.counts_now(db), self.counts)
        finally:
            db.close()

    def test_migration_is_idempotent_and_atomic(self):
        con = sqlite3.connect(self.db_path)
        migration_3(con)                             # applied twice by hand: no error, nothing duplicated
        migration_3(con)
        con.commit()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM asset_registry").fetchone()[0], 1)
        con.close()

        # an upgrade interrupted half-way leaves the database exactly as it was
        p2 = self.d / "data" / "interrupted.db"
        v073_database(p2, self.now)
        import server.storage.schema as schema
        orig = schema.MIGRATIONS[2]

        def boom(c):
            orig[2](c)
            raise RuntimeError("power cut")
        schema.MIGRATIONS[2] = (3, orig[1], boom)
        try:
            con = sqlite3.connect(p2)
            with self.assertRaises(RuntimeError):
                migrate(con)
            con.close()
        finally:
            schema.MIGRATIONS[2] = orig
        self.assertEqual(schema_version(p2), 2)
        con = sqlite3.connect(p2)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='asset_registry'").fetchone()[0], 0)
        self.assertEqual(migrate(con), [3])                          # simply runs again on the next start
        con.close()

    def test_service_runs_on_the_upgraded_database_with_its_history(self):
        c = cfg(mode="sim", feeds={"backend": "sim", "resync_grace_seconds": 2},
                sim={"assets": 3, "venues_per_asset": 3, "seed": 5, "scenarios": False},
                storage={"path": str(self.db_path)})

        async def go():
            svc = ScannerService(c)
            await svc.start()
            try:
                await asyncio.sleep(2)
                h = await svc.history("QNT", 2)
                self.assertGreaterEqual(len(h["composite"]), 50)          # v0.7 5 s rows still charted
                self.assertEqual(len(h["alerts"]), 1)                     # v0.7 alert still drawn
                st = svc.health_payload()["storage"]
                self.assertEqual((st["upgraded_from_schema"], st["schema_version"]), (2, 3))
                out = await svc.outcomes(30)
                self.assertTrue(any(r["status"] == "HIGH_CONVICTION" for r in out["by_status"]))
            finally:
                await svc.stop()
        asyncio.run(go())

    def test_v07_config_keeps_working_and_is_never_rewritten(self):
        v07 = {"mode": "live",
               "universe": {"pinned_assets": ["QNT", "LINK", "XDC", "RAIL"], "coingecko_ids": {"QNT": "quant-network"}},
               "intel": {"enabled": True, "etherscan_api_key_env": "ETHERSCAN_API_KEY", "discovery_calls_per_minute": 1,
                         "tokens": {"QNT": {"chain": "ethereum", "contract": "0x4a220E6096B25EADb88358cb44068A3248254675"}}}}
        raw = json.dumps(v07, indent=2)
        (self.d / "config.json").write_text(raw)
        c = load_config(self.d)
        self.assertEqual(c["universe"]["manual_assets"], ["QNT", "LINK", "XDC", "RAIL"])
        self.assertNotIn("pinned_assets", c["universe"])
        self.assertEqual(c["universe"]["coingecko_ids"]["XDC"], "xdce-crowd-sale")      # verified default kept
        self.assertEqual(c["assets"]["discovery_calls_per_minute"], 1)                  # v0.7.3 key carried over
        self.assertEqual(c["intel"]["etherscan_api_key_env"], "ETHERSCAN_API_KEY")
        self.assertTrue(any("manual_assets" in w for w in c["_meta"]["warnings"]))
        self.assertEqual((self.d / "config.json").read_text(), raw)                     # byte-identical


if __name__ == "__main__":
    unittest.main()
