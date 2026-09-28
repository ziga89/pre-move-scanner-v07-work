import shutil
import sqlite3
import unittest

from tests.helpers import T0, cfg, scratch_dir
from server.storage.db import Database, Writer, prune
from server.storage.history import (asset_history, asset_row_1m, event_row, events_query, fill_outcomes,
                                    load_market_minutes, market_row_1m, venue_history)
from server.storage.import_v06 import import_v06, sha256_file
from server.storage.schema import ASSET_1M_COLS, ASSET_5S_COLS, EVENT_COLS, MARKET_1M_COLS

# Exact v0.6 DDL (from v0.6 server/app.py init_db)
V06_DDL = """
CREATE TABLE IF NOT EXISTS composite_history_v2 (ts REAL, asset TEXT, price REAL, score REAL, coverage INTEGER,
 confirmed_venues INTEGER, bid_depth_1 REAL, ask_depth_1 REAL, ask_depth_ratio REAL, buy_ratio_60s REAL,
 volume_60s REAL, volume_ratio REAL, price_change_5m_pct REAL, ask_replenishment REAL, spread_bps REAL,
 trade_confidence REAL, components_json TEXT);
CREATE INDEX IF NOT EXISTS idx_composite_hist_asset_ts ON composite_history_v2(asset, ts);
CREATE TABLE IF NOT EXISTS venue_history_v2 (ts REAL, asset TEXT, venue TEXT, symbol TEXT, confirmed INTEGER,
 flags INTEGER, bid_depth_1 REAL, ask_depth_1 REAL, ask_depth_ratio REAL, buy_ratio_60s REAL, volume_60s REAL,
 volume_ratio REAL, spread_bps REAL, ask_replenishment REAL, discovery_volume_24h_usd REAL);
CREATE INDEX IF NOT EXISTS idx_venue_hist_asset_ts ON venue_history_v2(asset, ts);
CREATE TABLE IF NOT EXISTS scanner_events_v2 (ts REAL, asset TEXT, event_type TEXT, level REAL, message TEXT);
CREATE INDEX IF NOT EXISTS idx_scanner_events_asset_ts ON scanner_events_v2(asset, ts);
"""


def a5(ts, premove=10.0, asset="QNT", status="NORMAL"):
    d = {c: None for c in ASSET_5S_COLS}
    d.update(asset=asset, ts=int(ts), price=100.0, premove=premove, fast=premove, status=status,
             status_rank=4, confirmed=1, coverage=4, ask_ratio=0.9, buy_share=0.5, vol_ratio=1.0, volume_60s=1000.0)
    return tuple(d[c] for c in ASSET_5S_COLS)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = scratch_dir(self.id().split(".")[-1])
        for p in self.dir.glob("*"):
            p.unlink()
        self.db = Database(self.dir / "v07.db", cfg()["storage"])

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_migrations_and_wal(self):
        self.assertEqual(self.db.applied, [1, 2])   # 2 = v0.7.3 alerts + token_contracts
        mode = self.db.read_sync(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(mode.lower(), "wal")
        db2 = Database(self.dir / "v07.db", {})  # re-open: nothing re-applied
        self.assertEqual(db2.applied, [])
        db2.close()

    def test_writer_batches_and_drop_counter(self):
        self.db.insert("asset_metrics_5s", ASSET_5S_COLS, [a5(T0 + i * 5) for i in range(100)])
        self.db.flush()
        n = self.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM asset_metrics_5s").fetchone()[0])
        self.assertEqual(n, 100)
        w = Writer(self.dir / "x.db", maxsize=1)  # not started: queue fills
        self.assertTrue(w.submit("SELECT 1", [(1,)]))
        self.assertFalse(w.submit("SELECT 1", [(1,), (2,)]))
        self.assertEqual(w.dropped, 2)

    def test_history_keeps_spikes(self):
        rows = [a5(T0 + i * 5, premove=10.0) for i in range(2000)]
        rows[1000] = a5(T0 + 1000 * 5, premove=88.0)
        self.db.insert("asset_metrics_5s", ASSET_5S_COLS, rows)
        # minute table: 7 days with one 1-minute spike
        m = []
        for i in range(7 * 1440):
            d = {c: None for c in ASSET_1M_COLS}
            d.update(asset="QNT", ts=int(T0 - 7 * 86400 + i * 60), price_close=100.0, premove_avg=10.0,
                     premove_max=91.0 if i == 5000 else 12.0, status_last="NORMAL")
            m.append(asset_row_1m(d))
        self.db.insert("asset_metrics_1m", ASSET_1M_COLS, m)
        self.db.flush()
        h = self.db.read_sync(asset_history, "QNT", 6, T0 + 10000, 300)
        self.assertLessEqual(len(h["composite"]), 320)
        self.assertEqual(max(r["score"] for r in h["composite"]), 88.0)
        self.assertEqual(h["composite"][0]["status"], "NORMAL")
        h7 = self.db.read_sync(asset_history, "QNT", 168, T0 + 60, 1500)
        self.assertEqual(h7["source"], "asset_metrics_1m")
        self.assertLessEqual(len(h7["composite"]), 1500)
        self.assertEqual(max(r["score"] for r in h7["composite"]), 91.0)

    def test_prune_and_events(self):
        self.db.insert("asset_metrics_5s", ASSET_5S_COLS, [a5(T0 - 3 * 86400), a5(T0)])
        ev = {"ts": T0, "asset": "QNT", "category": "BOOK", "event_type": "thinning_onset", "severity": 1,
              "venue": "Gate", "level": 0.7, "message": "Ask depth -38% vs normal on Gate", "evidence": {"x": 1}}
        self.db.insert("events", EVENT_COLS, [event_row(ev)], mode="")
        self.db.flush()
        removed = self.db.call(lambda c: prune(c, cfg()["storage"], T0))
        self.assertEqual(removed["asset_metrics_5s"], 1)
        evs = self.db.read_sync(events_query, "QNT", T0 - 10)
        self.assertEqual(evs[0]["evidence"], {"x": 1})

    def test_v06_import_is_read_only_and_merged(self):
        src = self.dir / "scanner.db"
        con = sqlite3.connect(src)
        con.executescript(V06_DDL)
        for i in range(50):
            con.execute("INSERT INTO composite_history_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (T0 - 7200 + i * 5, "QNT", 100.0, 20.0 + i, 3, 1, 1e5, 1e5, 0.9, 0.55, 1e4, 1.1, 0.1,
                         None, 3.0, 0.8, "{}"))
            con.execute("INSERT INTO venue_history_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (T0 - 7200 + i * 5, "QNT", "Binance", "QNT-USDT", 0, 1, 1e5, 1e5, 0.9, 0.55, 1e4, 1.1, 3.0,
                         None, 1e6))
        con.execute("INSERT INTO scanner_events_v2 VALUES (?,?,?,?,?)",
                    (T0 - 7000, "QNT", "score_cross_up", 55, "Score crossed above 55"))
        con.commit()
        con.close()
        h0 = sha256_file(src)
        res = import_v06(src, self.db)
        self.assertTrue(res["unchanged"])
        self.assertEqual(sha256_file(src), h0)
        self.assertEqual(res["rows_read"]["composite_history_v2"], 50)
        import_v06(src, self.db)  # idempotent
        n = self.db.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM composite_history_v2").fetchone()[0])
        self.assertEqual(n, 50)
        # v0.7 data starts later; v0.6 rows are returned as legacy for the earlier part of the range
        self.db.insert("asset_metrics_5s", ASSET_5S_COLS, [a5(T0 - 3600 + i * 5) for i in range(10)])
        self.db.flush()
        h = self.db.read_sync(asset_history, "QNT", 6, T0, 1500)
        self.assertTrue(h["legacy"])
        self.assertTrue(all(r["src"] == "v06" for r in h["legacy"]))
        self.assertLess(h["legacy"][-1]["ts"], h["composite"][0]["ts"])
        evs = self.db.read_sync(events_query, "QNT", T0 - 86400)
        self.assertIn("LEGACY", {e["category"] for e in evs})
        vh = self.db.read_sync(venue_history, "QNT", 6, T0)
        self.assertIn("Binance", vh)

    def test_market_minutes_roundtrip_and_rehydrate(self):
        from server.engine.market_state import MarketState
        from server.sim import SimMarket
        c = cfg(engine={"min_baseline_minutes": 5, "baseline_lag_minutes": 2})
        st = MarketState("QNT", "x", "QNT/USDT", "USDT", lambda q: 1.0, c["engine"], c["feeds"], now=T0)
        m = SimMarket("x", "QNT/USDT", 50.0, seed=2)
        for i in range(12 * 60 * 2):
            ts = T0 + i * 0.5
            b, a, tr = m.step(ts, 0.5)
            st.on_book(b, a, ts)
            st.on_trades(tr, ts)
            if i % 2:
                st.tick(ts)
        recs = st.drain_minutes()
        self.assertIn("ask_ratio", recs[-1])
        self.db.insert("market_metrics_1m", MARKET_1M_COLS, [market_row_1m(r) for r in recs])
        self.db.flush()
        rows = self.db.read_sync(load_market_minutes, "QNT", "x", T0 - 60)
        self.assertEqual(len(rows), len(recs))
        fresh = MarketState("QNT", "x", "QNT/USDT", "USDT", lambda q: 1.0, c["engine"], c["feeds"], now=T0)
        fresh.rehydrate(rows, T0 + 12 * 60)
        self.assertTrue(fresh.base.warm)

    def test_outcomes_filled(self):
        self.db.call(lambda c: c.execute("INSERT INTO signal_outcomes (ts, asset, status, score, price) VALUES (?,?,?,?,?)",
                                         (T0, "QNT", "EMERGING", 60.0, 100.0)))
        rows = []
        for i in range(0, 300):
            d = {c: None for c in ASSET_1M_COLS}
            d.update(asset="QNT", ts=int(T0 + i * 60), price_close=100.0 + i * 0.1)
            rows.append(asset_row_1m(d))
        self.db.insert("asset_metrics_1m", ASSET_1M_COLS, rows)
        self.db.flush()
        self.db.call(lambda c: fill_outcomes(c, T0 + 5 * 3600))
        r = self.db.read_sync(lambda c: dict(c.execute("SELECT * FROM signal_outcomes").fetchone()))
        # minute rows are stamped at minute *start*: the price at T0+15m is the close
        # of the minute starting at T0+14m (i=14 -> 101.4)
        self.assertAlmostEqual(r["fwd_15m"], 1.4, places=3)
        self.assertAlmostEqual(r["fwd_1h"], 5.9, places=3)
        self.assertIsNone(r["fwd_24h"])


if __name__ == "__main__":
    unittest.main()
