"""Build exchange adapters for the configured mode (live ccxt or SIM)."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from ..sim import SimWorld
from .ccxt_adapter import CcxtAdapter
from .sim_adapter import SimAdapter, SimDriver


def build_adapters(cfg: Dict[str, Any], clock=time.time, exchanges=None) -> Tuple[Dict[str, Any], Optional[SimDriver]]:
    if cfg.get("mode") == "sim" or cfg["feeds"].get("backend") == "sim":
        s = cfg.get("sim", {})
        world = SimWorld(n_assets=int(s.get("assets", 12)), venues_per_asset=int(s.get("venues_per_asset", 4)),
                         seed=int(s.get("seed", 7)), scenarios=bool(s.get("scenarios", True)),
                         cycle_seconds=float(s.get("cycle_seconds", 3600)))
        world.speed = float(s.get("speed", 1.0))
        driver = SimDriver(world, clock=clock, step_s=0.5)
        names = sorted({ex for ex, _ in world.markets})
        return {ex: SimAdapter(ex, driver) for ex in names if exchanges is None or ex in exchanges}, driver
    names = cfg["discovery"]["exchanges"]
    return {ex: CcxtAdapter(ex) for ex in names if exchanges is None or ex in exchanges}, None
