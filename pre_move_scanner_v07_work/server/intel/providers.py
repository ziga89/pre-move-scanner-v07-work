"""Wallet-data providers: which service answers transfer / balance queries for which chain.

Today one provider (Etherscan V2) covers the EVM chains in `etherscan.CHAIN_IDS`.
Another chain family (e.g. Solana, TRON, XRP Ledger) or provider is added by
implementing the small interface below and registering it; the monitor, the
status resolver and contract discovery only ask the registry.

Provider interface:
    name: str                      display name
    chains: frozenset[str]         chain ids it serves (our ids, e.g. "ethereum", "bsc")
    keyed: bool                    credentials present
    supports(chain) -> bool
    async tokentx(chain, address=None, contract=None, startblock=0, page=1, offset=1000) -> list[dict]
    async tokenbalance(chain, contract, address) -> int | None
    remaining_today() -> int; daily: int; stats() -> dict
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# CoinGecko asset-platform id -> our chain id (EVM chains only).
COINGECKO_EVM_PLATFORMS = {
    "ethereum": "ethereum", "binance-smart-chain": "bsc", "polygon-pos": "polygon", "arbitrum-one": "arbitrum",
    "optimistic-ethereum": "optimism", "base": "base", "avalanche": "avalanche", "linea": "linea",
    "scroll": "scroll", "mantle": "mantle", "blast": "blast",
}
# Native gas coins of EVM chains: their transfers are not ERC-20 events.
NATIVE_EVM_COINS = {"ethereum": "Ethereum", "binancecoin": "BNB Smart Chain", "avalanche-2": "Avalanche C-Chain"}


class ProviderRegistry:
    def __init__(self, providers: List[Any]):
        self.providers = list(providers)

    def for_chain(self, chain: str) -> Optional[Any]:
        for p in self.providers:
            if p.supports(chain):
                return p
        return None

    def supports(self, chain: str) -> bool:
        return self.for_chain(chain) is not None

    def supported_chains(self) -> List[str]:
        return sorted({c for p in self.providers for c in p.chains})

    def keyed(self, chain: Optional[str] = None) -> bool:
        if chain is not None:
            p = self.for_chain(chain)
            return bool(p and p.keyed)
        return any(p.keyed for p in self.providers)

    def _need(self, chain: str) -> Any:
        p = self.for_chain(chain)
        if p is None:
            raise LookupError(f"no wallet-data provider for chain '{chain}'")
        return p

    async def tokentx(self, chain: str, **kw) -> List[Dict[str, Any]]:
        return await self._need(chain).tokentx(chain, **kw)

    async def tokenbalance(self, chain: str, contract: str, address: str) -> Optional[int]:
        return await self._need(chain).tokenbalance(chain, contract, address)

    def remaining_today(self) -> int:
        return sum(int(p.remaining_today()) for p in self.providers)

    @property
    def daily(self) -> int:
        return sum(int(p.daily) for p in self.providers)

    def stats(self) -> Dict[str, Any]:
        return {p.name: p.stats() for p in self.providers}
