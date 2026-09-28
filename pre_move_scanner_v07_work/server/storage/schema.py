"""SQLite schema (versioned migrations) and column lists.

v0.6 tables are recreated here *only* as import targets for the read-only
v0.6 history import; the v0.6 database file itself is never written.
"""
from __future__ import annotations

from typing import List, Tuple

from ..engine.baselines import MINUTE_RAW

ASSET_5S_COLS = [
    "asset", "ts", "price", "premove", "fast", "slow", "instant", "status", "status_rank", "confidence",
    "liquidity", "orderbook", "buy_pressure", "cross_venue", "mm", "whale", "cex_flow", "scarcity",
    "r15", "r60", "late_index", "coverage", "coverage_total", "confirmed", "families",
    "ask_depth_1", "bid_depth_1", "ask_ratio", "buy_share", "volume_60s", "vol_ratio", "refill",
    "spread_bps", "slippage_ratio", "cancel_proxy", "cancel_proxy_conf",
]
MARKET_10S_COLS = [
    "asset", "exchange", "ts", "symbol", "state", "confirmed", "families", "mid", "spread_bps",
    "bid_depth_05", "ask_depth_05", "bid_depth_1", "ask_depth_1", "bid_depth_2", "ask_depth_2",
    "ask_ratio", "bid_ratio", "buy_share", "volume_60s", "vol_ratio", "trades_60s", "ask_repl",
    "ask_refill", "ask_net_pct", "cancel_proxy", "cancel_proxy_conf", "slippage_bps", "confidence",
    "book_rate_hz",
]
ASSET_1M_COLS = [
    "asset", "ts", "price_open", "price_high", "price_low", "price_close", "premove_avg", "premove_max",
    "fast_max", "status_last", "confirmed_max", "coverage_min", "confidence_avg", "orderbook_avg",
    "liquidity_avg", "buy_pressure_avg", "cross_venue_avg", "mm_avg", "whale_avg", "cex_flow_avg",
    "scarcity_avg", "ask_ratio_avg", "buy_share_avg", "vol_ratio_avg", "spread_bps_avg",
    "slippage_ratio_avg", "volume_sum",
]
MARKET_1M_COLS = ["asset", "exchange", "ts", "symbol"] + list(MINUTE_RAW) + ["ask_ratio", "bid_ratio"]
EVENT_COLS = ["ts", "asset", "category", "event_type", "severity", "venue", "level", "message", "evidence"]

V06_COMPOSITE_COLS = [
    "ts", "asset", "price", "score", "coverage", "confirmed_venues", "bid_depth_1", "ask_depth_1",
    "ask_depth_ratio", "buy_ratio_60s", "volume_60s", "volume_ratio", "price_change_5m_pct",
    "ask_replenishment", "spread_bps", "trade_confidence", "components_json",
]
V06_VENUE_COLS = [
    "ts", "asset", "venue", "symbol", "confirmed", "flags", "bid_depth_1", "ask_depth_1", "ask_depth_ratio",
    "buy_ratio_60s", "volume_60s", "volume_ratio", "spread_bps", "ask_replenishment", "discovery_volume_24h_usd",
]
V06_EVENT_COLS = ["ts", "asset", "event_type", "level", "message"]


def _cols(cols: List[str], pk: List[str], text: Tuple[str, ...] = ()) -> str:
    parts = []
    for c in cols:
        if c in ("asset", "exchange", "symbol", "status", "state", "status_last", "category", "event_type",
                 "venue", "message", "evidence") or c in text:
            typ = "TEXT"
        elif c in ("ts",) and "ts" in pk:
            typ = "INTEGER NOT NULL"
        else:
            typ = "REAL"
        if c in pk and "NOT NULL" not in typ:
            typ += " NOT NULL"
        parts.append(f"{c} {typ}")
    return ", ".join(parts)


MIGRATIONS: List[Tuple[int, str, str]] = [
    (1, "v0.7 initial schema", f"""
CREATE TABLE IF NOT EXISTS asset_metrics_5s ({_cols(ASSET_5S_COLS, ['asset', 'ts'])},
  PRIMARY KEY (asset, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_am5_ts ON asset_metrics_5s(ts);

CREATE TABLE IF NOT EXISTS market_metrics_10s ({_cols(MARKET_10S_COLS, ['asset', 'exchange', 'ts'])},
  PRIMARY KEY (asset, exchange, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_mm10_ts ON market_metrics_10s(ts);

CREATE TABLE IF NOT EXISTS asset_metrics_1m ({_cols(ASSET_1M_COLS, ['asset', 'ts'])},
  PRIMARY KEY (asset, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_am1_ts ON asset_metrics_1m(ts);

CREATE TABLE IF NOT EXISTS market_metrics_1m ({_cols(MARKET_1M_COLS, ['asset', 'exchange', 'ts'])},
  PRIMARY KEY (asset, exchange, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_mm1_ts ON market_metrics_1m(ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, asset TEXT, category TEXT, event_type TEXT,
  severity INTEGER, venue TEXT, level REAL, message TEXT, evidence TEXT);
CREATE INDEX IF NOT EXISTS idx_events_asset_ts ON events(asset, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS universe_snapshots (
  ts REAL NOT NULL, coin_id TEXT, symbol TEXT, name TEXT, rank INTEGER, market_cap REAL,
  eligible INTEGER, in_universe INTEGER, pinned INTEGER, usable INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_universe_ts ON universe_snapshots(ts);

CREATE TABLE IF NOT EXISTS venue_selection (
  ts REAL NOT NULL, asset TEXT, exchange TEXT, symbol TEXT, quote TEXT, volume_24h_usd REAL,
  rank INTEGER, selected INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_vsel_asset_ts ON venue_selection(asset, ts);
CREATE INDEX IF NOT EXISTS idx_vsel_ts ON venue_selection(ts);

CREATE TABLE IF NOT EXISTS feed_health_1m (
  ts REAL NOT NULL, exchange TEXT, partition INTEGER, state TEXT, mode TEXT, markets INTEGER,
  live INTEGER, stale INTEGER, msgs REAL, reconnects INTEGER, errors INTEGER, last_error TEXT);
CREATE INDEX IF NOT EXISTS idx_fh_ts ON feed_health_1m(ts);

CREATE TABLE IF NOT EXISTS wallet_labels (
  chain TEXT NOT NULL, address TEXT NOT NULL, entity TEXT, entity_type TEXT, confidence TEXT,
  source TEXT, notes TEXT, added_ts REAL, PRIMARY KEY (chain, address)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS onchain_transfers (
  chain TEXT NOT NULL, tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL, ts REAL, block INTEGER,
  token TEXT, asset TEXT, from_addr TEXT, to_addr TEXT, amount REAL, usd_value REAL,
  from_entity TEXT, from_type TEXT, to_entity TEXT, to_type TEXT, classification TEXT,
  class_confidence TEXT, explanation TEXT, source TEXT,
  PRIMARY KEY (chain, tx_hash, log_index)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_otx_asset_ts ON onchain_transfers(asset, ts);
CREATE INDEX IF NOT EXISTS idx_otx_ts ON onchain_transfers(ts);

CREATE TABLE IF NOT EXISTS wallet_balances (
  chain TEXT NOT NULL, token TEXT NOT NULL, address TEXT NOT NULL, ts REAL NOT NULL, asset TEXT,
  balance REAL, source TEXT, PRIMARY KEY (chain, token, address, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_wbal_asset_ts ON wallet_balances(asset, ts);

CREATE TABLE IF NOT EXISTS intel_cursors (
  key TEXT PRIMARY KEY, value TEXT, updated_ts REAL);

CREATE TABLE IF NOT EXISTS signal_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, asset TEXT, status TEXT, score REAL,
  price REAL, fwd_15m REAL, fwd_1h REAL, fwd_4h REAL, fwd_24h REAL, filled INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_outcomes_filled ON signal_outcomes(filled, ts);

-- v0.6 history import targets (same columns as v0.6, plus uniqueness for idempotent re-import)
CREATE TABLE IF NOT EXISTS composite_history_v2 (
  ts REAL, asset TEXT, price REAL, score REAL, coverage INTEGER, confirmed_venues INTEGER,
  bid_depth_1 REAL, ask_depth_1 REAL, ask_depth_ratio REAL, buy_ratio_60s REAL, volume_60s REAL,
  volume_ratio REAL, price_change_5m_pct REAL, ask_replenishment REAL, spread_bps REAL,
  trade_confidence REAL, components_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v06_comp ON composite_history_v2(asset, ts);
CREATE TABLE IF NOT EXISTS venue_history_v2 (
  ts REAL, asset TEXT, venue TEXT, symbol TEXT, confirmed INTEGER, flags INTEGER, bid_depth_1 REAL,
  ask_depth_1 REAL, ask_depth_ratio REAL, buy_ratio_60s REAL, volume_60s REAL, volume_ratio REAL,
  spread_bps REAL, ask_replenishment REAL, discovery_volume_24h_usd REAL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v06_venue ON venue_history_v2(asset, venue, ts);
CREATE TABLE IF NOT EXISTS scanner_events_v2 (
  ts REAL, asset TEXT, event_type TEXT, level REAL, message TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v06_events ON scanner_events_v2(asset, ts, event_type, level);
CREATE TABLE IF NOT EXISTS legacy_import (
  ts REAL, source_path TEXT, source_sha256_before TEXT, source_sha256_after TEXT,
  rows_composite INTEGER, rows_venue INTEGER, rows_events INTEGER);
"""),
    (2, "v0.7.3 high-conviction alerts + discovered token contracts", """
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, asset TEXT NOT NULL, state TEXT NOT NULL,
  started_ts REAL, fired_ts REAL, updated_ts REAL, ended_ts REAL, duration_s REAL,
  evidence_score REAL, peak_evidence REAL, premove REAL, peak_premove REAL,
  price_at_fire REAL, price_at_end REAL, confirmed INTEGER, coverage INTEGER, coverage_total INTEGER,
  confirmed_venues TEXT, reasons TEXT, wallet_status TEXT, wallet_state TEXT,
  structure_score REAL, execution_score REAL, end_reason TEXT, checks TEXT);
CREATE INDEX IF NOT EXISTS idx_alerts_asset_fired ON alerts(asset, fired_ts);
CREATE INDEX IF NOT EXISTS idx_alerts_fired ON alerts(fired_ts);

CREATE TABLE IF NOT EXISTS token_contracts (
  asset TEXT PRIMARY KEY, coingecko_id TEXT, state TEXT NOT NULL, chain TEXT, contract TEXT,
  decimals INTEGER, reason TEXT, source TEXT, checked_ts REAL);
"""),
]

ALERT_COLS = ["id", "asset", "state", "started_ts", "fired_ts", "updated_ts", "ended_ts", "duration_s",
              "evidence_score", "peak_evidence", "premove", "peak_premove", "price_at_fire", "price_at_end",
              "confirmed", "coverage", "coverage_total", "confirmed_venues", "reasons", "wallet_status",
              "wallet_state", "structure_score", "execution_score", "end_reason", "checks"]
ALERT_JSON_COLS = ("confirmed_venues", "reasons", "checks")
TOKEN_CONTRACT_COLS = ["asset", "coingecko_id", "state", "chain", "contract", "decimals", "reason", "source",
                       "checked_ts"]

RETENTION = [
    # (table, config key, unit seconds[, time column - default "ts"])
    ("asset_metrics_5s", "asset_5s_hours", 3600),
    ("market_metrics_10s", "market_10s_hours", 3600),
    ("asset_metrics_1m", "asset_1m_days", 86400),
    ("market_metrics_1m", "market_1m_days", 86400),
    ("events", "events_days", 86400),
    ("alerts", "alerts_days", 86400, "fired_ts"),
    ("universe_snapshots", "universe_days", 86400),
    ("venue_selection", "universe_days", 86400),
    ("feed_health_1m", "health_days", 86400),
    ("onchain_transfers", "intel_days", 86400),
    ("wallet_balances", "intel_days", 86400),
]
