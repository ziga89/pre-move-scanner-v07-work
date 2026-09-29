"""EVM chains through the Etherscan V2 multichain API (one key for every chain).

* ERC-20 tokens: `account/tokentx` by address - one call covers every tracked token on the chain.
* Native coins (ETH on Ethereum, BNB on BSC, AVAX on Avalanche, XDC on XDC Network): `account/txlist`
  by address (successful transactions with a value). Value moved by contract-internal calls
  (`txlistinternal`) is not included; this is stated in the coverage note.
* Balances: `account/tokenbalance` (tokens, decimals known) and `account/balance` (native).
* Token-wide polling (`tokentx` by contract) for tokens with manageable activity.

The first poll of an address reads the newest page (`sort=desc`) and then catches up ascending from
there, so a busy exchange wallet is never paged from genesis. Etherscan errors are mapped: invalid key
→ auth, rate limit → back-off, "not supported for this chain / upgrade your plan" → the chain is
unavailable on this API plan (reported per chain, shown as DEGRADED with Etherscan's own text).
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from ..addr import evm_address
from ..chains import CHAINS
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset
from .base import (ProviderAuthError, ProviderBudgetExceeded, ProviderChainUnavailable, ProviderError,
                   ProviderRateLimited, WalletProvider)

BASE = "https://api.etherscan.io/v2/api"
CHAIN_IDS = {"ethereum": 1, "bsc": 56, "polygon": 137, "arbitrum": 42161, "optimism": 10, "base": 8453,
             "avalanche": 43114, "linea": 59144, "scroll": 534352, "mantle": 5000, "blast": 81457, "xdc": 50}
PLAN_MARKERS = ("not supported for this chain", "upgrade your api plan", "invalid chainid", "chain not supported",
                "unsupported chain")


class EtherscanError(ProviderError):
    pass


class EtherscanAuthError(EtherscanError, ProviderAuthError):
    pass


class EtherscanBudgetExceeded(EtherscanError, ProviderBudgetExceeded):
    pass


class EtherscanRateLimited(EtherscanError, ProviderRateLimited):
    pass


class EtherscanChainUnavailable(EtherscanError, ProviderChainUnavailable):
    pass


def log_index_of(x: Dict[str, Any]) -> int:
    li = x.get("logIndex")
    try:
        return int(li)
    except (TypeError, ValueError):
        h = hashlib.sha1(f"{x.get('from')}|{x.get('to')}|{x.get('value')}|{x.get('contractAddress')}".encode()).hexdigest()
        return int(h[:8], 16)


class EvmProvider(WalletProvider):
    """Transfer-data provider for the EVM chains in CHAIN_IDS (Etherscan V2)."""
    name = "etherscan"
    label = "Etherscan V2"
    family = "evm"
    requires_key = True
    default_daily = 90000
    default_per_second = 4.0

    def __init__(self, http: Any, pcfg: Optional[Dict[str, Any]] = None, budget_store: Optional[Dict[str, int]] = None,
                 **kw):
        pcfg = dict(pcfg or {})
        # v0.7 intel section keys are still understood
        pcfg.setdefault("api_key_env", pcfg.get("etherscan_api_key_env") or "ETHERSCAN_API_KEY")
        pcfg.setdefault("chains", sorted(CHAIN_IDS))
        # the v0.7 budget counter key ("budget:YYYYMMDD") keeps counting across the upgrade
        super().__init__(http, pcfg, budget_store, day_key="budget:{day}", **kw)
        self.base_url = str(self.cfg.get("base_url") or BASE)
        self.page_size = int(self.cfg.get("page_size", 1000))
        self.max_pages = int(self.cfg.get("max_pages_per_poll", 3))

    # v0.7 compatibility
    @property
    def calls(self) -> int:
        return self.budget.calls

    @property
    def errors(self) -> int:
        return self.budget.errors

    @property
    def last_error(self) -> str:
        return self.budget.last_error

    def day_key(self) -> str:
        return self.budget.day_key()

    def used_today(self) -> int:
        return self.budget.used_today()

    def supports(self, chain: str) -> bool:
        return self.enabled and chain in self.chains and chain in CHAIN_IDS

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return evm_address(address)

    def assets_per_call(self) -> str:
        return "chain"

    def supports_token_wide(self) -> bool:
        return True

    # ---------------------------------------------------------------- raw API
    async def call(self, chain: str, params: Dict[str, Any]) -> Any:
        if not self.key:
            raise EtherscanAuthError("Etherscan API key missing")
        if chain not in CHAIN_IDS:
            raise EtherscanError(f"chain '{chain}' not supported by Etherscan V2 in this build")
        try:
            await self.budget.acquire()
        except ProviderBudgetExceeded as exc:
            raise EtherscanBudgetExceeded(str(exc)) from None
        except ProviderRateLimited as exc:
            raise EtherscanRateLimited(str(exc)) from None
        q = {"chainid": CHAIN_IDS[chain], "apikey": self.key, **params}
        try:
            data = await self.http.get_json(self.base_url, params=q)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 429:
                self.budget.rate_limited(getattr(exc, "retry_after", None), "HTTP 429 from Etherscan")
                raise EtherscanRateLimited(str(exc)) from None
            self.budget.fail(f"{type(exc).__name__}: {exc}")
            raise EtherscanError(str(exc)) from None
        if isinstance(data, dict) and "jsonrpc" in data and "status" not in data:     # module=proxy answers
            if data.get("error"):
                err = data["error"]
                text = str(err.get("message") if isinstance(err, dict) else err)
                self.budget.fail(text)
                raise EtherscanError(text)
            self.chain_errors.pop(chain, None)
            self.budget.ok()
            return data.get("result")
        status, msg, result = str(data.get("status")), str(data.get("message", "")), data.get("result")
        if status == "1":
            self.chain_errors.pop(chain, None)
            self.budget.ok()
            return result
        if "no transactions found" in msg.lower() or (isinstance(result, list) and not result):
            self.chain_errors.pop(chain, None)
            self.budget.ok()
            return []
        text = f"{msg}: {result}" if isinstance(result, str) else msg
        low = text.lower()
        if "invalid api key" in low or ("missing" in low and "key" in low):
            self.budget.fail(text)
            raise EtherscanAuthError(text)
        if "rate limit" in low:
            self.budget.rate_limited(2.0, f"Etherscan: {text[:120]}")
            raise EtherscanRateLimited(text)
        self.budget.fail(text)
        self.chain_errors[chain] = f"Etherscan ({chain}): {text[:200]}"
        if any(m in low for m in PLAN_MARKERS):
            raise EtherscanChainUnavailable(text)
        raise EtherscanError(text)

    async def tokentx(self, chain: str, address: Optional[str] = None, contract: Optional[str] = None,
                      startblock: int = 0, page: int = 1, offset: int = 1000, sort: str = "asc") -> List[Dict[str, Any]]:
        p: Dict[str, Any] = {"module": "account", "action": "tokentx", "startblock": startblock,
                             "endblock": 99999999, "page": page, "offset": offset, "sort": sort}
        if address:
            p["address"] = address
        if contract:
            p["contractaddress"] = contract
        res = await self.call(chain, p)
        return res if isinstance(res, list) else []

    async def txlist(self, chain: str, address: str, startblock: int = 0, page: int = 1, offset: int = 1000,
                     sort: str = "asc") -> List[Dict[str, Any]]:
        res = await self.call(chain, {"module": "account", "action": "txlist", "address": address,
                                      "startblock": startblock, "endblock": 99999999, "page": page,
                                      "offset": offset, "sort": sort})
        return res if isinstance(res, list) else []

    async def tokenbalance(self, chain: str, contract: str, address: str) -> Optional[int]:
        res = await self.call(chain, {"module": "account", "action": "tokenbalance", "contractaddress": contract,
                                      "address": address, "tag": "latest"})
        try:
            return int(res)
        except (TypeError, ValueError):
            return None

    async def native_balance(self, chain: str, address: str) -> Optional[int]:
        res = await self.call(chain, {"module": "account", "action": "balance", "address": address, "tag": "latest"})
        try:
            return int(res)
        except (TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- paging
    async def _pages(self, fetch, cursor_block: Optional[int]) -> Tuple[List[Dict[str, Any]], int, bool]:
        """Seed with the newest page when there is no cursor, then page ascending by start block."""
        rows: List[Dict[str, Any]] = []
        start = cursor_block
        if start is None:
            newest = await fetch(0, "desc")
            rows.extend(newest)
            start = max((int(r.get("blockNumber") or 0) for r in newest), default=0)
            if not newest:
                return rows, 0, True
        complete = False
        for _ in range(max(1, self.max_pages)):
            page = await fetch(start, "asc")
            rows.extend(page)
            if page:
                start = max(start, max(int(r.get("blockNumber") or 0) for r in page))
            if len(page) < self.page_size:
                complete = True
                break
        return rows, start, complete

    # ---------------------------------------------------------------- interface
    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        out = FetchResult(cursor=dict(cursor))
        tokens = {a.token: a for a in assets if not a.native and a.token}
        natives = [a for a in assets if a.native]
        if tokens:
            rows, start, done = await self._pages(
                lambda sb, srt: self.tokentx(chain, address=address, startblock=sb, offset=self.page_size, sort=srt),
                cursor.get("tok", cursor.get("block")))
            out.cursor["tok"] = start
            out.complete = out.complete and done
            for x in rows:
                a = tokens.get(str(x.get("contractAddress") or "").lower())
                if a is None:
                    continue
                out.transfers.append(self._token_row(chain, a, x))
        if natives:
            a = natives[0]
            rows, start, done = await self._pages(
                lambda sb, srt: self.txlist(chain, address, startblock=sb, offset=self.page_size, sort=srt),
                cursor.get("nat"))
            out.cursor["nat"] = start
            out.complete = out.complete and done
            for x in rows:
                if str(x.get("isError", "0")) != "0" or str(x.get("txreceipt_status", "1")) == "0":
                    continue
                try:
                    wei = int(x.get("value") or 0)
                except ValueError:
                    continue
                if wei <= 0:
                    continue
                c = CHAINS.get(chain)
                out.transfers.append(RawTransfer(
                    chain=chain, asset=a.asset, token=NATIVE, tx_hash=str(x.get("hash")), index=-1,
                    ts=float(x.get("timeStamp") or 0), from_address=str(x.get("from", "")).lower(),
                    to_address=str(x.get("to", "")).lower(), amount=wei / 1e18, block=int(x.get("blockNumber") or 0),
                    symbol=c.native_symbol if c else None, decimals=18, provider=self.name))
        if natives:
            out.note = "native transfers from transactions only (contract-internal value transfers not included)"
        return out

    def _token_row(self, chain: str, a: TrackedAsset, x: Dict[str, Any]) -> RawTransfer:
        dec = x.get("tokenDecimal")
        dec = int(dec) if dec not in (None, "") else (a.decimals if a.decimals is not None else 18)
        return RawTransfer(chain=chain, asset=a.asset, token=str(a.token), tx_hash=str(x.get("hash")),
                           index=log_index_of(x), ts=float(x.get("timeStamp") or 0),
                           from_address=str(x.get("from", "")).lower(), to_address=str(x.get("to", "")).lower(),
                           amount=int(x.get("value") or 0) / (10 ** dec), block=int(x.get("blockNumber") or 0),
                           symbol=str(x.get("tokenSymbol") or "").upper() or None,
                           decimals=int(x["tokenDecimal"]) if x.get("tokenDecimal") not in (None, "") else None,
                           provider=self.name)

    async def fetch_token(self, chain: str, asset: TrackedAsset, cursor: Dict[str, Any]) -> FetchResult:
        start = cursor.get("block")
        if start is None:
            rows = await self.tokentx(chain, contract=asset.token, startblock=0, offset=self.page_size, sort="desc")
            rows = list(reversed(rows))
        else:
            rows = await self.tokentx(chain, contract=asset.token, startblock=int(start), offset=self.page_size)
        blocks = [int(r.get("blockNumber") or 0) for r in rows]
        return FetchResult([self._token_row(chain, asset, x) for x in rows],
                           {"block": max(blocks) if blocks else (start or 0)}, len(rows) < self.page_size)

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        if asset.native:
            raw = await self.native_balance(chain, address)
            return None if raw is None else raw / 1e18
        if asset.decimals is None:
            return None               # decimals learned from the first transfer; never guessed
        raw = await self.tokenbalance(chain, str(asset.token), address)
        return None if raw is None else raw / (10 ** int(asset.decimals))

    def stats(self) -> Dict[str, Any]:
        s = super().stats()
        s["base_url"] = self.base_url
        return s


# v0.7 name
EtherscanClient = EvmProvider
