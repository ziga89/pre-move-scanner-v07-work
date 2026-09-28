"""Print the exchange capability matrix from the INSTALLED ccxt (no network).

Fails (exit 1) if a configured exchange id does not exist in ccxt.pro, so CI
catches wrong ids before they reach a live machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import DEFAULTS  # noqa: E402
from server.feeds.capabilities import resolve_caps  # noqa: E402
from server.feeds.ccxt_adapter import CcxtAdapter  # noqa: E402


def main() -> int:
    import ccxt
    print(f"ccxt {ccxt.__version__}")
    bad = 0
    print(f"{'exchange':10} {'book':6} {'trades':6} {'sym/conn':>8} {'sym/call':>8}  runtime has")
    for ex in DEFAULTS["discovery"]["exchanges"]:
        try:
            caps = resolve_caps(ex, CcxtAdapter(ex).has())
            print(f"{ex:10} {caps.book_mode:6} {caps.trade_mode:6} {caps.max_symbols_per_connection:8} "
                  f"{caps.max_symbols_per_call:8}  {caps.runtime_has}")
            if caps.book_mode == "none":
                print(f"  WARNING: {ex} has no websocket order book in this ccxt version")
        except Exception as exc:
            bad += 1
            print(f"{ex:10} ERROR {exc!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
