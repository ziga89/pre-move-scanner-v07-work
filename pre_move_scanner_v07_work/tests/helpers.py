"""Shared helpers for the unittest suite (stdlib only)."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import DEFAULTS, deep_merge  # noqa: E402

T0 = 1_760_000_000.0  # fixed virtual epoch used by scenario tests


def cfg(**overrides):
    """A config dict based on DEFAULTS with nested overrides."""
    c = deep_merge(DEFAULTS, overrides)
    c["_meta"] = {"root": str(ROOT), "path": "", "warnings": []}
    return copy.deepcopy(c)


def fx_one(_quote):
    return 1.0


def scratch_dir(name: str) -> Path:
    base = Path(os.environ.get("PMS_TEST_TMP", ROOT / ".test_tmp"))
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d
