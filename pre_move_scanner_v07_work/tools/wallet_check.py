"""Live wallet-intelligence check (v0.8): CoinGecko metadata discovery and every wallet provider
against the real APIs. Writes data/wallet_check_report.md and .json.

    python tools/wallet_check.py                 # everything
    python tools/wallet_check.py --only bitcoin,xrpl
    run_windows.bat walletcheck

What it proves per provider: the API answers, a sample address taken from the chain's *latest* data
(never a hard-coded wallet identity) is fetched and normalised into transfers, a second poll with the
returned cursor works, and a balance is read. Etherscan-based checks (EVM tokens, native ETH, XDC) run
only when ETHERSCAN_API_KEY is set. The discovered ERC-20 contracts are also checked on-chain through
a public Ethereum JSON-RPC node (`symbol()` must match).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import __version__  # noqa: E402
from server.config import load_config  # noqa: E402
from server.intel.addr import tron_hex_to_base58  # noqa: E402
from server.intel.model import TrackedAsset  # noqa: E402
from server.intel.providers import ProviderRateLimited, build_providers  # noqa: E402
from server.intel.registry import READY, decide  # noqa: E402
from server.universe.coingecko import CoinGeckoClient  # noqa: E402
from server.universe.http import HttpClient  # noqa: E402

RESULTS: List[Dict[str, Any]] = []
DISCOVERY_IDS = [  # (symbol, CoinGecko id, expected decision)
    ("QNT", "quant-network", "ethereum token"), ("LINK", "chainlink", "ethereum token"),
    ("UNI", "uniswap", "ethereum token"), ("AAVE", "aave", "ethereum token"),
    ("XDC", "xdce-crowd-sale", "native xdc"), ("BTC", "bitcoin", "native bitcoin"),
    ("ETH", "ethereum", "native ethereum"), ("BNB", "binancecoin", "native bsc"),
    ("XRP", "ripple", "native xrpl"), ("TRX", "tron", "native tron"), ("SOL", "solana", "native solana"),
    ("HBAR", "hedera-hashgraph", "native hedera"), ("ADA", "cardano", "native cardano"),
    ("JUP", "jupiter-exchange-solana", "solana token"), ("SUI", "sui", "unsupported"),
]
PUBLIC_ETH_RPC = ["https://ethereum-rpc.publicnode.com", "https://cloudflare-eth.com", "https://eth.llamarpc.com"]
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # token contract used only as a TRC-20 sample (no wallet label)
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def rec(step: str, status: str, detail: str, data: Any = None) -> None:
    RESULTS.append({"step": step, "status": status, "detail": detail, "data": data})
    text = f"[{status}] {step}: {detail}".replace("\u2192", "->").replace("\u2026", "...").replace("\u00b7", "-")
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)   # Windows consoles: plain ASCII


def summarize_transfers(res) -> str:
    t = res.transfers
    if not t:
        return f"0 transfers (complete={res.complete})"
    x = t[0]
    return (f"{len(t)} transfers, complete={res.complete}, e.g. {x.amount:,.6f} {x.asset} "
            f"{x.from_address[:12]}… → {x.to_address[:12]}… tx {x.tx_hash[:16]}…")


async def patient(p, fn, *args):
    """One retry after a rate-limit back-off (public endpoints answer 'too busy' now and then)."""
    try:
        return await fn(*args)
    except ProviderRateLimited:
        wait = max(1.0, float(p.budget.backoff_until - time.time()) + 1.0)
        rec(f"{p.name} rate limit", "WARN", f"rate-limited by the public endpoint; retrying once after {wait:.0f}s")
        await asyncio.sleep(min(wait, 120.0))
        return await fn(*args)


async def poll_twice(p, chain: str, address: str, assets: List[TrackedAsset], step: str) -> None:
    r1 = await patient(p, p.fetch_address, chain, address, assets, {})
    status = "PASS" if r1.transfers else "WARN"
    rec(step + " · first poll", status, f"{address}: {summarize_transfers(r1)}; cursor {json.dumps(r1.cursor)[:120]}")
    r2 = await patient(p, p.fetch_address, chain, address, assets, r1.cursor)
    dup = {(x.tx_hash, x.index) for x in r1.transfers} & {(x.tx_hash, x.index) for x in r2.transfers}
    rec(step + " · second poll (cursor)", "PASS",
        f"{len(r2.transfers)} new transfers ({len(dup)} already seen - de-duplicated by the monitor), complete={r2.complete}")
    bal = await patient(p, p.fetch_balance, chain, address, assets[0])
    rec(step + " · balance", "PASS" if bal is not None else "WARN", f"{bal} {assets[0].asset}")


def asset(sym: str, chain: str, native: bool = True, token: Optional[str] = None, dec: Optional[int] = None):
    return TrackedAsset(asset=sym, chain=chain, native=native, token=token, decimals=dec)


# ------------------------------------------------------------------ discovery
async def check_discovery(cg, providers) -> Dict[str, Dict[str, Any]]:
    out = {}
    for sym, cid, expect in DISCOVERY_IDS:
        try:
            d = await cg.coin_detail(cid)
            r = decide(sym, cid, d, providers, time.time())
            desc = f"{r['state']} · chain {r['native_chain']} · native {bool(r['native_asset'])} · contract {r['contract_address']} · provider {r['wallet_provider']} · {r['reason']}"
            kind, where = expect.split()[0], expect.split()[-1]
            if expect == "unsupported":
                ok = r["state"] == "UNSUPPORTED"
            elif kind == "native":
                ok = r["state"] == READY and bool(r["native_asset"]) and r["native_chain"] == where
            else:                                    # "<chain> token"
                ok = r["state"] == READY and not r["native_asset"] and r["native_chain"] == kind \
                    and bool(r["contract_address"])
            rec(f"discovery {sym} ({cid})", "PASS" if ok else "FAIL", desc + ("" if ok else f" (expected {expect})"))
            out[sym] = r
        except Exception as exc:
            rec(f"discovery {sym} ({cid})", "FAIL", f"{type(exc).__name__}: {exc}")
    return out


async def check_erc20_onchain(http, found: Dict[str, Dict[str, Any]]) -> None:
    tokens = [(s, r["contract_address"]) for s, r in found.items() if r.get("native_chain") == "ethereum"
              and not r.get("native_asset") and r.get("contract_address")]
    for sym, contract in tokens:
        for url in PUBLIC_ETH_RPC:
            try:
                d = await http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                               "params": [{"to": contract, "data": "0x95d89b41"}, "latest"]})
                raw = bytes.fromhex(str(d.get("result") or "0x")[2:])
                if len(raw) >= 64:
                    n = int.from_bytes(raw[32:64], "big")
                    onchain = raw[64:64 + n].decode("utf-8", "replace")
                else:
                    onchain = raw.rstrip(b"\x00").decode("utf-8", "replace")
                ok = onchain.upper() == sym
                rec(f"ERC-20 on-chain symbol {sym}", "PASS" if ok else "FAIL",
                    f"{contract}: symbol() = {onchain!r} via {url}")
                break
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
        else:
            rec(f"ERC-20 on-chain symbol {sym}", "WARN", f"no public RPC answered ({last})")


# ------------------------------------------------------------------ providers
async def check_bitcoin(p) -> None:
    rec("bitcoin probe", "PASS", json.dumps(await p.probe()))
    blocks = await p.get(p.base + "/blocks")
    txs = await p.get(f"{p.base}/block/{blocks[0]['id']}/txs")
    addr = next(o["scriptpubkey_address"] for t in txs[1:] for o in t.get("vout", []) if o.get("scriptpubkey_address"))
    await poll_twice(p, "bitcoin", addr, [asset("BTC", "bitcoin")], "bitcoin")


async def check_xrpl(p) -> None:
    rec("xrpl probe", "PASS", json.dumps(await p.probe()))
    res = await p.rpc("ledger", {"ledger_index": "validated", "transactions": True, "expand": True})
    txs = (res.get("ledger") or {}).get("transactions") or []
    acct = None
    for t in txs:
        tx = t.get("tx_json") or t
        meta = t.get("metaData") or t.get("meta") or {}
        if tx.get("TransactionType") == "Payment" and isinstance(meta.get("delivered_amount"), str):
            acct = tx.get("Destination")
            break
    if not acct:
        rec("xrpl sample", "WARN", "no XRP payment in the latest validated ledger")
        return
    await poll_twice(p, "xrpl", acct, [asset("XRP", "xrpl")], "xrpl")


async def check_tron(p) -> None:
    rec("tron probe", "PASS", json.dumps(await p.probe()))
    blk = await p.get(f"{p.base}/wallet/getnowblock", None, p.headers) or {}
    addr = None
    for t in blk.get("transactions") or []:
        c = ((t.get("raw_data") or {}).get("contract") or [{}])[0]
        if c.get("type") == "TransferContract":
            addr = tron_hex_to_base58(((c.get("parameter") or {}).get("value") or {}).get("to_address"))
            if addr:
                break
    if addr:
        await poll_twice(p, "tron", addr, [asset("TRX", "tron")], "tron TRX")
    else:
        rec("tron TRX sample", "WARN", "no TransferContract in the latest block")
    ev = await p.get(f"{p.base}/v1/contracts/{USDT_TRC20}/events", {"event_name": "Transfer", "limit": 5,
                                                                     "only_confirmed": "true"}, p.headers) or {}
    holder = None
    for e in ev.get("data") or []:
        frm = (e.get("result") or {}).get("from")
        holder = tron_hex_to_base58(frm) if frm else None
        if holder:
            break
    if holder:
        await poll_twice(p, "tron", holder, [asset("USDT", "tron", False, USDT_TRC20, 6)], "tron TRC-20")
    else:
        rec("tron TRC-20 sample", "WARN", "no Transfer event returned")


async def check_solana(p) -> None:
    rec("solana probe", "PASS", json.dumps(await p.probe()))
    sigs = await p.rpc("getSignaturesForAddress", [SPL_TOKEN_PROGRAM, {"limit": 10}]) or []
    for s in sigs:
        if s.get("err") is not None:
            continue
        tx = await p.get_transaction(s["signature"])
        if not tx:
            continue
        meta = tx.get("meta") or {}
        post = {(b.get("owner"), b.get("mint")): int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                for b in meta.get("postTokenBalances") or [] if b.get("owner")}
        pre = {(b.get("owner"), b.get("mint")): int((b.get("uiTokenAmount") or {}).get("amount") or 0)
               for b in meta.get("preTokenBalances") or [] if b.get("owner")}
        changed = [k for k in set(post) | set(pre) if post.get(k, 0) != pre.get(k, 0)]
        if not changed:
            continue
        owner, mint = changed[0]
        dec = next((int((b.get("uiTokenAmount") or {}).get("decimals") or 0) for b in meta.get("postTokenBalances") or []
                    if b.get("mint") == mint), None)
        rec("solana sample", "PASS", f"owner {owner} mint {mint} from tx {s['signature'][:20]}…")
        await poll_twice(p, "solana", owner, [asset("SPL", "solana", False, mint, dec)], "solana SPL token")
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        payer = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if payer:
            await poll_twice(p, "solana", payer, [asset("SOL", "solana")], "solana SOL")
        return
    rec("solana sample", "WARN", "no token balance change among the latest token-program transactions")


async def check_hedera(p) -> None:
    rec("hedera probe", "PASS", json.dumps(await p.probe()))
    d = await p.get(f"{p.base}/api/v1/transactions", {"limit": 25, "order": "desc", "transactiontype": "cryptotransfer",
                                                        "result": "success"}) or {}
    acct = None
    for t in d.get("transactions") or []:
        for x in t.get("transfers") or []:
            num = int(str(x.get("account") or "0.0.0").split(".")[-1])
            if num > 1000 and int(x.get("amount") or 0) > 0:          # skip system / node / fee accounts
                acct = str(x["account"])
                break
        if acct:
            break
    if not acct:
        rec("hedera sample", "WARN", "no HBAR transfer in the latest transactions")
        return
    await poll_twice(p, "hedera", acct, [asset("HBAR", "hedera")], "hedera")


async def check_cardano(p) -> None:
    rec("cardano probe", "PASS", json.dumps(await p.probe()))
    blocks = await p.get(p.base + "/blocks", {"limit": 3}, p.headers) or []
    hashes: List[str] = []
    for b in blocks:
        bt = await p.post(p.base + "/block_txs", {"_block_hashes": [b["hash"]]}, p.headers) or []
        for x in bt:
            if isinstance(x.get("tx_hashes"), list):
                hashes.extend(x["tx_hashes"])
            elif x.get("tx_hash"):
                hashes.append(x["tx_hash"])
        if hashes:
            break
    if not hashes:
        rec("cardano sample", "WARN", "no transactions in the latest blocks")
        return
    info = await p.post(p.base + "/tx_info", {"_tx_hashes": hashes[:1], "_inputs": True, "_assets": True}, p.headers) or []
    outs = (info[0] if info else {}).get("outputs") or []
    addr = next(((o.get("stake_addr") or (o.get("payment_addr") or {}).get("bech32")) for o in outs
                 if o.get("stake_addr") or (o.get("payment_addr") or {}).get("bech32")), None)
    if not addr:
        rec("cardano sample", "WARN", "no output address in the sample transaction")
        return
    await poll_twice(p, "cardano", addr, [asset("ADA", "cardano")], "cardano")


async def check_evm(p, found: Dict[str, Dict[str, Any]]) -> None:
    if not p.keyed:
        rec("etherscan (EVM)", "WARN", f"skipped: {p.key_env} not set - EVM tokens / ETH / XDC not live-checked")
        return
    tokens = [(s, r) for s, r in found.items() if r.get("native_chain") == "ethereum" and not r.get("native_asset")
              and r.get("contract_address")]
    for sym, r in tokens[:4]:
        rows = await p.tokentx("ethereum", contract=r["contract_address"], offset=5, sort="desc")
        if not rows:
            rec(f"etherscan {sym} sample", "WARN", "no transfers returned")
            continue
        holder = rows[0]["from"]
        ta = asset(sym, "ethereum", False, r["contract_address"], r.get("decimals"))
        eth = asset("ETH", "ethereum")
        await poll_twice(p, "ethereum", holder, [ta, eth], f"etherscan ERC-20 {sym} + native ETH")
    for chain, sym in (("xdc", "XDC"), ("bsc", "BNB"), ("base", "ETH")):
        try:
            blk = await p.call(chain, {"module": "proxy", "action": "eth_getBlockByNumber", "tag": "latest",
                                       "boolean": "true"})
            txs = (blk or {}).get("transactions") or []
            addr = next((t.get("from") for t in txs if int(t.get("value") or "0x0", 16) > 0), None)
            if not addr:
                rec(f"etherscan {chain}", "WARN", "no value transfer in the latest block")
                continue
            await poll_twice(p, chain, addr, [asset(sym, chain)], f"etherscan native {sym} on {chain}")
        except Exception as exc:
            rec(f"etherscan {chain}", "WARN", f"{type(exc).__name__}: {exc}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Live wallet-provider check")
    ap.add_argument("--only", default="", help="comma list: discovery,bitcoin,xrpl,tron,solana,hedera,cardano,evm")
    ap.add_argument("--out", default="data/wallet_check_report")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    cfg = load_config(ROOT, create_if_missing=False)
    http = HttpClient()
    providers = build_providers(dict(cfg["intel"], enabled=True), http, {})
    cg = CoinGeckoClient(http, cfg["universe"])
    rec("version", "PASS", f"Pre-Move Scanner {__version__}; CoinGecko key: {'yes' if cg.headers else 'no'}")
    found: Dict[str, Dict[str, Any]] = {}
    checks = [("discovery", None), ("bitcoin", check_bitcoin), ("xrpl", check_xrpl), ("tron", check_tron),
              ("solana", check_solana), ("hedera", check_hedera), ("cardano", check_cardano), ("evm", None)]
    by_family = {p.family: p for p in providers.providers}
    for name, fn in checks:
        if only and name not in only:
            continue
        try:
            if name == "discovery":
                found = await check_discovery(cg, providers)
                await check_erc20_onchain(http, found)
            elif name == "evm":
                await check_evm(by_family["evm"], found)
            else:
                await fn(by_family[name])
        except Exception as exc:
            rec(f"{name}", "FAIL", f"{type(exc).__name__}: {exc}", traceback.format_exc()[-1500:])
    for p in providers.providers:
        s = p.stats()
        rec(f"provider stats {p.name}", "PASS" if not s["errors"] else "WARN",
            f"calls {s['calls']}, errors {s['errors']}, rate-limit hits {s['rate_limit_hits']}, last error {s['last_error'][:160]}")
    await http.close()
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
    lines = [f"# Wallet check - Pre-Move Scanner {__version__}", "", f"Run: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
             "", "| Status | Step | Detail |", "|---|---|---|"]
    lines += [f"| {r['status']} | {r['step']} | {str(r['detail']).replace('|', '/')} |" for r in RESULTS]
    out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n{len(RESULTS)} checks, {len(fails)} FAIL, {sum(1 for r in RESULTS if r['status'] == 'WARN')} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
