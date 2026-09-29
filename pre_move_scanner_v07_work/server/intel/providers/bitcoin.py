"""Bitcoin through an Esplora REST API (mempool.space by default, blockstream.info as fallback). Keyless.

* `GET /address/{a}/txs` returns the newest transactions (mempool first, then 25 confirmed);
  `/address/{a}/txs/chain/{last_txid}` pages further back. Only confirmed transactions are used.
* UTXO semantics (see `normalize.utxo_transfers`): spending by the monitored address produces one
  transfer per non-change output; receiving produces transfers from the dominant input address.
* The cursor is the newest confirmed height plus the txids at that height. Paging stops at the
  cursor; if `max_pages_per_poll` is reached first the address is lagging (partial coverage).
* Balance: `GET /address/{a}` chain stats (funded - spent).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..addr import bitcoin_address
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset
from .base import ProviderError, WalletProvider
from .normalize import utxo_transfers

SATS = 1e8
DEFAULT_URLS = ["https://mempool.space/api", "https://blockstream.info/api"]


class BitcoinProvider(WalletProvider):
    name = "esplora"
    label = "Bitcoin (Esplora)"
    family = "bitcoin"
    default_daily = 20000
    default_per_second = 1.0

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        super().__init__(http, pcfg, budget_store, **kw)
        self.urls = list(self.cfg.get("base_urls") or DEFAULT_URLS)
        self._url = 0
        self.max_pages = int(self.cfg.get("max_pages_per_poll", 4))

    @property
    def base(self) -> str:
        return self.urls[self._url % len(self.urls)].rstrip("/")

    async def _get(self, path: str) -> Any:
        try:
            return await self.get(self.base + path)
        except ProviderError:
            self._url += 1                    # next call tries the fallback instance
            raise

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return bitcoin_address(address)

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        btc = next((a for a in assets if a.native), None)
        if btc is None:
            return FetchResult(cursor=dict(cursor))
        c_height = cursor.get("height")
        c_txids = set(cursor.get("txids") or [])
        txs: List[Dict[str, Any]] = []
        reached = False
        path = f"/address/{address}/txs"
        for _ in range(max(1, self.max_pages)):
            page = await self._get(path)
            if not isinstance(page, list):
                raise ProviderError(f"{self.label}: unexpected answer for {address}")
            confirmed = [t for t in page if (t.get("status") or {}).get("confirmed")]
            for t in confirmed:
                h = int(t["status"].get("block_height") or 0)
                if c_height is not None and (h < c_height or (h == c_height and t.get("txid") in c_txids)):
                    reached = True            # everything older was processed by an earlier poll
                    break
                txs.append(t)
            if reached or c_height is None or len(confirmed) < 25:
                reached = True                # seeded from the newest page, or no older history
                break
            path = f"/address/{address}/txs/chain/{confirmed[-1]['txid']}"
        new_cursor = dict(cursor)
        if txs:
            top = max(int(t["status"]["block_height"]) for t in txs)
            new_cursor = {"height": top, "txids": [t["txid"] for t in txs if int(t["status"]["block_height"]) == top]}
        out = FetchResult(cursor=new_cursor, complete=reached)
        for t in txs:
            out.transfers.extend(self._normalize(btc, address, t))
        return out

    def _normalize(self, asset: TrackedAsset, owner: str, t: Dict[str, Any]) -> List[RawTransfer]:
        st = t.get("status") or {}
        ins = [((v.get("prevout") or {}).get("scriptpubkey_address"), float((v.get("prevout") or {}).get("value") or 0) / SATS)
               for v in t.get("vin") or [] if not v.get("is_coinbase")]
        outs = [(i, o.get("scriptpubkey_address"), float(o.get("value") or 0) / SATS) for i, o in enumerate(t.get("vout") or [])]
        res = []
        for idx, frm, to, amt, note in utxo_transfers(lambda a: a == owner, owner, ins, outs):
            res.append(RawTransfer(chain="bitcoin", asset=asset.asset, token=NATIVE, tx_hash=str(t.get("txid")),
                                   index=idx, ts=float(st.get("block_time") or 0), from_address=frm, to_address=to,
                                   amount=amt, block=int(st.get("block_height") or 0), symbol="BTC", decimals=8,
                                   provider=self.name, note=note))
        return res

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        d = await self._get(f"/address/{address}")
        cs = (d or {}).get("chain_stats") or {}
        if "funded_txo_sum" not in cs:
            return None
        return (int(cs["funded_txo_sum"]) - int(cs.get("spent_txo_sum") or 0)) / SATS

    async def probe(self) -> Dict[str, Any]:
        h = await self._get("/blocks/tip/height")
        return {"tip_height": h, "base": self.base}
