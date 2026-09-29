"""Chain metadata: which chain a CoinGecko coin / token platform belongs to and which provider
family can serve it.

`provider` names the provider family that implements the chain in `server/intel/providers/`.
`None` means no provider exists yet: assets on that chain are reported as
`UNSUPPORTED · provider not implemented` (never faked, never silently N/A).

CoinGecko ids are used exactly as CoinGecko publishes them (`asset_platform_id` for tokens,
coin id for native coins). A coin id that is not listed here and has no token platform is a
native coin of a chain without a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Chain:
    id: str                         # our chain id
    name: str                       # display name
    family: str                     # evm | bitcoin | solana | xrpl | tron | hedera | cardano | other
    cg_platform: Optional[str]      # CoinGecko asset_platform_id of tokens on this chain
    native_coin: Optional[str]      # CoinGecko coin id of the chain's native coin (None: not tracked as native)
    native_symbol: str
    native_decimals: int
    provider: Optional[str]         # provider family implementing the chain (None: not implemented)


_CHAINS = [
    # EVM (Etherscan V2 multichain API)
    Chain("ethereum", "Ethereum", "evm", "ethereum", "ethereum", "ETH", 18, "evm"),
    Chain("bsc", "BNB Smart Chain", "evm", "binance-smart-chain", "binancecoin", "BNB", 18, "evm"),
    Chain("base", "Base", "evm", "base", None, "ETH", 18, "evm"),
    Chain("arbitrum", "Arbitrum One", "evm", "arbitrum-one", None, "ETH", 18, "evm"),
    Chain("optimism", "OP Mainnet", "evm", "optimistic-ethereum", None, "ETH", 18, "evm"),
    Chain("polygon", "Polygon PoS", "evm", "polygon-pos", None, "POL", 18, "evm"),
    Chain("avalanche", "Avalanche C-Chain", "evm", "avalanche", "avalanche-2", "AVAX", 18, "evm"),
    Chain("mantle", "Mantle", "evm", "mantle", None, "MNT", 18, "evm"),
    Chain("linea", "Linea", "evm", "linea", None, "ETH", 18, "evm"),
    Chain("scroll", "Scroll", "evm", "scroll", None, "ETH", 18, "evm"),
    Chain("blast", "Blast", "evm", "blast", None, "ETH", 18, "evm"),
    Chain("xdc", "XDC Network", "evm", "xdc-network", "xdce-crowd-sale", "XDC", 18, "evm"),
    # non-EVM chains with a provider
    Chain("bitcoin", "Bitcoin", "bitcoin", None, "bitcoin", "BTC", 8, "bitcoin"),
    Chain("solana", "Solana", "solana", "solana", "solana", "SOL", 9, "solana"),
    Chain("xrpl", "XRP Ledger", "xrpl", "xrp", "ripple", "XRP", 6, "xrpl"),
    Chain("tron", "TRON", "tron", "tron", "tron", "TRX", 6, "tron"),
    Chain("hedera", "Hedera", "hedera", "hedera-hashgraph", "hedera-hashgraph", "HBAR", 8, "hedera"),
    Chain("cardano", "Cardano", "cardano", "cardano", "cardano", "ADA", 6, "cardano"),
    # known chains without a provider yet (explicit reason instead of a bare UNSUPPORTED)
    Chain("sui", "Sui", "other", "sui", "sui", "SUI", 9, None),
    Chain("aptos", "Aptos", "other", "aptos", "aptos", "APT", 8, None),
    Chain("ton", "TON", "other", "the-open-network", "the-open-network", "TON", 9, None),
    Chain("near", "NEAR", "other", "near-protocol", "near", "NEAR", 24, None),
    Chain("polkadot", "Polkadot", "other", "polkadot", "polkadot", "DOT", 10, None),
    Chain("stellar", "Stellar", "other", "stellar", "stellar", "XLM", 7, None),
    Chain("cosmos", "Cosmos Hub", "other", "cosmos", "cosmos", "ATOM", 6, None),
    Chain("algorand", "Algorand", "other", "algorand", "algorand", "ALGO", 6, None),
    Chain("litecoin", "Litecoin", "other", None, "litecoin", "LTC", 8, None),
    Chain("dogecoin", "Dogecoin", "other", None, "dogecoin", "DOGE", 8, None),
    Chain("bitcoin-cash", "Bitcoin Cash", "other", None, "bitcoin-cash", "BCH", 8, None),
    Chain("ethereum-classic", "Ethereum Classic", "other", "ethereum-classic", "ethereum-classic", "ETC", 18, None),
    Chain("internet-computer", "Internet Computer", "other", "internet-computer", "internet-computer", "ICP", 8, None),
    Chain("monero", "Monero", "other", None, "monero", "XMR", 12, None),
    Chain("tezos", "Tezos", "other", "tezos", "tezos", "XTZ", 6, None),
]

CHAINS: Dict[str, Chain] = {c.id: c for c in _CHAINS}
BY_PLATFORM: Dict[str, Chain] = {c.cg_platform: c for c in _CHAINS if c.cg_platform}
BY_NATIVE_COIN: Dict[str, Chain] = {c.native_coin: c for c in _CHAINS if c.native_coin}
PROVIDER_FAMILIES = ("evm", "bitcoin", "solana", "xrpl", "tron", "hedera", "cardano")


def chain(cid: Optional[str]) -> Optional[Chain]:
    return CHAINS.get(str(cid or "").lower())


def family(cid: Optional[str]) -> Optional[str]:
    c = chain(cid)
    return c.family if c else None


def chain_name(cid: Optional[str]) -> str:
    c = chain(cid)
    return c.name if c else str(cid or "?")


def chains_for_provider(provider: str):
    return sorted(c.id for c in _CHAINS if c.provider == provider)
