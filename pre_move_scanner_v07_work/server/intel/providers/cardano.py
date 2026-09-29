"""Cardano through the Koios REST API (api.koios.rest). A bearer token is optional (environment
variable named in `intel.providers.cardano.api_key_env`); the public tier is rate-limited.

* Labels may be payment addresses (`addr1...`) or stake addresses (`stake1...`, recommended: an
  exchange wallet spans many payment addresses under one stake key). Transactions come from
  `POST /address_txs` or `POST /account_txs`; the first poll reads the newest page
  (`order=block_height.desc`), later polls read after the stored block height.
* `POST /tx_info` gives inputs and outputs with ADA (lovelace) and native-asset lists. Counterparties
  are identified by their stake address when they have one, otherwise by payment address.
* UTXO semantics as for Bitcoin (`normalize.utxo_transfers`): change back to the sender is never
  counted, receipts are attributed to the dominant input.
* Balance: `POST /address_info` / `POST /account_info` (ADA only; native-token balances are not read).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..addr import cardano_address
from ..model import NATIVE, FetchResult, RawTransfer, TrackedAsset
from .base import WalletProvider
from .normalize import utxo_transfers

BASE = "https://api.koios.rest/api/v1"


def _ident(io: Dict[str, Any]) -> Optional[str]:
    """Stake address when the output belongs to a stake key, otherwise the payment address."""
    if io.get("stake_addr"):
        return str(io["stake_addr"])
    pa = io.get("payment_addr") or {}
    return str(pa.get("bech32")) if isinstance(pa, dict) and pa.get("bech32") else None


def _matches(io: Dict[str, Any], owner: str) -> bool:
    if owner.startswith("stake"):
        return io.get("stake_addr") == owner
    pa = io.get("payment_addr") or {}
    return isinstance(pa, dict) and pa.get("bech32") == owner


class CardanoProvider(WalletProvider):
    name = "koios"
    label = "Cardano (Koios)"
    family = "cardano"
    default_daily = 4000
    default_per_second = 0.5

    def __init__(self, http, pcfg=None, budget_store=None, **kw):
        pcfg = dict(pcfg or {})
        pcfg.setdefault("api_key_env", "KOIOS_API_TOKEN")
        super().__init__(http, pcfg, budget_store, **kw)
        self.base = str(self.cfg.get("base_url") or BASE).rstrip("/")
        self.page_size = int(self.cfg.get("page_size", 50))
        self.max_tx = int(self.cfg.get("max_tx_per_poll", 50))

    @property
    def headers(self) -> Dict[str, str]:
        return {"authorization": f"Bearer {self.key}"} if self.key else {}

    def normalize_address(self, chain: str, address: str) -> Optional[str]:
        return cardano_address(address)

    async def _tx_list(self, owner: str, after: Optional[int]) -> List[Dict[str, Any]]:
        stake = owner.startswith("stake")
        path = "/account_txs" if stake else "/address_txs"
        body: Dict[str, Any] = {"_stake_address": owner} if stake else {"_addresses": [owner]}
        if after is None:
            qs = f"?order=block_height.desc&limit={self.page_size}"
        else:
            body["_after_block_height"] = int(after)
            qs = f"?order=block_height.asc&limit={self.max_tx + 1}"
        return list(await self.post(self.base + path + qs, body, self.headers) or [])

    async def fetch_address(self, chain: str, address: str, assets: List[TrackedAsset],
                            cursor: Dict[str, Any]) -> FetchResult:
        after = cursor.get("height")
        rows = await self._tx_list(address, after)
        rows = [r for r in rows if after is None or int(r.get("block_height") or 0) > int(after)]
        complete = len(rows) <= self.max_tx
        rows = sorted(rows, key=lambda r: int(r.get("block_height") or 0))[:self.max_tx] if after is not None else rows
        out = FetchResult(cursor=dict(cursor), complete=complete)
        if not rows:
            return out
        out.cursor["height"] = max(int(r.get("block_height") or 0) for r in rows)
        hashes = [str(r["tx_hash"]) for r in rows if r.get("tx_hash")]
        infos: List[Dict[str, Any]] = []
        for i in range(0, len(hashes), 50):
            infos.extend(await self.post(self.base + "/tx_info", {"_tx_hashes": hashes[i:i + 50], "_inputs": True,
                                                                  "_assets": True}, self.headers) or [])
        for tx in infos:
            out.transfers.extend(self.normalize_tx(tx, address, assets))
        return out

    def normalize_tx(self, tx: Dict[str, Any], owner: str, assets: List[TrackedAsset]) -> List[RawTransfer]:
        res: List[RawTransfer] = []
        ins_raw, outs_raw = tx.get("inputs") or [], tx.get("outputs") or []
        for a in assets:
            def amount(io: Dict[str, Any]) -> float:
                if a.native:
                    return int(io.get("value") or 0) / 1e6
                total = 0.0
                for x in io.get("asset_list") or []:
                    if f"{x.get('policy_id', '')}{x.get('asset_name') or ''}".lower() == str(a.token).lower():
                        dec = x.get("decimals") if x.get("decimals") is not None else (a.decimals or 0)
                        total += int(x.get("quantity") or 0) / (10 ** int(dec))
                return total
            owner_ids = {_ident(io) for io in list(ins_raw) + list(outs_raw) if _matches(io, owner)}
            ins = [(owner if _matches(io, owner) else _ident(io), amount(io)) for io in ins_raw]
            outs = [(int(io.get("tx_index") if io.get("tx_index") is not None else i),
                     owner if _matches(io, owner) else _ident(io), amount(io)) for i, io in enumerate(outs_raw)]
            ins = [(x if x not in owner_ids else owner, v) for x, v in ins]
            outs = [(i, x if x not in owner_ids else owner, v) for i, x, v in outs]
            for idx, frm, to, amt, note in utxo_transfers(lambda x: x == owner, owner, ins, outs):
                res.append(RawTransfer(chain="cardano", asset=a.asset, token=NATIVE if a.native else str(a.token),
                                       tx_hash=str(tx.get("tx_hash")), index=idx, ts=float(tx.get("tx_timestamp") or 0),
                                       from_address=frm, to_address=to, amount=amt,
                                       block=int(tx.get("block_height") or 0) or None,
                                       symbol="ADA" if a.native else None, decimals=6 if a.native else a.decimals,
                                       provider=self.name, note=note))
        return res

    async def fetch_balance(self, chain: str, address: str, asset: TrackedAsset) -> Optional[float]:
        if not asset.native:
            return None
        if address.startswith("stake"):
            d = await self.post(self.base + "/account_info", {"_stake_addresses": [address]}, self.headers) or []
            v = (d[0] if d else {}).get("total_balance")
        else:
            d = await self.post(self.base + "/address_info", {"_addresses": [address]}, self.headers) or []
            v = (d[0] if d else {}).get("balance")
        return None if v is None else int(v) / 1e6

    async def probe(self) -> Dict[str, Any]:
        d = await self.get(self.base + "/tip", None, self.headers) or []
        t = d[0] if isinstance(d, list) and d else {}
        return {"block_no": t.get("block_no"), "epoch_no": t.get("epoch_no"), "keyed": bool(self.key)}
