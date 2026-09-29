"""Address / token-identifier validation and normalisation per chain family (stdlib only).

A wallet label, a discovered contract or a manual override is accepted only when it is a
well-formed identifier *for its chain*: an EVM address is never used on Solana, a TRON contract
must pass its base58check checksum, and so on. Nothing here guesses; invalid input returns None.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

B58_BTC = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_XRP = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
XDC_RE = re.compile(r"^xdc[0-9a-fA-F]{40}$", re.I)
HEDERA_RE = re.compile(r"^\d+\.\d+\.\d+$")
CARDANO_TOKEN_RE = re.compile(r"^[0-9a-f]{56}[0-9a-f]{0,64}$")
XRPL_TOKEN_RE = re.compile(r"^([A-Za-z0-9]{3}|[0-9A-Fa-f]{40})\.(r[1-9A-HJ-NP-Za-km-z]{24,34})$")


# ---------------------------------------------------------------- base58
def b58decode(s: str, alphabet: str = B58_BTC) -> Optional[bytes]:
    if not s:
        return None
    n = 0
    for ch in s:
        i = alphabet.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip(alphabet[0]))
    return b"\x00" * pad + full


def b58encode(b: bytes, alphabet: str = B58_BTC) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return alphabet[0] * pad + out


def _dsha(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def b58check_decode(s: str, alphabet: str = B58_BTC) -> Optional[bytes]:
    raw = b58decode(s, alphabet)
    if raw is None or len(raw) < 5:
        return None
    body, chk = raw[:-4], raw[-4:]
    return body if _dsha(body)[:4] == chk else None


def b58check_encode(body: bytes, alphabet: str = B58_BTC) -> str:
    return b58encode(body + _dsha(body)[:4], alphabet)


# ---------------------------------------------------------------- bech32 / bech32m
def _polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def bech32_valid(s: str, hrps=None) -> bool:
    """Checksum-valid bech32 or bech32m string (no length limit: Cardano addresses exceed 90)."""
    if not s or s.lower() != s and s.upper() != s:
        return False
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        return False
    hrp, data = s[:pos], s[pos + 1:]
    if hrps is not None and hrp not in hrps:
        return False
    if any(c not in BECH32_CHARSET for c in data):
        return False
    vals = [BECH32_CHARSET.find(c) for c in data]
    pm = _polymod([ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp] + vals)
    return pm in (1, 0x2BC830A3)


# ---------------------------------------------------------------- per family
def evm_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if XDC_RE.match(a):
        a = "0x" + a[3:]
    return a.lower() if EVM_RE.match(a) else None


def bitcoin_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if a.lower().startswith("bc1"):
        return a.lower() if bech32_valid(a, {"bc"}) else None
    body = b58check_decode(a)
    if body is not None and len(body) == 21 and body[0] in (0x00, 0x05):
        return a
    return None


def solana_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if not 32 <= len(a) <= 44:
        return None
    raw = b58decode(a)
    return a if raw is not None and len(raw) == 32 else None


def xrpl_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if not a.startswith("r"):
        return None
    body = b58check_decode(a, B58_XRP)
    return a if body is not None and len(body) == 21 and body[0] == 0 else None


def tron_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if len(a) == 42 and a.lower().startswith("41"):
        try:
            return b58check_encode(bytes.fromhex(a))
        except ValueError:
            return None
    body = b58check_decode(a)
    return a if body is not None and len(body) == 21 and body[0] == 0x41 else None


def tron_hex_to_base58(h: str) -> Optional[str]:
    h = str(h or "").lower()
    if h.startswith("0x"):
        h = "41" + h[2:]
    return tron_address(h) if len(h) == 42 else None


def hedera_id(a: str) -> Optional[str]:
    a = str(a or "").strip()
    return a if HEDERA_RE.match(a) else None


def cardano_address(a: str) -> Optional[str]:
    a = str(a or "").strip()
    if a.startswith(("addr1", "stake1")) and bech32_valid(a, {"addr", "stake"}):
        return a.lower()
    return None


def normalize_address(family: str, a: str) -> Optional[str]:
    fn = {"evm": evm_address, "bitcoin": bitcoin_address, "solana": solana_address, "xrpl": xrpl_address,
          "tron": tron_address, "hedera": hedera_id, "cardano": cardano_address}.get(family)
    return fn(a) if fn else None


def normalize_token(family: str, t: str) -> Optional[str]:
    """A token identifier on its chain: EVM contract, SPL mint, TRC-20 contract, HTS token id,
    Cardano policy id (+ asset name hex) or XRPL `CURRENCY.issuer`."""
    t = str(t or "").strip()
    if family == "evm":
        return evm_address(t)
    if family == "solana":
        return solana_address(t)
    if family == "tron":
        return tron_address(t)
    if family == "hedera":
        return hedera_id(t)
    if family == "cardano":
        return t.lower() if CARDANO_TOKEN_RE.match(t.lower()) else None
    if family == "xrpl":
        m = XRPL_TOKEN_RE.match(t)
        return f"{m.group(1)}.{m.group(2)}" if m and xrpl_address(m.group(2)) else None
    return None
