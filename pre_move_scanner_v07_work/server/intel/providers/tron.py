"""TRON through the TronGrid v1 REST API. An API key is optional (header `TRON-PRO-API-KEY`, from the
environment variable named in `intel.providers.tron.api_key_env`); without one TronGrid is more
strictly rate-limited.

* TRC-20: `GET /v1/accounts/{a}/transactions/trc20` (only confirmed, `Transfer` events), amounts with
  the token's decimals from `token_info`.
* Native TRX: `GET /v1/accounts/{a}/transactions` - successful `TransferContract` transactions; the hex
  (41...) addresses are converted to base58check. Contract-internal TRX moves are not included.
* The first poll reads the newest page; later polls read ascending from the stored block timestamp,
  following `meta.fingerprint`.
* Balance: `GET /v1/accounts/{a}` (`balance` in sun, `trc20` list for tokens).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..addr import tron_address, tron_hex_to_base58
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset, stable_index
from .base import WalletProvider

BASE = "https://api.trongrid.io"


class TronProvider(WalletProvider):
    name = "trongrid"
    label = "TRON (TronGrid)"
    family = "tron"
    default_daily = 10000
    default_per_second = 1.0

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        pcfg = dict(pcfg or {})
        pcfg.setdefault("api_key_env", "TRONGRID_API_KEY")
        super().__init__(http, pcfg, budget_store, **kw)
        self.base = str(self.cfg.get("base_url") or BASE).rstrip("/")
        self.max_pages = int(self.cfg.get("max_pages_per_poll", 3))
        self.page_size = int(self.cfg.get("page_size", 200))

    @property
    def headers(self) -> Dict[str, str]:
        return {"TRON-PRO-API-KEY": self.key} if self.key else {}

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return tron_address(address)

    async def _paged(self, path: str, params: Dict[str, Any], since_ms: Optional[int]):
        rows: List[Dict[str, Any]] = []
        if since_ms is None:
            data = await self.get(self.base + path, dict(params, limit=self.page_size, order_by="block_timestamp,desc"),
                                  self.headers)
            return list((data or {}).get("data") or []), True
        fp = None
        for _ in range(max(1, self.max_pages)):
            p = dict(params, limit=self.page_size, order_by="block_timestamp,asc", min_timestamp=int(since_ms))
            if fp:
                p["fingerprint"] = fp
            data = await self.get(self.base + path, p, self.headers) or {}
            page = list(data.get("data") or [])
            rows.extend(page)
            fp = (data.get("meta") or {}).get("fingerprint")
            if not fp or len(page) < self.page_size:
                return rows, True
        return rows, False

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        out = FetchResult(cursor=dict(cursor))
        tokens = {str(a.token): a for a in assets if not a.native and a.token}
        native = next((a for a in assets if a.native), None)
        if tokens:
            params: Dict[str, Any] = {"only_confirmed": "true"}
            if len(tokens) == 1:
                params["contract_address"] = next(iter(tokens))
            rows, done = await self._paged(f"/v1/accounts/{address}/transactions/trc20", params, cursor.get("trc20_ts"))
            out.complete = out.complete and done
            top = cursor.get("trc20_ts")
            for r in rows:
                ti = r.get("token_info") or {}
                a = tokens.get(str(ti.get("address") or ""))
                ts = int(r.get("block_timestamp") or 0)
                top = max(ts, int(top or 0))
                if a is None or str(r.get("type") or "Transfer") != "Transfer":
                    continue
                dec = int(ti.get("decimals") if ti.get("decimals") not in (None, "") else (a.decimals or 0))
                amt = int(r.get("value") or 0) / (10 ** dec)
                if amt <= 0:
                    continue
                out.transfers.append(RawTransfer(
                    chain="tron", asset=a.asset, token=str(a.token), tx_hash=str(r.get("transaction_id")),
                    index=stable_index(r.get("from"), r.get("to"), r.get("value")), ts=ts / 1000.0,
                    from_address=str(r.get("from") or ""), to_address=str(r.get("to") or ""), amount=amt, symbol=str(ti.get("symbol") or "").upper() or None, decimals=dec, provider=self.name))
            if top is not None:
                out.cursor["trc20_ts"] = top
        if native is not None:
            rows, done = await self._paged(f"/v1/accounts/{address}/transactions", {"only_confirmed": "true"},
                                           cursor.get("trx_ts"))
            out.complete = out.complete and done
            top = cursor.get("trx_ts")
            for r in rows:
                ts = int(r.get("block_timestamp") or 0)
                top = max(ts, int(top or 0))
                if "internal_tx_id" in r:
                    continue                   # contract-internal moves: not included
                ret = (r.get("ret") or [{}])[0].get("contractRet")
                if ret not in (None, "SUCCESS"):
                    continue
                for c in ((r.get("raw_data") or {}).get("contract") or [])[:1]:
                    if c.get("type") != "TransferContract":
                        continue
                    v = (c.get("parameter") or {}).get("value") or {}
                    frm, to = tron_hex_to_base58(v.get("owner_address")), tron_hex_to_base58(v.get("to_address"))
                    amt = int(v.get("amount") or 0) / 1e6
                    if not frm or not to or amt <= 0:
                        continue
                    out.transfers.append(RawTransfer(
                        chain="tron", asset=native.asset, token=NATIVE, tx_hash=str(r.get("txID")), index=-1,
                        ts=ts / 1000.0, from_address=frm, to_address=to, amount=amt,
                        block=int(r.get("blockNumber") or 0) or None, symbol="TRX", decimals=6, provider=self.name))
            if top is not None:
                out.cursor["trx_ts"] = top
            out.note = "TRX from TransferContract transactions (contract-internal moves not included)"
        return out

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        data = await self.get(f"{self.base}/v1/accounts/{address}", None, self.headers) or {}
        acct = (data.get("data") or [None])[0]
        if acct is None:
            return None
        if asset.native:
            return int(acct.get("balance") or 0) / 1e6
        if asset.decimals is None:
            return None
        for entry in acct.get("trc20") or []:
            if str(asset.token) in entry:
                return int(entry[str(asset.token)]) / (10 ** int(asset.decimals))
        return 0.0

    async def probe(self) -> Dict[str, Any]:
        data = await self.get(f"{self.base}/wallet/getnowblock", None, self.headers) or {}
        return {"block": ((data.get("block_header") or {}).get("raw_data") or {}).get("number"), "keyed": bool(self.key)}
