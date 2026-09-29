"""Configuration loading.

Defaults live here; `config.json` only needs to contain what you change.
v0.6 keys (`watchlist`, `coingecko_ids`, `venue_discovery`, `baseline_minutes`,
`warmup_minutes`, `sample_interval_seconds`, `persist_interval_seconds`,
`onchain`) are still understood and mapped onto the v0.7 sections, and the
v0.7 key `universe.pinned_assets` is read as `universe.manual_assets`.

The loader never overwrites an existing config.json. Secrets are never stored
in it: `*_api_key_env` / `*_url_env` keys hold environment-variable NAMES only.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

KNOWN_MM_NAMES = {
    "wintermute", "jump", "jump trading", "gsr", "dwf", "dwf labs", "amber", "amber group",
    "cumberland", "flow traders", "b2c2", "keyrock", "auros", "kairon", "galaxy digital",
    "alameda", "flowdesk", "gotbit", "mantaray", "presto",
}

DEFAULTS: Dict[str, Any] = {
    # "live" uses CoinGecko + exchanges; "sim" runs a synthetic market world
    # (offline demo and tests).
    "mode": "live",
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "broadcast_seconds": 1.0,
        "coin_broadcast_seconds": 2.0,
    },
    "universe": {
        "target_size": 100,
        "per_page": 250,
        "fetch_pages": 2,
        "refresh_minutes": 60,
        # CoinGecko unreachable (no live ranking): retry after 2, 4, 8, ... minutes, capped at refresh_minutes
        "retry_minutes": 2,
        "cache_path": "data/universe_cache.json",
        "entry_confirmations": 2,
        "exit_rank_buffer": 15,
        # initial manual assets (seeded once into SQLite; afterwards managed on the Universe page)
        "manual_assets": ["QNT", "LINK", "XDC"],
        # verified CoinGecko ids for the default manual assets (a ticker alone is never guessed)
        "coingecko_ids": {"QNT": "quant-network", "LINK": "chainlink", "XDC": "xdce-crowd-sale"},
        "exclude_symbols": [],
        "include_symbols": [],
        "exclude_coin_ids": [],
        "include_coin_ids": [],
        "exclude_categories": [
            "stablecoins", "wrapped-tokens", "bridged-tokens", "liquid-staking-tokens",
            "liquid-restaking-tokens", "tokenized-gold", "tokenized-btc", "bridged-stablecoins",
        ],
        "min_usable_venues": 1,
        "min_usable_volume_usd": 250000,
        "coingecko_api_key_env": "COINGECKO_API_KEY",
        "coingecko_api_key_type": "demo",
        "coingecko_calls_per_minute": 8,
    },
    "discovery": {
        "exchanges": [
            "binance", "okx", "bybit", "coinbase", "kraken", "kucoin", "gate", "mexc",
            "bitget", "htx", "cryptocom", "upbit", "bitfinex", "bitstamp", "bitrue",
        ],
        "max_venues_per_asset": 5,
        "refresh_minutes": 45,
        "min_market_volume_usd": 50000,
        "max_spread_pct": 1.0,
        "price_tolerance_pct": 5.0,
        "price_tolerance_krw_pct": 12.0,
        "replace_ratio": 1.5,
        "replace_confirmations": 2,
        "verify_timeout_seconds": 90,
        "wash_volume_to_depth_ratio": 4000,
        "stale_ticker_minutes": 120,
        "symbol_aliases": {},
        "excluded_exchanges": [],
        "coingecko_crosscheck_hours": 24,
    },
    "feeds": {
        "backend": "ccxt",
        "workers": 1,
        "book_limit": None,
        "book_min_interval_ms": 250,
        "stale_book_seconds": 20,
        "stale_multiplier": 5.0,
        "trade_quiet_seconds": 600,
        "resync_grace_seconds": 60,
        "backoff_initial_seconds": 1.0,
        "backoff_max_seconds": 60.0,
        "breaker_failures": 6,
        "breaker_window_seconds": 300,
        "breaker_cooldown_seconds": 300,
        "watchdog_min_seconds": 60.0,
        "exchange_overrides": {},
    },
    "engine": {
        "tick_seconds": 1.0,
        "band_pct": 2.0,
        "flow_band_pct": 1.0,
        "baseline_short_minutes": 30,
        "baseline_minutes": 120,
        "baseline_long_minutes": 1440,
        "baseline_lag_minutes": 10,
        "min_baseline_minutes": 20,
        "rehydrate_max_age_hours": 12,
        "min_valid_fraction": 0.8,
        "activity_floor_usd": [1000.0, 25000.0],
        "activity_floor_trades": [5, 30],
        "book_floor_usd": 20000.0,
        "slippage_fraction_of_depth": 0.2,
        "slippage_order_min_usd": 1000.0,
        "slippage_order_max_usd": 100000.0,
        "historical_trade_seconds": 10.0,
    },
    "scoring": {
        "weights": {"orderbook": 0.35, "cross_venue": 0.25, "buy_pressure": 0.22, "liquidity": 0.18},
        "family_active": 0.30,
        "venue_family_active": 0.40,
        "venue_confirm_families": 2,
        "venue_confirm_min_confidence": 0.35,
        "family_caps": {"0": 20, "1": 35, "2": 55, "3": 72},
        "coverage_caps": {"1": 42, "2": 72},
        "confirmed_caps": {"0": 39, "1": 59, "2": 82},
        "min_confirm_liquidity_share": 0.30,
        "liquidity_share_cap": 60,
        "partial_coverage_share": 0.5,
        "partial_cap": 60,
        "warmup_cap": 44,
        "activity_caps": [[0.2, 32], [0.4, 48]],
        "book_conf_cap": [0.3, 45],
        "compression_max_bonus": 0.10,
        "compression_ratio": 0.6,
        "context_max_points": 8.0,
        "late": {
            "hard_pct": {"15": 5.0, "30": 8.0, "60": 12.0},
            "vol_z_late": 3.0,
            "vol_min_abs_pct": 1.0,
            "down_weight": 0.7,
            "penalty_start": 0.35,
            "in_progress": 0.6,
            "min_multiplier": 0.3,
            "late_cap": 25.0,
            "fallback_sigma_pct": {"15": 1.5, "30": 2.1, "60": 3.0},
            "min_sigma_pct": {"15": 0.25, "30": 0.35, "60": 0.5},
            "min_vol_minutes": 240,
        },
        "fast_window_seconds": 30,
        "slow_window_seconds": 150,
        "confirm_hold_seconds": 120,
        "emerging_weight": 0.85,
        "status": {
            "watch": 40.0,
            "emerging": 55.0,
            "confirmed": 60.0,
            "strong": 72.0,
            "emerging_min_families": 2,
            "confirmed_min_families": 3,
            "strong_min_families": 3,
            "strong_min_liquidity_share": 0.5,
            "low_confidence": 0.3,
            "hysteresis": 5.0,
        },
        "onset_on": 0.40,
        "onset_off": 0.20,
        "onset_hold_seconds": 30,
        "propagation_window_minutes": 15,
    },
    "assets": {
        # chain / platform / contract metadata from CoinGecko coin details (shares the CoinGecko budget)
        "discovery_calls_per_minute": 2,
        "discovery_ttl_days": 30,
        "search_limit": 8,
        # manual overrides for problematic assets, e.g.
        #   {"XYZ": {"chain": "ethereum", "contract": "0x...", "decimals": 18}}
        #   {"ABC": {"chain": "xdc", "native": true}}      {"DEF": {"unsupported": "reason"}}
        "overrides": {},
    },
    "alerts": {
        "enabled": True,
        # This is a strict composite evidence alarm, not a probability of a profitable trade.
        "min_premove_with_wallet": 82.0,
        "min_premove_wallet_neutral": 86.0,
        "min_premove_no_wallet": 90.0,
        "min_confirmed_venues": 3,
        "min_live_venues": 3,
        "min_live_liquidity_share": 0.70,
        "min_selected_live_ratio": 1.0,
        "min_engine_confidence": 0.55,
        "min_orderbook": 65.0,
        "min_buy_pressure": 60.0,
        "min_cross_venue": 68.0,
        "min_vol_ratio": 1.25,
        "max_spread_anomaly": 0.65,
        "min_base_families": 3,
        "min_structure_venues": 2,
        "min_buy_venues": 2,
        "max_top_trade_share": 0.35,
        "persistence_seconds": 120,
        "clear_after_seconds": 60,
        "max_active_banners": 3,
        # v0.7.3 Signal Radar
        "watch_min_premove": 70.0,          # WATCH: mandatory gates hold and pre-move >= this
        "watch_min_confirmed_venues": 2,
        "watch_min_orderbook": 50.0,
        "watch_linger_seconds": 20,         # keep a WATCH entry briefly so the bar does not flicker
        "invalidated_display_seconds": 900, # an invalidated alert stays listed this long
        "invalidated_headline_seconds": 600,  # ... and is the headline state this long
        "persist_update_seconds": 30,       # SQLite refresh cadence of an open alert
    },
    "events": {
        "score_thresholds": [55, 70, 80],
        "score_hysteresis": 5,
        "confirm_debounce_seconds": 60,
        "status_min_gap_seconds": 60,
        "breakout_pct": {"15": 3.0, "60": 5.0},
        "precursor_lookback_minutes": 120,
        "memory_per_asset": 300,
    },
    "storage": {
        "path": "data/scanner_v07.db",
        "asset_5s_hours": 48,
        "market_10s_hours": 24,
        "asset_1m_days": 30,
        "market_1m_days": 14,
        "events_days": 90,
        "alerts_days": 365,
        "universe_days": 90,
        "health_days": 14,
        "intel_days": 90,
        "persist_asset_seconds": 5,
        "persist_market_seconds": 10,
        "writer_queue": 20000,
        "retention_every_minutes": 10,
        # consistent copy of an existing database before a schema upgrade (skipped when disk space is short)
        "backup_before_migration": True,
    },
    "intel": {
        "enabled": False,
        "etherscan_api_key_env": "ETHERSCAN_API_KEY",
        "calls_per_second": 4.0,
        "daily_call_budget": 90000,
        "labels_file": "labels/wallet_labels.csv",
        "tokens": {
            "QNT": {"chain": "ethereum", "contract": "0x4a220E6096B25EADb88358cb44068A3248254675",
                    "token_wide": "auto"},
            "LINK": {"chain": "ethereum", "contract": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
                     "token_wide": "off"},
        },
        "address_poll_min_seconds": 60,
        "balance_poll_minutes": 60,
        "token_wide_max_transfers_per_hour": 400,
        "min_event_usd": 250000,
        "max_pages_per_poll": 3,
        "page_size": 1000,
        "legacy_labels": [],
        # v0.7.3
        "warmup_minutes": 60,               # WARMING until this much transfer history is collected
        "auto_discover_contracts": True,    # track discovered chains / contracts (asset registry)
        "discovered_token_wide": "off",     # discovered tokens: address-centric only (budget-safe)
        "whale_candidate_usd": 250000,      # unlabelled counterparties above this are listed, never counted
        # v0.8 multi-chain providers. Keys: environment-variable NAMES only (never the key itself).
        # The EVM provider (Etherscan V2) uses etherscan_api_key_env / daily_call_budget / calls_per_second.
        "providers": {
            "evm": {"enabled": True},
            "bitcoin": {"enabled": True, "base_urls": ["https://mempool.space/api", "https://blockstream.info/api"],
                        "daily_call_budget": 20000, "calls_per_second": 1.0},
            "xrpl": {"enabled": True, "rpc_urls": ["https://xrplcluster.com/", "https://s1.ripple.com:51234/",
                                                   "https://s2.ripple.com:51234/"],
                     "daily_call_budget": 20000, "calls_per_second": 2.0},
            "tron": {"enabled": True, "api_key_env": "TRONGRID_API_KEY", "daily_call_budget": 10000,
                     "calls_per_second": 1.0},
            "solana": {"enabled": True, "rpc_url": "https://api.mainnet-beta.solana.com",
                       "rpc_url_env": "SOLANA_RPC_URL", "daily_call_budget": 20000, "calls_per_second": 2.0},
            "hedera": {"enabled": True, "daily_call_budget": 20000, "calls_per_second": 2.0},
            "cardano": {"enabled": True, "api_key_env": "KOIOS_API_TOKEN", "daily_call_budget": 4000,
                        "calls_per_second": 0.5},
        },
    },
    "sim": {
        "assets": 12,
        "venues_per_asset": 4,
        "seed": 7,
        "scenarios": True,
        "cycle_seconds": 3600,
        "speed": 1.0,
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _map_legacy(raw: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    """Translate v0.6 top-level keys into v0.7 sections (non-destructively)."""
    cfg = copy.deepcopy(raw)
    uni = cfg.setdefault("universe", {})
    if "pinned_assets" in uni:
        if "manual_assets" not in uni:
            uni["manual_assets"] = list(uni.get("pinned_assets") or [])
            warnings.append("universe.pinned_assets (v0.7) read as universe.manual_assets - the initial manual-asset list")
        uni.pop("pinned_assets", None)
    if isinstance(raw.get("watchlist"), list):
        manual = list(uni.get("manual_assets", DEFAULTS["universe"]["manual_assets"]))
        for sym in raw["watchlist"]:
            s = str(sym).upper()
            if s.endswith("USDT") and len(s) > 4:  # v0.1-v0.3 used Binance pairs like QNTUSDT
                s = s[:-4]
            if s not in manual:
                manual.append(s)
        uni["manual_assets"] = manual
        warnings.append("v0.6 'watchlist' mapped to universe.manual_assets")
    if isinstance(raw.get("coingecko_ids"), dict):
        ids = dict(uni.get("coingecko_ids", {}))
        ids.update({str(k).upper(): v for k, v in raw["coingecko_ids"].items()})
        uni["coingecko_ids"] = ids
    vd = raw.get("venue_discovery")
    if isinstance(vd, dict) and "max_venues_per_asset" in vd:
        cfg.setdefault("discovery", {}).setdefault("max_venues_per_asset", int(vd["max_venues_per_asset"]))
    eng = cfg.setdefault("engine", {})
    if "baseline_minutes" in raw:
        eng.setdefault("baseline_minutes", int(raw["baseline_minutes"]))
    if "warmup_minutes" in raw:
        lag = int(eng.get("baseline_lag_minutes", DEFAULTS["engine"]["baseline_lag_minutes"]))
        eng.setdefault("min_baseline_minutes", max(5, int(raw["warmup_minutes"]) - lag))
    if "sample_interval_seconds" in raw:
        cfg.setdefault("server", {}).setdefault("broadcast_seconds", float(raw["sample_interval_seconds"]))
    if "persist_interval_seconds" in raw:
        cfg.setdefault("storage", {}).setdefault("persist_asset_seconds", int(raw["persist_interval_seconds"]))
    oc = raw.get("onchain")
    if isinstance(oc, dict):
        intel = cfg.setdefault("intel", {})
        intel.setdefault("enabled", bool(oc.get("enabled", False)))
        if oc.get("etherscan_api_key_env"):
            intel.setdefault("etherscan_api_key_env", oc["etherscan_api_key_env"])
        if oc.get("poll_seconds"):
            intel.setdefault("address_poll_min_seconds", int(oc["poll_seconds"]))
        tokens = dict(intel.get("tokens", {}))
        legacy_labels = list(intel.get("legacy_labels", []))
        for asset, spec in (oc.get("assets") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("contract"):
                tokens.setdefault(str(asset).upper(), {"chain": "ethereum", "contract": spec["contract"],
                                                       "token_wide": "auto"})
            for label, addr in (spec.get("watch_wallets") or {}).items():
                is_mm = str(label).strip().lower() in KNOWN_MM_NAMES
                legacy_labels.append({
                    "chain": "ethereum", "address": str(addr).lower(), "entity": str(label),
                    "entity_type": "MM" if is_mm else "WATCH",
                    "confidence": "MEDIUM" if is_mm else "LOW",
                    "source": "v0.6 config onchain.watch_wallets",
                    "notes": f"imported from v0.6 config for {asset}",
                })
        if tokens:
            intel["tokens"] = tokens
        intel["legacy_labels"] = legacy_labels
        warnings.append("v0.6 'onchain' section mapped to intel")
    intel_raw = raw.get("intel") if isinstance(raw.get("intel"), dict) else {}
    for k in ("discovery_calls_per_minute", "discovery_ttl_days"):       # v0.7.3 intel keys -> assets
        if k in intel_raw:
            cfg.setdefault("assets", {}).setdefault(k, intel_raw[k])
    for k in ("watchlist", "coingecko_ids", "venue_discovery", "baseline_minutes", "warmup_minutes",
              "sample_interval_seconds", "persist_interval_seconds", "onchain"):
        cfg.pop(k, None)
    return cfg


def load_config(root: Path, path: Path | None = None, create_if_missing: bool = True) -> Dict[str, Any]:
    root = Path(root)
    path = Path(path) if path else root / "config.json"
    warnings: List[str] = []
    if not path.exists() and create_if_missing:
        example = root / "config.example.json"
        if example.exists():
            shutil.copyfile(example, path)  # only ever created when missing
            warnings.append(f"created {path.name} from config.example.json")
    raw: Dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # keep running on defaults, but say so loudly
            warnings.append(f"could not parse {path.name}: {exc!r}; using defaults")
            raw = {}
    cfg = deep_merge(DEFAULTS, _map_legacy(raw, warnings))
    if os.environ.get("PMS_MODE", "").lower() == "sim":
        # Offline demo: synthetic exchanges, separate database (never mixes with live history).
        cfg = deep_merge(cfg, {"mode": "sim", "feeds": {"backend": "sim"}, "storage": {"path": "data/scanner_sim.db"},
                               "intel": {"enabled": False}})
        warnings.append("PMS_MODE=sim: synthetic market data, database data/scanner_sim.db")
    cfg["_meta"] = {"root": str(root), "path": str(path), "warnings": warnings}
    return cfg


def resolve_path(cfg: Dict[str, Any], rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return Path(cfg.get("_meta", {}).get("root", ".")) / p
