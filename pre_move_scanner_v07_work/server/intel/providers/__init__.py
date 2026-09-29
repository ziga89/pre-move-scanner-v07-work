"""Wallet-data providers and the registry that routes a chain to its provider.

    evm.py      Etherscan V2: Ethereum, BSC, Base, Arbitrum, Optimism, Polygon, Avalanche, Mantle,
                Linea, Scroll, Blast, XDC Network (ERC-20 / XRC-20 tokens and native coins)
    bitcoin.py  Esplora (mempool.space / blockstream.info)
    xrpl.py     rippled JSON-RPC
    tron.py     TronGrid
    solana.py   Solana JSON-RPC
    hedera.py   Hedera mirror node
    cardano.py  Koios

Chains without a provider (Sui, Aptos, TON, NEAR, Polkadot, Stellar, Cosmos, ...) are listed in
`intel/chains.py` with `provider=None` and reported as `UNSUPPORTED · provider not implemented`.
A new provider subclasses `base.WalletProvider`, returns `RawTransfer`s and is added to `FAMILIES`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..chains import CHAINS, chain_name
from .base import (ProviderAuthError, ProviderBudgetExceeded, ProviderChainUnavailable, ProviderError,  # noqa: F401
                   ProviderRateLimited, WalletProvider)
from .bitcoin import BitcoinProvider
from .cardano import CardanoProvider
from .evm import EvmProvider
from .hedera import HederaProvider
from .solana import SolanaProvider
from .tron import TronProvider
from .xrpl import XrplProvider

FAMILIES = {"evm": EvmProvider, "bitcoin": BitcoinProvider, "xrpl": XrplProvider, "tron": TronProvider,
            "solana": SolanaProvider, "hedera": HederaProvider, "cardano": CardanoProvider}

# v0.7 re-exports (the registry used to live in intel/providers.py)
from ..chains import BY_PLATFORM as _BY_PLATFORM  # noqa: E402

COINGECKO_EVM_PLATFORMS = {pid: c.id for pid, c in _BY_PLATFORM.items() if c.family == "evm"}
NATIVE_EVM_COINS = {"ethereum": "Ethereum", "binancecoin": "BNB Smart Chain", "avalanche-2": "Avalanche C-Chain"}


def provider_config(icfg: Dict[str, Any], fam: str) -> Dict[str, Any]:
    """Provider settings from `intel.providers.<family>`; the EVM provider also reads the v0.7 keys."""
    p = dict(((icfg.get("providers") or {}).get(fam)) or {})
    if fam == "evm":
        legacy = {"api_key_env": icfg.get("etherscan_api_key_env") or "ETHERSCAN_API_KEY",
                  "daily_call_budget": icfg.get("daily_call_budget", 90000),
                  "calls_per_second": icfg.get("calls_per_second", 4.0),
                  "page_size": icfg.get("page_size", 1000), "max_pages_per_poll": icfg.get("max_pages_per_poll", 3)}
        p = {**legacy, **{k: v for k, v in p.items() if v is not None}}
    return p


def build_providers(icfg: Dict[str, Any], http: Any, budget_store: Optional[Dict[str, int]] = None,
                    **kw) -> "ProviderRegistry":
    out = []
    for fam, cls in FAMILIES.items():
        out.append(cls(http, provider_config(icfg, fam), budget_store, **kw))
    return ProviderRegistry(out)


class ProviderRegistry:
    def __init__(self, providers: List[Any]):
        self.providers = list(providers)

    # ---------------------------------------------------------------- routing
    def for_chain(self, chain: str) -> Optional[Any]:
        """The enabled provider serving `chain`."""
        for p in self.providers:
            if p.supports(chain):
                return p
        return None

    def assigned(self, chain: str) -> Optional[Any]:
        """The provider for `chain` even if it is disabled (to report *why* it is not used)."""
        p = self.for_chain(chain)
        if p is not None:
            return p
        for p in self.providers:
            if chain in getattr(p, "chains", ()):
                return p
        return None

    def by_name(self, name: str) -> Optional[Any]:
        return next((p for p in self.providers if p.name == name), None)

    def supports(self, chain: str) -> bool:
        return self.for_chain(chain) is not None

    def supported_chains(self) -> List[str]:
        return sorted({c for p in self.providers for c in p.chains if p.supports(c)})

    def keyed(self, chain: Optional[str] = None) -> bool:
        if chain is not None:
            p = self.for_chain(chain)
            return bool(p and p.keyed)
        return any(p.keyed for p in self.providers if getattr(p, "enabled", True))

    def chain_state(self, chain: str) -> Dict[str, Any]:
        """ok | off | no_key | degraded | unsupported, with the reason and the provider name."""
        c = CHAINS.get(chain)
        p = self.assigned(chain)
        if p is None:
            return {"state": "unsupported", "provider": None,
                    "reason": f"{chain_name(chain)}: provider not implemented"}
        st = p.state(chain) if hasattr(p, "state") else {"state": "ok" if p.keyed else "no_key", "reason": ""}
        return {**st, "provider": p.name, "provider_label": getattr(p, "label", p.name),
                "chain_name": c.name if c else chain}

    # ---------------------------------------------------------------- v0.7 compatible calls
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
