"""v0.8 wallet providers against recorded-format fixtures (no network): normalisation into the one
event model, native coin vs token semantics, cursors, temporary failures, missing keys, rate limits,
retry / fail-over, unsupported chains and budget enforcement.

The same code paths were run live against the real APIs from GitHub-hosted runners
(`tools/wallet_check.py`, workflow `live-wallet.yml`; see docs/TEST_REPORT_V080.md)."""
import asyncio
import os
import unittest

from server.intel.addr import b58check_decode, tron_hex_to_base58
from server.intel.labels import Label, LabelRegistry
from server.intel.model import NATIVE, FetchResult, RawTransfer, TrackedAsset
from server.intel.monitor import WalletMonitor
from server.intel.providers import (BitcoinProvider, CardanoProvider, EvmProvider, HederaProvider, ProviderAuthError,
                                    ProviderBudgetExceeded, ProviderChainUnavailable, ProviderError, ProviderRateLimited,
                                    ProviderRegistry, SolanaProvider, TronProvider, XrplProvider, build_providers)
from server.intel.providers.normalize import allocate_deltas, utxo_transfers
from server.universe.http import HttpError
from tests.helpers import cfg

NOW = 1_760_000_000.0


async def nosleep(_):
    return None


class Http:
    """Scripted HTTP: handler(method, url, params_or_body) -> JSON or an exception to raise."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get_json(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params, headers))
        r = self.handler("GET", url, params or {})
        if isinstance(r, Exception):
            raise r
        return r

    async def post_json(self, url, body, headers=None):
        self.calls.append(("POST", url, body, headers))
        r = self.handler("POST", url, body)
        if isinstance(r, Exception):
            raise r
        return r


def mk(cls, handler, **pcfg):
    return cls(Http(handler), pcfg, {}, clock=lambda: NOW, sleep=nosleep)


def native(sym, chain):
    return TrackedAsset(asset=sym, chain=chain, native=True, status="ok")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- normalisation helpers
class NormalizeTests(unittest.TestCase):
    def test_utxo_spend_change_and_receive(self):
        own = "A"
        # A spends 5 (2 inputs), pays B 3, change 1.9 back to A -> one transfer A->B 3
        out = utxo_transfers(lambda a: a == own, own, [("A", 2.0), ("A", 3.0)], [(0, "B", 3.0), (1, "A", 1.9)])
        self.assertEqual(out, [(0, "A", "B", 3.0, "")])
        # consolidation: everything back to input addresses -> nothing
        self.assertEqual(utxo_transfers(lambda a: a == own, own, [("A", 1.0), ("C", 1.0)], [(0, "C", 1.99)]), [])
        # A receives from a multi-input tx -> attributed to the dominant input, and says so
        out = utxo_transfers(lambda a: a == own, own, [("X", 5.0), ("Y", 1.0)], [(0, "A", 4.0), (1, "Z", 1.9)])
        self.assertEqual(out[0][:4], (0, "X", "A", 4.0))
        self.assertIn("largest input", out[0][4])
        # coinbase (no inputs) -> nothing attributable
        self.assertEqual(utxo_transfers(lambda a: a == own, own, [], [(0, "A", 3.125)]), [])

    def test_allocate_deltas(self):
        self.assertEqual(allocate_deltas("A", {"A": -10.0, "B": 7.0, "C": 3.0}), [("A", "B", 7.0), ("A", "C", 3.0)])
        self.assertEqual(allocate_deltas("A", {"A": 5.0, "B": -8.0, "FEE": -0.1}, ignore={"FEE"}), [("B", "A", 5.0)])
        self.assertEqual(allocate_deltas("A", {"A": 0.0, "B": 1.0}), [])


# ---------------------------------------------------------------- Bitcoin (Esplora)
def btc_tx(txid, height, vin, vout, t=NOW):
    return {"txid": txid, "status": {"confirmed": height is not None, "block_height": height, "block_time": t},
            "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


OWN = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


class BitcoinTests(unittest.TestCase):
    def test_pages_until_cursor_and_normalises(self):
        page1 = [btc_tx("mem", None, [("X", 1)], [(OWN, 1)])] + \
            [btc_tx(f"t{i}", 900 - i, [("X", 50_000_000)], [(OWN, 40_000_000), ("X", 9_990_000)]) for i in range(25)]
        page2 = [btc_tx(f"u{i}", 870 - i, [(OWN, 10 ** 8)], [("Z", 70_000_000), (OWN, 29_000_000)]) for i in range(25)]

        def h(m, url, p):
            if url.endswith(f"/address/{OWN}/txs"):
                return page1
            if "/txs/chain/t24" in url:
                return page2
            return []
        p = mk(BitcoinProvider, h, max_pages_per_poll=2)
        r = run(p.fetch_address("bitcoin", OWN, [native("BTC", "bitcoin")], {"height": 800, "txids": []}))
        self.assertFalse(r.complete)                     # 50 new confirmed txs, cursor not reached in 2 pages
        self.assertEqual(len(r.transfers), 50)           # unconfirmed mempool tx ignored
        t0 = r.transfers[0]
        self.assertEqual((t0.from_address, t0.to_address, t0.amount, t0.token, t0.index), ("X", OWN, 0.4, NATIVE, 0))
        spend = [t for t in r.transfers if t.from_address == OWN]
        self.assertTrue(spend and all(t.to_address == "Z" and t.amount == 0.7 for t in spend))   # change not counted
        self.assertEqual(r.cursor, {"height": 900, "txids": ["t0"]})
        # next poll stops at the cursor
        r2 = run(p.fetch_address("bitcoin", OWN, [native("BTC", "bitcoin")], r.cursor))
        self.assertTrue(r2.complete)
        self.assertEqual(r2.transfers, [])

    def test_fallback_instance_and_balance(self):
        state = {"n": 0}

        def h(m, url, p):
            state["n"] += 1
            if "mempool.space" in url:
                return ConnectionError("connect timeout")
            return {"chain_stats": {"funded_txo_sum": 300_000_000, "spent_txo_sum": 100_000_000}}
        p = mk(BitcoinProvider, h)
        with self.assertRaises(ProviderError):
            run(p.fetch_balance("bitcoin", OWN, native("BTC", "bitcoin")))
        self.assertEqual(run(p.fetch_balance("bitcoin", OWN, native("BTC", "bitcoin"))), 2.0)   # blockstream.info
        self.assertEqual(p.budget.errors, 1)
        self.assertEqual(p.state()["state"], "ok")
        self.assertIsNone(p.normalize_address("bitcoin", "0x" + "11" * 20))
        self.assertEqual(p.normalize_address("bitcoin", "BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ"),
                         "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")


# ---------------------------------------------------------------- XRPL
R_A, R_B = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz"


def xrpl_entry(h, led, frm, to, delivered, result="tesSUCCESS", typ="Payment", v2=False, **extra):
    tx = {"TransactionType": typ, "Account": frm, "Destination": to, "Amount": "999999999999", "date": 800_000_000,
          "ledger_index": led, **extra}
    meta = {"TransactionResult": result, "delivered_amount": delivered}
    if v2:
        return {"tx_json": tx, "hash": h, "meta": meta, "validated": True, "ledger_index": led}
    return {"tx": dict(tx, hash=h), "meta": meta, "validated": True}


class XrplTests(unittest.TestCase):
    def test_delivered_amount_types_and_cursor(self):
        txs = [xrpl_entry("H1", 100, R_B, R_A, "2500000", DestinationTag=77),                 # 2.5 XRP in
               xrpl_entry("H2", 101, R_A, R_B, {"currency": "SOLO", "issuer": R_B, "value": "5"}),  # IOU (not tracked)
               xrpl_entry("H3", 102, R_A, R_B, "1000000", result="tecPATH_DRY"),             # failed
               xrpl_entry("H4", 103, R_A, R_B, "unavailable"),                               # never guessed
               xrpl_entry("H5", 104, R_A, R_B, "7000000", typ="OfferCreate"),                # not a payment
               xrpl_entry("H6", 105, R_A, R_B, "3000000", v2=True)]                          # API v2 shape

        def h(m, url, body):
            self.assertEqual(m, "POST")
            p = body["params"][0]
            self.assertEqual(body["method"], "account_tx")
            self.assertFalse(p["forward"])                  # first poll: newest page
            return {"result": {"status": "success", "transactions": txs}}
        p = mk(XrplProvider, h)
        r = run(p.fetch_address("xrpl", R_A, [native("XRP", "xrpl")], {}))
        got = [(t.tx_hash, t.from_address, t.to_address, t.amount) for t in r.transfers]
        self.assertEqual(got, [("H1", R_B, R_A, 2.5), ("H6", R_A, R_B, 3.0)])
        self.assertEqual(r.transfers[0].ts, 800_000_000 + 946684800)
        self.assertIn("destination tag 77", r.transfers[0].note)
        self.assertEqual(r.cursor, {"ledger": 105})

    def test_issued_token_and_forward_paging(self):
        calls = []

        def h(m, url, body):
            p = body["params"][0]
            calls.append(p)
            if "marker" not in p:
                return {"result": {"status": "success", "marker": {"ledger": 1, "seq": 2}, "transactions": [
                    xrpl_entry("T1", 201, R_B, R_A, {"currency": "SOLO", "issuer": R_B, "value": "12.5"})]}}
            return {"result": {"status": "success", "transactions": []}}
        tok = TrackedAsset(asset="SOLO", chain="xrpl", native=False, token=f"SOLO.{R_B}")
        p = mk(XrplProvider, h)
        r = run(p.fetch_address("xrpl", R_A, [tok], {"ledger": 200}))
        self.assertTrue(r.complete)
        self.assertEqual([(t.asset, t.amount, t.token) for t in r.transfers], [("SOLO", 12.5, f"SOLO.{R_B}")])
        self.assertTrue(calls[0]["forward"] and calls[0]["ledger_index_min"] == 200)

    def test_too_busy_fails_over_and_backs_off(self):
        def h(m, url, body):
            if "xrplcluster" in url:
                return {"result": {"status": "error", "error": "tooBusy"}}
            return {"result": {"status": "success", "account_data": {"Balance": "12000000"}}}
        p = mk(XrplProvider, h)
        with self.assertRaises(ProviderRateLimited):
            run(p.fetch_balance("xrpl", R_A, native("XRP", "xrpl")))
        self.assertEqual(p.state()["state"], "degraded")         # backing off
        self.assertEqual(p.stats()["rate_limit_hits"], 1)
        p.budget.backoff_until = 0
        self.assertEqual(run(p.fetch_balance("xrpl", R_A, native("XRP", "xrpl"))), 12.0)   # next server
        self.assertIn("s1.ripple.com", p.http.calls[-1][1])


# ---------------------------------------------------------------- TRON
T_A = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
T_HEX = b58check_decode(T_A).hex()


class TronTests(unittest.TestCase):
    def test_trc20_and_native_trx(self):
        other = tron_hex_to_base58("41" + "11" * 20)

        def h(m, url, p):
            if url.endswith("/transactions/trc20"):
                self.assertEqual(p["order_by"], "block_timestamp,desc")
                return {"success": True, "data": [
                    {"transaction_id": "tx1", "block_timestamp": 1_700_000_000_000, "from": other, "to": T_A,
                     "type": "Transfer", "value": "2500000", "token_info": {"symbol": "USDT", "address": USDT, "decimals": 6}},
                    {"transaction_id": "tx2", "block_timestamp": 1_700_000_001_000, "from": T_A, "to": other,
                     "type": "Approval", "value": "1", "token_info": {"symbol": "USDT", "address": USDT, "decimals": 6}}]}
            if url.endswith("/transactions"):
                return {"success": True, "data": [
                    {"txID": "n1", "blockNumber": 5, "block_timestamp": 1_700_000_002_000, "ret": [{"contractRet": "SUCCESS"}],
                     "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
                         "amount": 3_000_000, "owner_address": T_HEX, "to_address": "41" + "11" * 20}}}]}},
                    {"txID": "n2", "block_timestamp": 1_700_000_003_000, "ret": [{"contractRet": "REVERT"}],
                     "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
                         "amount": 9, "owner_address": T_HEX, "to_address": "41" + "11" * 20}}}]}},
                    {"internal_tx_id": "i1", "block_timestamp": 1_700_000_004_000}]}
            return {}
        p = mk(TronProvider, h)
        tok = TrackedAsset(asset="USDT", chain="tron", native=False, token=USDT)
        r = run(p.fetch_address("tron", T_A, [tok, native("TRX", "tron")], {}))
        got = [(t.asset, t.from_address, t.to_address, t.amount) for t in r.transfers]
        self.assertEqual(got, [("USDT", other, T_A, 2.5), ("TRX", T_A, other, 3.0)])
        self.assertEqual(r.cursor, {"trc20_ts": 1_700_000_001_000, "trx_ts": 1_700_000_004_000})
        self.assertEqual(r.transfers[0].symbol, "USDT")
        self.assertIn("contract-internal", r.note)
        self.assertEqual(p.headers, {})                  # key optional: none set, none sent

    def test_optional_key_header_and_forward_paging(self):
        os.environ["PMS_TEST_TRON"] = "secret-value"
        pages = []

        def h(m, url, p):
            pages.append(p)
            if "fingerprint" not in p:
                return {"data": [{"txID": f"a{i}", "block_timestamp": 10 + i} for i in range(2)],
                        "meta": {"fingerprint": "next"}}
            return {"data": [], "meta": {}}
        p = mk(TronProvider, h, api_key_env="PMS_TEST_TRON", page_size=2)
        r = run(p.fetch_address("tron", T_A, [native("TRX", "tron")], {"trx_ts": 5}))
        self.assertTrue(r.complete)
        self.assertEqual(pages[0]["min_timestamp"], 5)
        self.assertEqual(p.http.calls[0][3], {"TRON-PRO-API-KEY": "secret-value"})
        self.assertNotIn("secret-value", str(p.stats()))


# ---------------------------------------------------------------- Solana
S_OWNER = "Sgo6roPnWxZUtDHKBeJkxVyUVWYcGwZh5hgX6w6pXHH"
S_OTHER = "CsVdJ8WH8Q9e1rmxxbNQ7jUc5fHs6MfxEZ4FzHkQ9YsL"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def sol_tx(pre, post, keys, fee=5000, tpre=(), tpost=()):
    return {"blockTime": 1_700_000_000, "slot": 42,
            "meta": {"err": None, "fee": fee, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": list(tpre), "postTokenBalances": list(tpost)},
            "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}}}


def tb(owner, amount, dec=6, mint=MINT, idx=1):
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount), "decimals": dec}}


class SolanaTests(unittest.TestCase):
    def test_native_and_spl_normalisation_with_version_retry(self):
        sent = []

        def h(m, url, body):
            meth = body["method"]
            sent.append(body)
            if meth == "getSignaturesForAddress":
                return {"result": [{"signature": "sigB", "err": None}, {"signature": "sigA", "err": None},
                                   {"signature": "sigF", "err": {"InstructionError": [0, "x"]}}]}
            if meth == "getTokenAccountsByOwner":
                return {"result": {"value": [{"pubkey": "TokAcct111"}]}}
            if meth == "getTransaction":
                if body["params"][1]["maxSupportedTransactionVersion"] < 2:
                    return {"error": {"code": -32015, "message": "Transaction version (2) is not supported by the "
                                                                 "requesting client. Please try the request again with "
                                                                 "the following configuration parameter: "
                                                                 "\"maxSupportedTransactionVersion\": 2"}}
                if body["params"][0] == "sigA":
                    # owner (fee payer) sends 2 SOL to S_OTHER; fee removed from the payer before allocation
                    return {"result": sol_tx([10_000_000_000, 0], [7_999_995_000, 2_000_000_000], [S_OWNER, S_OTHER])}
                return {"result": sol_tx([1, 1], [1, 1], [S_OTHER, S_OWNER],
                                         tpre=[tb(S_OTHER, 9_000_000, idx=2), tb(S_OWNER, 0)],
                                         tpost=[tb(S_OTHER, 1_500_000, idx=2), tb(S_OWNER, 7_500_000)])}
            return {"result": None}
        p = mk(SolanaProvider, h)
        r = run(p.fetch_address("solana", S_OWNER, [native("SOL", "solana")], {}))
        self.assertEqual(p.max_tx_version, 2)                              # raised when the node asked for it
        sol = [(t.from_address, t.to_address, t.amount) for t in r.transfers]
        self.assertIn((S_OWNER, S_OTHER, 2.0), sol)
        self.assertEqual(r.cursor["sig:" + S_OWNER], "sigB")
        tok = TrackedAsset(asset="USDC", chain="solana", native=False, token=MINT)
        r = run(p.fetch_address("solana", S_OWNER, [tok], {}))
        self.assertEqual(r.cursor["ta:" + MINT], ["TokAcct111"])            # owner's token account watched
        spl = [(t.from_address, t.to_address, t.amount) for t in r.transfers]
        self.assertIn((S_OTHER, S_OWNER, 7.5), spl)

    def test_private_rpc_from_environment_and_rate_limit(self):
        os.environ["PMS_TEST_SOL_RPC"] = "https://rpc.example/?api-key=very-secret"

        def h(m, url, body):
            return HttpError(429, "Too Many Requests", retry_after=30)
        p = mk(SolanaProvider, h, rpc_url_env="PMS_TEST_SOL_RPC")
        self.assertTrue(p.private_rpc)
        self.assertNotIn("very-secret", str(p.stats()))
        with self.assertRaises(ProviderRateLimited):
            run(p.fetch_balance("solana", S_OWNER, native("SOL", "solana")))
        self.assertEqual(p.stats()["backoff_s"], 30.0)
        with self.assertRaises(ProviderRateLimited):                          # never hammers during back-off
            run(p.fetch_balance("solana", S_OWNER, native("SOL", "solana")))
        self.assertEqual(len(p.http.calls), 1)


# ---------------------------------------------------------------- Hedera
class HederaTests(unittest.TestCase):
    def test_fee_accounts_payer_fee_and_tokens(self):
        tx = {"consensus_timestamp": "1700000000.000000001", "transaction_id": "0.0.1001-1700000000-000000000",
              "result": "SUCCESS", "node": "0.0.3", "charged_tx_fee": 100_000,
              "transfers": [{"account": "0.0.1001", "amount": -500_100_000}, {"account": "0.0.2002", "amount": 500_000_000},
                            {"account": "0.0.3", "amount": 20_000}, {"account": "0.0.98", "amount": 80_000}],
              "token_transfers": [{"token_id": "0.0.7777", "account": "0.0.2002", "amount": -250},
                                  {"token_id": "0.0.7777", "account": "0.0.1001", "amount": 250}]}
        failed = dict(tx, result="INSUFFICIENT_PAYER_BALANCE", consensus_timestamp="1700000001.0")

        def h(m, url, p):
            if url.endswith("/api/v1/tokens/0.0.7777"):
                return {"decimals": "2"}
            return {"transactions": [tx, failed], "links": {"next": None}}
        p = mk(HederaProvider, h)
        tok = TrackedAsset(asset="HTK", chain="hedera", native=False, token="0.0.7777")
        r = run(p.fetch_address("hedera", "0.0.1001", [native("HBAR", "hedera"), tok], {}))
        got = sorted((t.asset, t.from_address, t.to_address, t.amount) for t in r.transfers)
        self.assertEqual(got, [("HBAR", "0.0.1001", "0.0.2002", 5.0), ("HTK", "0.0.2002", "0.0.1001", 2.5)])
        self.assertEqual(r.cursor, {"ts": "1700000001.0"})


# ---------------------------------------------------------------- Cardano
ADDR = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3n0d3vllmyqwsx5wktcd8cc3sq835lu7drv2xwl2wywfgse35a3x"
STAKE = "stake1uyehkck0lajq8gr28t9uxnuvgcqrc6070x3k9r8048z8y5gh6ffgw"


class CardanoTests(unittest.TestCase):
    def test_stake_identity_utxo_and_native_token(self):
        pol = "ab" * 28

        def io(pay, stake, lovelace, idx=None, assets=()):
            d = {"payment_addr": {"bech32": pay}, "stake_addr": stake, "value": str(lovelace),
                 "asset_list": [{"policy_id": pol, "asset_name": "4d59", "decimals": 2, "quantity": str(q)} for q in assets]}
            if idx is not None:
                d["tx_index"] = idx
            return d

        def h(m, url, body):
            if "/account_txs" in url:
                self.assertIn("order=block_height.desc", url)
                return [{"tx_hash": "c1", "block_height": 10}]
            if url.endswith("/tx_info"):
                return [{"tx_hash": "c1", "block_height": 10, "tx_timestamp": 1_700_000_000,
                         "inputs": [io("addr1_mine_a", STAKE, 12_000_000, assets=[500])],
                         "outputs": [io("addr1_them", "stake1_them", 7_000_000, 0, assets=[300]),
                                     io("addr1_mine_b", STAKE, 4_800_000, 1, assets=[200])]}]
            return []
        p = mk(CardanoProvider, h)
        tok = TrackedAsset(asset="MY", chain="cardano", native=False, token=pol + "4d59")
        r = run(p.fetch_address("cardano", STAKE, [native("ADA", "cardano"), tok], {}))
        got = sorted((t.asset, t.from_address, t.to_address, t.amount) for t in r.transfers)
        self.assertEqual(got, [("ADA", STAKE, "stake1_them", 7.0), ("MY", STAKE, "stake1_them", 3.0)])   # change skipped
        self.assertEqual(r.cursor, {"height": 10})
        self.assertEqual(p.normalize_address("cardano", ADDR), ADDR)


# ---------------------------------------------------------------- EVM provider specifics
class EvmTests(unittest.TestCase):
    def setUp(self):
        os.environ["PMS_TEST_ES"] = "k-123"

    def test_missing_key_is_no_key(self):
        os.environ.pop("PMS_TEST_ES_MISSING", None)
        p = EvmProvider(Http(lambda *a: {}), {"api_key_env": "PMS_TEST_ES_MISSING"})
        self.assertEqual(p.state()["state"], "no_key")
        with self.assertRaises(ProviderAuthError):
            run(p.tokentx("ethereum", address="0x" + "11" * 20))
        self.assertEqual(p.http.calls, [])                               # nothing sent without a key

    def test_plan_limited_chain_is_reported(self):
        def h(m, url, p):
            return {"status": "0", "message": "NOTOK",
                    "result": "Free API access is not supported for this chain. Please upgrade your api plan for full chain coverage."}
        p = mk(EvmProvider, h, api_key_env="PMS_TEST_ES")
        with self.assertRaises(ProviderChainUnavailable):
            run(p.txlist("base", "0x" + "11" * 20))
        st = p.state("base")
        self.assertEqual(st["state"], "degraded")
        self.assertIn("upgrade your api plan", st["reason"])
        self.assertEqual(p.state("ethereum")["state"], "ok")             # other chains unaffected

    def test_proxy_module_and_xdc_chain_id(self):
        def h(m, url, p):
            return {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
        p = mk(EvmProvider, h, api_key_env="PMS_TEST_ES")
        self.assertEqual(run(p.call("xdc", {"module": "proxy", "action": "eth_blockNumber"})), "0x10")
        self.assertEqual(p.http.calls[0][2]["chainid"], 50)
        self.assertEqual(p.normalize_address("xdc", "xdc" + "AB" * 20), "0x" + "ab" * 20)

    def test_budget_never_exceeded(self):
        p = mk(EvmProvider, lambda *a: {"status": "1", "result": []}, api_key_env="PMS_TEST_ES", daily_call_budget=3)
        for _ in range(3):
            run(p.tokentx("ethereum", address="0x" + "11" * 20))
        with self.assertRaises(ProviderBudgetExceeded):
            run(p.tokentx("ethereum", address="0x" + "11" * 20))
        self.assertEqual(len(p.http.calls), 3)
        self.assertEqual(p.stats()["remaining_today"], 0)
        self.assertEqual(p.state()["state"], "degraded")


# ---------------------------------------------------------------- monitor across chains
class FlakyProvider:
    """A provider whose fetch fails / rate-limits on demand (monitor behaviour on provider errors)."""
    name = "flaky"
    label = "Flaky"
    family = "bitcoin"
    requires_key = False
    enabled = True
    keyed = True

    def __init__(self):
        self.chains = frozenset({"bitcoin"})
        self.mode = "ok"
        self.calls = 0
        self.daily = 1000

    def supports(self, chain):
        return chain in self.chains

    def supports_token_wide(self):
        return False

    def remaining_today(self):
        return self.daily - self.calls

    async def fetch_address(self, chain, address, assets, cursor):
        self.calls += 1
        if self.mode == "down":
            raise ProviderError("connection reset")
        if self.mode == "429":
            raise ProviderRateLimited("HTTP 429")
        return FetchResult([RawTransfer("bitcoin", "BTC", NATIVE, f"tx{self.calls}", 0, NOW, address, OWN, 12.0,
                                        provider=self.name)], {"height": self.calls})

    async def fetch_balance(self, chain, address, asset):
        return None


class MultiChainMonitorTests(unittest.TestCase):
    def mk(self):
        lab = LabelRegistry()
        lab.add(Label("bitcoin", "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "Exchange A", "CEX_HOT", "HIGH", "t"))
        prov = FlakyProvider()
        c = dict(cfg(intel={"enabled": True})["intel"], tokens={}, address_poll_min_seconds=0)
        m = WalletMonitor(c, lab, ProviderRegistry([prov]), lambda a: 50_000.0, clock=lambda: NOW)
        m.track(TrackedAsset("BTC", "bitcoin", True, status="ok"))
        return m, prov

    def test_normalised_events_from_a_non_evm_chain(self):
        m, prov = self.mk()
        done = run(m.run_once())
        self.assertEqual(done["address"], 1)
        ev = m.transfers[0]
        self.assertEqual((ev["chain"], ev["event_type"], ev["from_entity"], ev["usd_value"], ev["provider"]),
                         ("bitcoin", "CEX_OUT", "Exchange A", 600_000.0, "flaky"))
        self.assertEqual(m.cursors["cur:flaky:bitcoin:3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"], {"height": 1})
        self.assertTrue(m.coverage("BTC")["covered"])

    def test_temporary_failure_and_rate_limit_then_recovery(self):
        m, prov = self.mk()
        prov.mode = "down"
        done = run(m.run_once())
        self.assertEqual((done["address"], done["errors"]), (0, 1))
        self.assertEqual(m.status, "degraded")
        self.assertFalse(m.coverage("BTC")["covered"])
        st = next(iter(v for k, v in m.addr_state.items() if k[0] == "bitcoin"))
        self.assertIn("connection reset", st["last_error"])
        prov.mode = "429"
        for v in m.addr_state.values():
            v["next"] = 0
        self.assertEqual(run(m.run_once())["errors"], 1)
        prov.mode = "ok"                                              # reconnect / retry on the next cycle
        for v in m.addr_state.values():
            v["next"] = 0
        self.assertEqual(run(m.run_once())["address"], 1)
        self.assertTrue(m.coverage("BTC")["covered"])

    def test_priority_tiers_space_out_quiet_assets(self):
        m, prov = self.mk()
        m.cfg["address_poll_min_seconds"] = 60
        m.priority = {"BTC": 5}
        run(m.run_once())
        quiet_iv = next(v for k, v in m.addr_state.items() if k[0] == "bitcoin")["interval"]
        m.priority = {"BTC": 1}
        for v in m.addr_state.values():
            v["next"] = 0
        run(m.run_once())
        hot_iv = next(v for k, v in m.addr_state.items() if k[0] == "bitcoin")["interval"]
        self.assertGreater(quiet_iv, hot_iv * 7.9)

    def test_legacy_rows_reclassified_with_current_labels(self):
        m, _ = self.mk()
        m.load_history([{"chain": "bitcoin", "tx_hash": "old", "log_index": 0, "ts": NOW - 60, "asset": "BTC",
                         "token": "native", "from_addr": "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "to_addr": OWN,
                         "amount": 1.0, "usd_value": 50_000.0, "classification": "SHIFT", "from_type": "MM"}])
        self.assertEqual(m.transfers[0]["event_type"], "CEX_OUT")
        self.assertIn(("bitcoin", "old", 0), m.seen)

    def test_unsupported_chain_is_never_polled(self):
        reg = build_providers(cfg(intel={"enabled": True})["intel"], None, {})
        self.assertFalse(reg.supports("sui"))
        self.assertEqual(reg.chain_state("sui")["state"], "unsupported")
        m = WalletMonitor(dict(cfg()["intel"], tokens={}), LabelRegistry(), reg, lambda a: 1.0, clock=lambda: NOW)
        m.track(TrackedAsset("SUI", "sui", True))
        self.assertEqual(m.schedule(), [])
        self.assertTrue(m.coverage("SUI")["unsupported"])


if __name__ == "__main__":
    unittest.main()
