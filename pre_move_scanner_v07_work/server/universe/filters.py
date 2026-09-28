"""Universe exclusion rules: stablecoins, wrapped / staked / bridged
representations, tokenised commodities, duplicates.

CoinGecko category membership is the primary signal; the static lists and
name patterns are a fallback for when category calls fail or lag behind.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Set

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1", "RLUSD", "USDD", "FRAX",
    "GUSD", "USDP", "BUSD", "LUSD", "CRVUSD", "GHO", "USDB", "USDX", "USD0", "USDA", "EURC", "EURT",
    "EURS", "USTC", "BSC-USD", "USDTB", "USDG", "USR", "DOLA", "SUSD", "USDF", "AUSD", "USDO", "FXUSD",
    "BFUSD", "USDY", "OUSG", "BUIDL", "USYC", "SYRUPUSDC", "USDL", "USDQ", "XUSD", "MIM", "RUSD",
}
GOLD_SYMBOLS = {"PAXG", "XAUT", "KAU", "XAUM", "CGO"}
WRAPPED_SYMBOLS = {
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "EETH", "CBBTC", "RETH", "CBETH", "WBETH", "BETH",
    "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "INF", "HSOL", "BBSOL", "LBTC", "SOLVBTC", "SOLVBTC.BBN",
    "EZETH", "RSETH", "METH", "CMETH", "SUSDE", "SUSDS", "SDAI", "SFRAX", "BTCB", "TBTC", "OSETH",
    "SWETH", "LSETH", "ETHX", "PUFETH", "RSWETH", "BBTC", "CLBTC", "FBTC", "UNIBTC", "PUMPBTC", "EBTC",
    "WBNB", "WTRX", "WAVAX", "WMATIC", "WPOL", "WSOL", "WHYPE", "STHYPE", "KHYPE", "SAVAX", "STS",
    "WS", "STKAAVE", "SAVUSD", "WSTUSR", "SUSDA", "SUSDX", "STUSDT", "WUSDM", "USDM", "OETH", "ANKRETH",
    "FRXETH", "SFRXETH", "WEETHS", "AGETH", "RSTETH", "STBTC", "ENZOBTC", "XSOLVBTC", "BTC.B", "WBT.B",
    "STX.B", "VBTC", "VETH", "VBNB", "SLISBNB", "ASBNB", "STONE", "WSTHYPE", "LSTHYPE", "SYRUPUSDT",
}
NAME_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bwrapped\b", r"\bbridged\b", r"\bstaked\b", r"liquid staked", r"liquid staking", r"\bpeg\b",
        r"binance-peg", r"\(wormhole\)", r"\(portal\)", r"\brestaked\b", r"\bsavings\b", r"tokenized",
        r"\bvault\b",
    )
]
STABLE_NAME = re.compile(r"\b(usd|dollar|euro|eur|stable)\b", re.I)


def classify_exclusion(row: Dict[str, Any], category_ids: Dict[str, Set[str]], ucfg: Dict[str, Any]
                       ) -> Optional[str]:
    """Return an exclusion reason, or None if the coin is eligible."""
    cid = str(row.get("id") or "")
    sym = str(row.get("symbol") or "").upper()
    name = str(row.get("name") or "")
    if sym in {s.upper() for s in ucfg.get("include_symbols", [])} or cid in set(ucfg.get("include_coin_ids", [])):
        return None
    if sym in {s.upper() for s in ucfg.get("exclude_symbols", [])} or cid in set(ucfg.get("exclude_coin_ids", [])):
        return "excluded by config"
    for cat, ids in (category_ids or {}).items():
        if cid in ids:
            if "stable" in cat:
                return "stablecoin"
            if "gold" in cat:
                return "tokenized gold"
            return f"wrapped/staked/bridged ({cat})"
    if sym in STABLE_SYMBOLS:
        return "stablecoin"
    price = row.get("current_price")
    ch24 = row.get("price_change_percentage_24h")
    if (isinstance(price, (int, float)) and 0.97 <= price <= 1.03 and STABLE_NAME.search(f"{name} {sym}")
            and (ch24 is None or abs(ch24) < 1.0)):
        return "stablecoin (peg heuristic)"
    if sym in GOLD_SYMBOLS:
        return "tokenized gold"
    if sym in WRAPPED_SYMBOLS:
        return "wrapped/staked/bridged representation"
    for pat in NAME_PATTERNS:
        if pat.search(name):
            return "wrapped/staked/bridged representation"
    return None
