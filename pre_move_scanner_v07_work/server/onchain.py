from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Dict, Any

import httpx


class EtherscanWatcher:
    """
    Optional on-chain watcher for KNOWN wallets.
    It does not infer ownership or exchange labels. It simply reports token
    transfers involving the configured wallet addresses.

    Requires ETHERSCAN_API_KEY (or the env variable configured in config.json).
    """
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))
        env_name = self.cfg.get("etherscan_api_key_env", "ETHERSCAN_API_KEY")
        self.api_key = os.getenv(env_name, "")
        self.poll_seconds = int(self.cfg.get("poll_seconds", 15))
        self.events = deque(maxlen=500)
        self.seen = set()
        self.last_error = ""
        self._stop = asyncio.Event()

    async def close(self):
        self._stop.set()

    async def run(self):
        if not self.enabled:
            return
        if not self.api_key:
            self.last_error = "On-chain watcher enabled but Etherscan API key is missing."
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            while not self._stop.is_set():
                try:
                    for asset, spec in self.cfg.get("assets", {}).items():
                        contract = spec.get("contract")
                        for label, wallet in spec.get("watch_wallets", {}).items():
                            params = {
                                "chainid": 1,
                                "module": "account",
                                "action": "tokentx",
                                "address": wallet,
                                "contractaddress": contract,
                                "page": 1,
                                "offset": 40,
                                "sort": "desc",
                                "apikey": self.api_key,
                            }
                            r = await client.get("https://api.etherscan.io/v2/api", params=params)
                            data = r.json()
                            rows = data.get("result", [])
                            if not isinstance(rows, list):
                                continue
                            for x in rows:
                                key = f'{x.get("hash")}:{x.get("logIndex")}'
                                if key in self.seen:
                                    continue
                                self.seen.add(key)
                                decimals = int(x.get("tokenDecimal", "0") or 0)
                                amount = int(x.get("value", "0") or 0) / (10 ** decimals if decimals else 1)
                                frm = x.get("from", "").lower()
                                to = x.get("to", "").lower()
                                wl = wallet.lower()
                                direction = "IN" if to == wl else "OUT"
                                self.events.appendleft({
                                    "ts": int(x.get("timeStamp", "0") or 0),
                                    "asset": asset,
                                    "wallet_label": label,
                                    "wallet": wallet,
                                    "direction": direction,
                                    "amount": amount,
                                    "counterparty": x.get("from") if direction == "IN" else x.get("to"),
                                    "tx": x.get("hash"),
                                })
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = repr(exc)
                await asyncio.sleep(self.poll_seconds)

    def snapshot(self):
        return {
            "enabled": self.enabled,
            "active": self.enabled and bool(self.api_key),
            "last_error": self.last_error,
            "events": list(self.events)[:100],
        }
