"""XRP Ledger through public rippled JSON-RPC servers (xrplcluster.com, s1/s2.ripple.com). Keyless.

* `account_tx` pages an account's validated transactions. The first poll reads the newest page
  (`forward=false`); later polls read forward from the stored ledger index, following `marker`.
* Only successful `Payment` transactions count, and the amount is `meta.delivered_amount` (the
  amount actually delivered - a partial payment's `Amount` field can be far larger). Transactions
  where it is unavailable are skipped, never guessed.
* Native XRP amounts are drops (1 XRP = 1,000,000 drops); issued tokens are tracked as
  `CURRENCY.issuer` and matched exactly.
* Balance: `account_info` (XRP) / `account_lines` (issued tokens).
Both API versions are read: `tx` (v1) and `tx_json` + top-level `hash` (v2).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..addr import xrpl_address
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset
from .base import ProviderError, ProviderRateLimited, WalletProvider

RIPPLE_EPOCH = 946684800
DEFAULT_URLS = ["https://xrplcluster.com/", "https://s1.ripple.com:51234/", "https://s2.ripple.com:51234/"]


def _currency(c: str) -> str:
    return c.upper() if len(c) == 40 else c


class XrplProvider(WalletProvider):
    name = "xrpl"
    label = "XRPL (rippled JSON-RPC)"
    family = "xrpl"
    default_daily = 20000
    default_per_second = 2.0

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        super().__init__(http, pcfg, budget_store, **kw)
        self.urls = list(self.cfg.get("rpc_urls") or DEFAULT_URLS)
        self._url = 0
        self.max_pages = int(self.cfg.get("max_pages_per_poll", 3))
        self.page_size = int(self.cfg.get("page_size", 200))

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return xrpl_address(address)

    async def rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.urls[self._url % len(self.urls)]
        try:
            data = await self.post(url, {"method": method, "params": [params]})
        except ProviderError:
            self._url += 1
            raise
        res = (data or {}).get("result") or {}
        if res.get("status") == "error" or res.get("error"):
            err = str(res.get("error") or "error")
            if err in ("slowDown", "tooBusy"):
                self.budget.rate_limited(None, f"XRPL {err}")
                raise ProviderRateLimited(f"{self.label}: {err}")
            if err == "actNotFound":
                return {"not_found": True}
            self.budget.fail(f"{method}: {err} {res.get('error_message', '')}")
            raise ProviderError(f"{self.label}: {method}: {err}")
        return res

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        native = next((a for a in assets if a.native), None)
        tokens = {a.token: a for a in assets if not a.native and a.token}
        start = cursor.get("ledger")
        entries: List[Dict[str, Any]] = []
        complete = True
        if start is None:
            res = await self.rpc("account_tx", {"account": address, "ledger_index_min": -1, "ledger_index_max": -1,
                                                "limit": self.page_size, "forward": False})
            entries = list(res.get("transactions") or [])
        else:
            marker = None
            complete = False
            for _ in range(max(1, self.max_pages)):
                p = {"account": address, "ledger_index_min": int(start), "ledger_index_max": -1,
                     "limit": self.page_size, "forward": True}
                if marker is not None:
                    p["marker"] = marker
                res = await self.rpc("account_tx", p)
                entries.extend(res.get("transactions") or [])
                marker = res.get("marker")
                if not marker:
                    complete = True
                    break
        out = FetchResult(cursor=dict(cursor), complete=complete)
        top = start
        for e in entries:
            tx = e.get("tx") or e.get("tx_json") or {}
            meta = e.get("meta") or {}
            led = tx.get("ledger_index") or e.get("ledger_index")
            if led is not None:
                top = max(int(led), int(top or 0))
            if not e.get("validated", True) or meta.get("TransactionResult") != "tesSUCCESS":
                continue
            if tx.get("TransactionType") != "Payment":
                continue
            frm, to = str(tx.get("Account") or ""), str(tx.get("Destination") or "")
            if address not in (frm, to):
                continue
            delivered = meta.get("delivered_amount", meta.get("DeliveredAmount"))
            if delivered is None or delivered == "unavailable":
                continue
            date = tx.get("date")
            ts = float(date) + RIPPLE_EPOCH if date is not None else 0.0
            h = str(tx.get("hash") or e.get("hash") or "")
            if isinstance(delivered, str):
                if native is None:
                    continue
                amt, asset, token, sym, dec = int(delivered) / 1e6, native, NATIVE, "XRP", 6
            elif isinstance(delivered, dict):
                key = f"{_currency(str(delivered.get('currency', '')))}.{delivered.get('issuer', '')}"
                asset = next((a for t, a in tokens.items() if t and
                              f"{_currency(t.split('.')[0])}.{t.split('.', 1)[1]}" == key), None)
                if asset is None:
                    continue
                amt, token, sym, dec = float(delivered.get("value") or 0), asset.token, asset.asset, None
            else:
                continue
            if amt <= 0:
                continue
            out.transfers.append(RawTransfer(chain="xrpl", asset=asset.asset, token=token, tx_hash=h, index=0, ts=ts,
                                             from_address=frm, to_address=to, amount=amt,
                                             block=int(led) if led is not None else None, symbol=sym, decimals=dec,
                                             provider=self.name,
                                             note=f"destination tag {tx['DestinationTag']}" if "DestinationTag" in tx else ""))
        if top is not None:
            out.cursor["ledger"] = int(top)
        return out

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        if asset.native:
            res = await self.rpc("account_info", {"account": address, "ledger_index": "validated"})
            bal = ((res or {}).get("account_data") or {}).get("Balance")
            return None if bal is None else int(bal) / 1e6
        cur, issuer = str(asset.token).split(".", 1)
        res = await self.rpc("account_lines", {"account": address, "peer": issuer, "ledger_index": "validated"})
        for ln in res.get("lines") or []:
            if _currency(str(ln.get("currency", ""))) == _currency(cur):
                return float(ln.get("balance") or 0)
        return 0.0 if not res.get("not_found") else None

    async def probe(self) -> Dict[str, Any]:
        res = await self.rpc("ledger", {"ledger_index": "validated"})
        return {"validated_ledger": (res.get("ledger") or {}).get("ledger_index") or res.get("ledger_index"),
                "url": self.urls[self._url % len(self.urls)]}
