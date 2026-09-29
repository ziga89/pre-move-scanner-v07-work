"""Hedera through the public mirror-node REST API (mainnet-public.mirrornode.hedera.com). Keyless.

* `GET /api/v1/transactions?account.id=0.0.X` - successful transactions of an account. The first poll
  reads the newest page; later polls read ascending after the stored consensus timestamp, following
  `links.next`.
* One transaction carries a list of HBAR deltas (`transfers`) and HTS token deltas
  (`token_transfers`). Network fee accounts (the submitting node, 0.0.98, 0.0.800, 0.0.801) are
  ignored and the fee is removed from the payer's HBAR delta; the monitored account's delta is then
  allocated to the counterparties (`normalize.allocate_deltas`).
* HBAR has 8 decimals (tinybars); HTS token decimals come from `GET /api/v1/tokens/{id}` (cached).
* Balance: `GET /api/v1/accounts/{id}` (`balance.balance`, `balance.tokens`).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..addr import hedera_id
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset, stable_index
from .base import WalletProvider
from .normalize import allocate_deltas

BASE = "https://mainnet-public.mirrornode.hedera.com"
FEE_ACCOUNTS = {"0.0.98", "0.0.800", "0.0.801"}


class HederaProvider(WalletProvider):
    name = "hedera_mirror"
    label = "Hedera (mirror node)"
    family = "hedera"
    default_daily = 20000
    default_per_second = 2.0

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        super().__init__(http, pcfg, budget_store, **kw)
        self.base = str(self.cfg.get("base_url") or BASE).rstrip("/")
        self.max_pages = int(self.cfg.get("max_pages_per_poll", 3))
        self.page_size = int(self.cfg.get("page_size", 100))
        self.decimals: Dict[str, int] = {}

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return hedera_id(address)

    async def token_decimals(self, token_id: str) -> Optional[int]:
        if token_id not in self.decimals:
            d = await self.get(f"{self.base}/api/v1/tokens/{token_id}") or {}
            if d.get("decimals") is None:
                return None
            self.decimals[token_id] = int(d["decimals"])
        return self.decimals[token_id]

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        native = next((a for a in assets if a.native), None)
        tokens = {str(a.token): a for a in assets if not a.native and a.token}
        since = cursor.get("ts")
        txs: List[Dict[str, Any]] = []
        complete = True
        if since is None:
            d = await self.get(f"{self.base}/api/v1/transactions",
                               {"account.id": address, "limit": self.page_size, "order": "desc"}) or {}
            txs = list(d.get("transactions") or [])
        else:
            url: Optional[str] = f"{self.base}/api/v1/transactions"
            params: Optional[Dict[str, Any]] = {"account.id": address, "limit": self.page_size, "order": "asc",
                                                "timestamp": f"gt:{since}"}
            complete = False
            for _ in range(max(1, self.max_pages)):
                d = await self.get(url, params) or {}
                txs.extend(d.get("transactions") or [])
                nxt = (d.get("links") or {}).get("next")
                if not nxt:
                    complete = True
                    break
                url, params = self.base + nxt, None
        out = FetchResult(cursor=dict(cursor), complete=complete)
        top = since
        for t in txs:
            cts = str(t.get("consensus_timestamp") or "")
            if cts and (top is None or float(cts) > float(top)):
                top = cts
            if t.get("result") != "SUCCESS":
                continue
            ts = float(cts or 0)
            txid = str(t.get("transaction_id") or cts)
            payer = txid.split("-")[0]
            ignore = FEE_ACCOUNTS | ({str(t["node"])} if t.get("node") else set())
            if native is not None:
                deltas: Dict[str, float] = {}
                for x in t.get("transfers") or []:
                    deltas[str(x.get("account"))] = deltas.get(str(x.get("account")), 0.0) + int(x.get("amount") or 0)
                if payer in deltas:
                    deltas[payer] += int(t.get("charged_tx_fee") or 0)
                for f, to, amt in allocate_deltas(address, deltas, ignore):
                    out.transfers.append(RawTransfer(chain="hedera", asset=native.asset, token=NATIVE, tx_hash=txid,
                                                     index=stable_index(NATIVE, f, to), ts=ts, from_address=f,
                                                     to_address=to, amount=amt / 1e8, symbol="HBAR", decimals=8,
                                                     provider=self.name))
            by_token: Dict[str, Dict[str, float]] = {}
            for x in t.get("token_transfers") or []:
                tid = str(x.get("token_id"))
                if tid in tokens:
                    dd = by_token.setdefault(tid, {})
                    dd[str(x.get("account"))] = dd.get(str(x.get("account")), 0.0) + int(x.get("amount") or 0)
            for tid, deltas in by_token.items():
                a = tokens[tid]
                dec = a.decimals if a.decimals is not None else await self.token_decimals(tid)
                if dec is None:
                    continue
                for f, to, amt in allocate_deltas(address, deltas, ignore):
                    out.transfers.append(RawTransfer(chain="hedera", asset=a.asset, token=tid, tx_hash=txid,
                                                     index=stable_index(tid, f, to), ts=ts, from_address=f, to_address=to,
                                                     amount=amt / (10 ** dec), decimals=dec, provider=self.name))
        if top is not None:
            out.cursor["ts"] = top
        return out

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        d = await self.get(f"{self.base}/api/v1/accounts/{address}") or {}
        bal = d.get("balance") or {}
        if asset.native:
            return None if bal.get("balance") is None else int(bal["balance"]) / 1e8
        dec = asset.decimals if asset.decimals is not None else await self.token_decimals(str(asset.token))
        if dec is None:
            return None
        for tk in bal.get("tokens") or []:
            if str(tk.get("token_id")) == str(asset.token):
                return int(tk.get("balance") or 0) / (10 ** dec)
        return None                    # not in the (possibly truncated) list: unknown, not 0

    async def probe(self) -> Dict[str, Any]:
        d = await self.get(f"{self.base}/api/v1/blocks", {"limit": 1, "order": "desc"}) or {}
        b = (d.get("blocks") or [{}])[0]
        return {"block": b.get("number"), "timestamp": (b.get("timestamp") or {}).get("to")}
