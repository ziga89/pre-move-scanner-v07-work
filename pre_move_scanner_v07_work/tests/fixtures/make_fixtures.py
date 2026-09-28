"""Generate deterministic discovery fixtures (run: python tests/fixtures/make_fixtures.py).

The coin list and prices are illustrative (roughly shaped like a real top-150
by market cap), not live data. Edge cases are deliberate:
  * stablecoins, wrapped/staked/bridged tokens and tokenised gold near the top
  * LEO / WBT / PI: no usable spot venue among supported exchanges
  * XDC: NOT listed on Binance; biggest on Bitrue / KuCoin / Gate
  * MEXC 'QNT/USDT' is a *different* token (price collision)
  * HTX XDC ticker is stale; Gate QNT spread is 3 %
  * Upbit KRW pairs carry a ~2 % premium; Kraken BTC/EUR, Binance BTC/TRY for FX
  * a low-ranked coin re-using the LINK ticker (duplicate symbol)
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = 1_760_000_000.0

COINS = [
    # id, symbol, name, price
    ("bitcoin", "btc", "Bitcoin", 112000.0), ("ethereum", "eth", "Ethereum", 4100.0),
    ("tether", "usdt", "Tether", 1.0002), ("ripple", "xrp", "XRP", 2.85), ("binancecoin", "bnb", "BNB", 980.0),
    ("solana", "sol", "Solana", 205.0), ("usd-coin", "usdc", "USDC", 0.9999), ("tron", "trx", "TRON", 0.34),
    ("dogecoin", "doge", "Dogecoin", 0.23), ("cardano", "ada", "Cardano", 0.80),
    ("staked-ether", "steth", "Lido Staked Ether", 4098.0), ("wrapped-bitcoin", "wbtc", "Wrapped Bitcoin", 111900.0),
    ("hyperliquid", "hype", "Hyperliquid", 46.0), ("chainlink", "link", "Chainlink", 21.5),
    ("wrapped-steth", "wsteth", "Wrapped stETH", 4980.0), ("bitcoin-cash", "bch", "Bitcoin Cash", 560.0),
    ("stellar", "xlm", "Stellar", 0.36), ("sui", "sui", "Sui", 3.3), ("ethena-usde", "usde", "Ethena USDe", 1.0005),
    ("wrapped-eeth", "weeth", "Wrapped eETH", 4400.0), ("leo-token", "leo", "LEO Token", 9.6),
    ("avalanche-2", "avax", "Avalanche", 29.0), ("hedera-hashgraph", "hbar", "Hedera", 0.22),
    ("litecoin", "ltc", "Litecoin", 105.0), ("the-open-network", "ton", "Toncoin", 2.8),
    ("shiba-inu", "shib", "Shiba Inu", 0.0000125), ("monero", "xmr", "Monero", 290.0), ("weth", "weth", "WETH", 4101.0),
    ("coinbase-wrapped-btc", "cbbtc", "Coinbase Wrapped BTC", 111950.0), ("dai", "dai", "Dai", 1.0),
    ("polkadot", "dot", "Polkadot", 4.1), ("usds", "usds", "USDS", 0.9998), ("bitget-token", "bgb", "Bitget Token", 4.6),
    ("binance-bridged-usdt-bnb-smart-chain", "bsc-usd", "Binance Bridged USDT (BNB Smart Chain)", 1.0),
    ("uniswap", "uni", "Uniswap", 8.2), ("ethena", "ena", "Ethena", 0.62), ("pepe", "pepe", "Pepe", 0.0000102),
    ("aave", "aave", "Aave", 290.0), ("crypto-com-chain", "cro", "Cronos", 0.20), ("okb", "okb", "OKB", 190.0),
    ("bittensor", "tao", "Bittensor", 330.0), ("near", "near", "NEAR Protocol", 2.9),
    ("ethereum-classic", "etc", "Ethereum Classic", 19.5), ("ondo-finance", "ondo", "Ondo", 0.92),
    ("aptos", "apt", "Aptos", 4.4), ("internet-computer", "icp", "Internet Computer", 4.6),
    ("pi-network", "pi", "Pi Network", 0.35), ("mantle", "mnt", "Mantle", 1.6), ("whitebit", "wbt", "WhiteBIT Coin", 42.0),
    ("kaspa", "kas", "Kaspa", 0.08), ("pax-gold", "paxg", "PAX Gold", 3650.0), ("tether-gold", "xaut", "Tether Gold", 3645.0),
    ("cosmos", "atom", "Cosmos Hub", 4.5), ("vechain", "vet", "VeChain", 0.024), ("render-token", "render", "Render", 3.6),
    ("arbitrum", "arb", "Arbitrum", 0.48), ("algorand", "algo", "Algorand", 0.23), ("filecoin", "fil", "Filecoin", 2.4),
    ("sei-network", "sei", "Sei", 0.30), ("worldcoin-wld", "wld", "Worldcoin", 1.3),
    ("first-digital-usd", "fdusd", "First Digital USD", 0.999), ("jupiter-exchange-solana", "jup", "Jupiter", 0.48),
    ("bonk", "bonk", "Bonk", 0.000021), ("polygon-ecosystem-token", "pol", "POL (ex-MATIC)", 0.26),
    ("injective-protocol", "inj", "Injective", 13.0), ("optimism", "op", "Optimism", 0.72), ("blockstack", "stx", "Stacks", 0.62),
    ("fetch-ai", "fet", "Artificial Superintelligence Alliance", 0.62), ("the-graph", "grt", "The Graph", 0.088),
    ("quant-network", "qnt", "Quant", 108.0), ("immutable-x", "imx", "Immutable", 0.55), ("theta-token", "theta", "Theta Network", 0.78),
    ("gala", "gala", "GALA", 0.016), ("the-sandbox", "sand", "The Sandbox", 0.28), ("curve-dao-token", "crv", "Curve DAO", 0.78),
    ("sky", "sky", "Sky", 0.07), ("lido-dao", "ldo", "Lido DAO", 1.2), ("flare-networks", "flr", "Flare", 0.022),
    ("kucoin-shares", "kcs", "KuCoin Token", 14.8), ("jasmycoin", "jasmy", "JasmyCoin", 0.015), ("pendle", "pendle", "Pendle", 4.9),
    ("ethena-staked-usde", "susde", "Ethena Staked USDe", 1.19), ("usd1-wlfi", "usd1", "USD1", 1.0),
    ("paypal-usd", "pyusd", "PayPal USD", 0.9997), ("iota", "iota", "IOTA", 0.19), ("tezos", "xtz", "Tezos", 0.72),
    ("eos", "eos", "EOS", 0.70), ("neo", "neo", "NEO", 6.4), ("floki", "floki", "FLOKI", 0.000095),
    ("dogwifcoin", "wif", "dogwifhat", 0.85), ("pyth-network", "pyth", "Pyth Network", 0.16), ("raydium", "ray", "Raydium", 3.1),
    ("conflux-token", "cfx", "Conflux", 0.17), ("nexo", "nexo", "NEXO", 1.3), ("zcash", "zec", "Zcash", 58.0),
    ("decentraland", "mana", "Decentraland", 0.30), ("axie-infinity", "axs", "Axie Infinity", 2.4), ("chiliz", "chz", "Chiliz", 0.04),
    ("official-trump", "trump", "Official Trump", 8.5), ("bitcoin-cash-sv", "bsv", "Bitcoin SV", 27.0), ("mina-protocol", "mina", "Mina", 0.19),
    ("starknet", "strk", "Starknet", 0.13), ("xdce-crowd-sale", "xdc", "XDC Network", 0.078), ("helium", "hnt", "Helium", 2.7),
    ("ecash", "xec", "eCash", 0.000021), ("kava", "kava", "Kava", 0.36), ("dexe", "dexe", "DeXe", 9.0),
    ("aerodrome-finance", "aero", "Aerodrome Finance", 1.1), ("oasis-network", "rose", "Oasis", 0.028),
    ("compound-governance-token", "comp", "Compound", 45.0), ("1inch", "1inch", "1inch", 0.27), ("zksync", "zk", "ZKsync", 0.065),
    ("ethereum-name-service", "ens", "Ethereum Name Service", 22.0), ("celestia", "tia", "Celestia", 1.6),
    ("apecoin", "ape", "ApeCoin", 0.58), ("synthetix-network-token", "snx", "Synthetix", 0.72), ("dash", "dash", "Dash", 23.0),
    ("theta-fuel", "tfuel", "Theta Fuel", 0.036), ("safe", "safe", "Safe", 0.45), ("zilliqa", "zil", "Zilliqa", 0.012),
    ("ankr", "ankr", "Ankr", 0.018), ("basic-attention-token", "bat", "Basic Attention", 0.16), ("gmx", "gmx", "GMX", 15.0),
    ("fake-link-clone", "link", "Link Clone Token", 0.0031),
]

EXCHANGES = ["binance", "okx", "bybit", "coinbase", "kraken", "kucoin", "gate", "mexc", "bitget", "htx", "upbit", "bitrue"]
NOT_LISTED = {
    "binance": {"XDC", "LEO", "WBT", "PI", "KAS", "OKB", "BGB", "KCS", "XMR", "MNT", "CRO", "HYPE", "XAUT", "BSV"},
    "okx": {"XDC", "WBT", "PI", "BGB", "KCS", "XMR", "LEO", "HNT"},
    "bybit": {"XDC", "WBT", "LEO", "OKB", "KCS", "XMR", "BSV"},
    "coinbase": {"XDC", "WBT", "LEO", "PI", "KAS", "OKB", "BGB", "KCS", "XMR", "MNT", "TRX", "TON", "HYPE", "BSV", "CFX", "NEXO", "PYTH", "DEXE"},
    "kraken": {"XDC", "WBT", "PI", "OKB", "BGB", "KCS", "XMR", "LEO", "HYPE", "DEXE"},
    "kucoin": {"WBT", "LEO", "OKB", "BGB", "XMR"},
    "gate": {"WBT", "LEO"},
    "mexc": {"WBT", "LEO"},
    "bitget": {"WBT", "LEO", "OKB", "KCS"},
    "htx": {"WBT", "LEO", "OKB", "BGB", "KCS"},
    "upbit": set(),
    "bitrue": set(),
}
UPBIT = {"BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "LINK", "SUI", "AVAX", "HBAR", "SHIB", "PEPE", "ONDO", "ENA", "QNT", "XLM", "TRX"}
BITRUE = {"XDC", "XRP", "BTC", "ETH", "HBAR", "FLR", "QNT", "XLM"}
EXTRA_VOL = {("bitrue", "XDC"): 12e6, ("kucoin", "XDC"): 5.2e6, ("gate", "XDC"): 3.1e6, ("htx", "XDC"): 2.2e6,
             ("mexc", "XDC"): 1.5e6, ("bitget", "XDC"): 0.9e6, ("upbit", "XRP"): 900e6}


def h01(*parts) -> float:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def main():
    rows = []
    for i, (cid, sym, name, px) in enumerate(COINS):
        rank = i + 1 if cid != "fake-link-clone" else 400
        mcap = 2.2e12 / (rank ** 1.25)
        rows.append({"id": cid, "symbol": sym, "name": name, "market_cap_rank": rank, "current_price": px,
                     "market_cap": mcap, "total_volume": mcap * 0.04, "price_change_percentage_24h": round((h01(cid, "ch") - 0.5) * 8, 3)})
    (HERE / "coingecko_markets.json").write_text(json.dumps(rows, indent=1))

    catalogs = []
    for ex in EXCHANGES:
        markets, tickers = {}, {}
        for i, (cid, sym, name, px) in enumerate(COINS):
            S = sym.upper()
            if cid == "fake-link-clone" or S in {"USDT", "USDC", "USDE", "DAI", "USDS", "FDUSD", "USD1", "PYUSD", "BSC-USD"}:
                continue
            if ex == "upbit" and S not in UPBIT:
                continue
            if ex == "bitrue" and S not in BITRUE:
                continue
            if S in NOT_LISTED.get(ex, set()):
                continue
            quotes = {"coinbase": ["USD"], "kraken": ["USD", "EUR"], "upbit": ["KRW"]}.get(ex, ["USDT", "USDC"])
            if S in ("USDT", "USDC"):
                continue
            rank = i + 1
            base_vol = 2e9 / (rank ** 1.35)
            for j, q in enumerate(quotes):
                sym_ex = f"{S}/{q}"
                share = (0.3 + h01(ex, S) * 1.2) * (1.0 if j == 0 else 0.15)
                vol_usd = EXTRA_VOL.get((ex, S), base_vol * share)
                fx = {"USD": 1.0, "USDT": 1.0002, "USDC": 0.9999, "EUR": 1.17, "KRW": 1 / 1390.0}[q]
                premium = 1.02 if q == "KRW" else 1.0
                last_usd = px * (1 + (h01(ex, S, "p") - 0.5) * 0.004) * premium
                last = last_usd / fx
                spread = 0.0008
                if ex == "gate" and S == "QNT":
                    spread = 0.03
                tickers[sym_ex] = {"last": last, "bid": last * (1 - spread / 2), "ask": last * (1 + spread / 2),
                                   "quoteVolume": vol_usd / fx, "baseVolume": vol_usd / last_usd,
                                   "timestamp": NOW - 30 if not (ex == "htx" and S == "XDC") else NOW - 5 * 3600}
                markets[sym_ex] = {"base": S, "quote": q, "active": True, "spot": True}
        # FX helpers and deliberate traps
        if ex == "kraken":
            markets["BTC/EUR"] = {"base": "BTC", "quote": "EUR", "active": True, "spot": True}
            tickers["BTC/EUR"] = {"last": 112000.0 / 1.17, "bid": None, "ask": None, "quoteVolume": 4e8 / 1.17, "baseVolume": 3500, "timestamp": NOW}
        if ex == "binance":
            markets["BTC/TRY"] = {"base": "BTC", "quote": "TRY", "active": True, "spot": True}
            tickers["BTC/TRY"] = {"last": 112000.0 * 41.5, "bid": None, "ask": None, "quoteVolume": 2e8 * 41.5, "baseVolume": 1800, "timestamp": NOW}
            markets["QNT/TRY"] = {"base": "QNT", "quote": "TRY", "active": True, "spot": True}
            tickers["QNT/TRY"] = {"last": 108.0 * 41.5, "bid": None, "ask": None, "quoteVolume": 3e5 * 41.5, "baseVolume": 2800, "timestamp": NOW}
        if ex == "mexc":
            tickers["QNT/USDT"] = {"last": 0.0121, "bid": 0.012, "ask": 0.0122, "quoteVolume": 9e7, "baseVolume": 7e9, "timestamp": NOW}
        if ex == "kucoin":
            markets["LUNA/USDT"] = {"base": "LUNA", "quote": "USDT", "active": False, "spot": True}
            tickers["LUNA/USDT"] = {"last": 0.2, "quoteVolume": 1e6, "timestamp": NOW}
        catalogs.append({"exchange": ex, "markets": markets, "tickers": tickers, "fetched_ts": NOW, "error": ""})
    (HERE / "exchange_catalogs.json").write_text(json.dumps(catalogs))
    print(len(rows), "coins;", sum(len(c["markets"]) for c in catalogs), "markets")


if __name__ == "__main__":
    main()
