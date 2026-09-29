"""Solana through JSON-RPC (public mainnet-beta by default). Keyless by default; a private RPC URL that
contains a key (Helius, QuickNode, ...) can be supplied through the environment variable named in
`intel.providers.solana.rpc_url_env` - it is never stored in the config.

* Native SOL: signatures of the owner address; SPL tokens: signatures of the owner's token accounts
  for the mint (`getTokenAccountsByOwner`, refreshed every 6 h), because an incoming token transfer
  does not always list the owner itself.
* Each new, successful transaction is read with `getTransaction` (jsonParsed) and normalised from
  balance deltas (`pre/postBalances`, `pre/postTokenBalances` grouped by owner): the owner's delta is
  allocated to the counterparties with the opposite delta (`normalize.allocate_deltas`). The fee is
  removed from the fee payer's SOL delta first. This handles program-mediated transfers and swaps
  without decoding instructions.
* Cursor: the newest processed signature per watched account (`until`). At most
  `max_tx_per_poll` transactions are read per poll; more means the address is lagging.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ...env import env_secret
from ..addr import solana_address
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset, stable_index
from .base import ProviderError, ProviderRateLimited, WalletProvider
from .normalize import allocate_deltas

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
LAMPORTS = 1e9


class SolanaProvider(WalletProvider):
    name = "solana_rpc"
    label = "Solana (JSON-RPC)"
    family = "solana"
    default_daily = 20000
    default_per_second = 2.0

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        super().__init__(http, pcfg, budget_store, **kw)
        env_name = str(self.cfg.get("rpc_url_env") or "").strip()
        private = env_secret(env_name, env_name) if env_name else ""
        self.private_rpc = bool(private)
        self.rpc_url = private or str(self.cfg.get("rpc_url") or DEFAULT_RPC)
        self.max_tx = int(self.cfg.get("max_tx_per_poll", 25))
        self.seed = int(self.cfg.get("seed_signatures", 10))
        # newest transaction version the node may return (Solana introduced v1 transactions); raised
        # automatically when the node asks for a higher one
        self.max_tx_version = int(self.cfg.get("max_supported_transaction_version", 1))
        self._id = 0

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return solana_address(address)

    async def rpc(self, method: str, params: List[Any]) -> Any:
        self._id += 1
        data = await self.post(self.rpc_url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            if code in (429, -32429) or "rate" in str(msg).lower() or "too many" in str(msg).lower():
                self.budget.rate_limited(None, f"Solana RPC {msg}")
                raise ProviderRateLimited(f"{self.label}: {msg}")
            self.budget.fail(f"{method}: {msg}")
            raise ProviderError(f"{self.label}: {method}: {msg}")
        return (data or {}).get("result")

    async def token_accounts(self, owner: str, mint: str) -> List[str]:
        res = await self.rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        return [str(v.get("pubkey")) for v in ((res or {}).get("value") or []) if v.get("pubkey")]

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        out = FetchResult(cursor=dict(cursor))
        now = self.clock()
        budget_left = self.max_tx
        for a in assets:
            if a.native:
                watch = [address]
            else:
                key = f"ta:{a.token}"
                if key not in out.cursor or now - float(out.cursor.get(key + ":ts", 0)) > 6 * 3600:
                    out.cursor[key] = await self.token_accounts(address, str(a.token))
                    out.cursor[key + ":ts"] = now
                watch = list(out.cursor.get(key) or [])
            for w in watch:
                ck = f"sig:{w}"
                until = out.cursor.get(ck)
                opts: Dict[str, Any] = {"limit": self.seed if until is None else max(self.max_tx + 1, 20)}
                if until:
                    opts["until"] = until
                sigs = await self.rpc("getSignaturesForAddress", [w, opts]) or []
                if not sigs:
                    continue
                out.cursor[ck] = sigs[0].get("signature")
                new = [s for s in sigs if s.get("err") is None]
                if until is not None and len(sigs) >= opts["limit"]:
                    out.complete = False           # more signatures than one page since the cursor
                take = new[:max(0, budget_left)]
                if len(take) < len(new):
                    out.complete = False
                budget_left -= len(take)
                for s in reversed(take):           # oldest first
                    tx = await self.get_transaction(s["signature"])
                    if tx:
                        out.transfers.extend(self.normalize_tx(s["signature"], tx, address, a))
        return out

    async def get_transaction(self, sig: str) -> Optional[Dict[str, Any]]:
        opts = {"encoding": "jsonParsed", "commitment": "finalized"}
        try:
            return await self.rpc("getTransaction", [sig, dict(opts, maxSupportedTransactionVersion=self.max_tx_version)])
        except ProviderError as exc:
            m = re.search(r'maxSupportedTransactionVersion"?\s*:\s*(\d+)', str(exc))
            if not m or int(m.group(1)) <= self.max_tx_version:
                raise
            self.max_tx_version = int(m.group(1))
            self.budget.consecutive_failures = 0
            return await self.rpc("getTransaction", [sig, dict(opts, maxSupportedTransactionVersion=self.max_tx_version)])

    def normalize_tx(self, sig: str, tx: Dict[str, Any], owner: str, asset: TrackedAsset) -> List[RawTransfer]:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return []
        ts = float(tx.get("blockTime") or 0)
        slot = tx.get("slot")
        if asset.native:
            keys = [k.get("pubkey") if isinstance(k, dict) else k
                    for k in (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])]
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            deltas: Dict[str, float] = {}
            for i, k in enumerate(keys):
                if i < len(pre) and i < len(post) and k:
                    deltas[k] = deltas.get(k, 0.0) + (int(post[i]) - int(pre[i]))
            if keys and keys[0]:
                deltas[keys[0]] = deltas.get(keys[0], 0.0) + int(meta.get("fee") or 0)   # remove the fee
            moves = allocate_deltas(owner, deltas)
            return [RawTransfer(chain="solana", asset=asset.asset, token=NATIVE, tx_hash=sig,
                                index=stable_index(NATIVE, f, t), ts=ts, from_address=f, to_address=t,
                                amount=amt / LAMPORTS, block=slot, symbol="SOL", decimals=9, provider=self.name)
                    for f, t, amt in moves]
        mint = str(asset.token)
        deltas = {}
        dec = asset.decimals
        for sign, rows in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
            for b in rows:
                if b.get("mint") != mint or not b.get("owner"):
                    continue
                ui = b.get("uiTokenAmount") or {}
                dec = int(ui.get("decimals")) if ui.get("decimals") is not None else dec
                deltas[b["owner"]] = deltas.get(b["owner"], 0.0) + sign * int(ui.get("amount") or 0)
        if dec is None:
            return []
        moves = allocate_deltas(owner, deltas)
        return [RawTransfer(chain="solana", asset=asset.asset, token=mint, tx_hash=sig, index=stable_index(mint, f, t),
                            ts=ts, from_address=f, to_address=t, amount=amt / (10 ** dec), block=slot, symbol=None,
                            decimals=dec, provider=self.name) for f, t, amt in moves]

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        if asset.native:
            res = await self.rpc("getBalance", [address])
            v = (res or {}).get("value") if isinstance(res, dict) else res
            return None if v is None else int(v) / LAMPORTS
        res = await self.rpc("getTokenAccountsByOwner", [address, {"mint": str(asset.token)}, {"encoding": "jsonParsed"}])
        total = 0.0
        for v in (res or {}).get("value") or []:
            info = ((((v.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            ui = info.get("tokenAmount") or {}
            total += float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
        return total

    async def probe(self) -> Dict[str, Any]:
        return {"slot": await self.rpc("getSlot", []), "private_rpc": self.private_rpc}

    def stats(self) -> Dict[str, Any]:
        s = super().stats()
        s["rpc"] = "private (from environment)" if self.private_rpc else self.rpc_url
        return s

