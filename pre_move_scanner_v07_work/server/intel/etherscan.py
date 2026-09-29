"""v0.7 import path of the Etherscan V2 client. Since v0.8 it lives in `providers/evm.py`
(the EVM wallet provider); this module re-exports it for existing imports."""
from __future__ import annotations

from .providers.evm import (BASE, CHAIN_IDS, EtherscanAuthError, EtherscanBudgetExceeded,  # noqa: F401
                            EtherscanChainUnavailable, EtherscanClient, EtherscanError, EtherscanRateLimited,
                            EvmProvider, log_index_of)
