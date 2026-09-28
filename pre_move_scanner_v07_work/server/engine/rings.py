"""Fixed-size ring buffers.

Memory is bounded by construction: a market uses the same amount of memory
whether it prints 1 trade per minute or 500 per second. That removes the
silent truncation of v0.6's raw-message deques (C4).
"""
from __future__ import annotations

from array import array
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class SecondRing:
    """Per-second accumulators for a set of fields, `size` seconds deep."""

    __slots__ = ("size", "fields", "data", "stamp", "last_sec")

    def __init__(self, fields: Sequence[str], size: int):
        self.size = int(size)
        self.fields = tuple(fields)
        zeros = bytes(8 * self.size)
        self.data: Dict[str, array] = {f: array("d", zeros) for f in self.fields}
        self.stamp = array("q", [-1] * self.size)
        self.last_sec = -1

    def _zero(self, s: int) -> None:
        i = s % self.size
        self.stamp[i] = s
        for arr in self.data.values():
            arr[i] = 0.0

    def _slot(self, sec: int) -> int:
        if sec > self.last_sec:
            # Moving forward: zero every skipped second so windows never see
            # values left over from a previous lap of the ring.
            for s in range(max(self.last_sec + 1, sec - self.size + 1), sec + 1):
                self._zero(s)
            self.last_sec = sec
            return sec % self.size
        i = sec % self.size
        if self.stamp[i] != sec:
            self._zero(sec)
        return i

    def advance(self, sec: int) -> None:
        """Zero any slots for seconds up to `sec` that received no writes."""
        if sec > self.last_sec:
            self._slot(sec)

    def _too_old(self, sec: int) -> bool:
        return sec <= self.last_sec - self.size

    def add(self, sec: int, field: str, value: float) -> None:
        if self._too_old(sec):
            return
        i = self._slot(sec)
        self.data[field][i] += value

    def put_max(self, sec: int, field: str, value: float) -> None:
        if self._too_old(sec):
            return
        i = self._slot(sec)
        arr = self.data[field]
        if value > arr[i]:
            arr[i] = value

    def put(self, sec: int, field: str, value: float) -> None:
        if self._too_old(sec):
            return
        i = self._slot(sec)
        self.data[field][i] = value

    def _ranges(self, end_sec: int, window: int) -> List[Tuple[int, int]]:
        window = min(window, self.size)
        start = end_sec - window + 1
        i0 = start % self.size
        i1 = end_sec % self.size
        if i0 <= i1:
            return [(i0, i1 + 1)]
        return [(i0, self.size), (0, i1 + 1)]

    def sum(self, field: str, end_sec: int, window: int) -> float:
        """Sum over seconds (end_sec-window, end_sec]. Call advance(end_sec) first."""
        arr = self.data[field]
        return sum(sum(arr[a:b]) for a, b in self._ranges(end_sec, window))

    def max(self, field: str, end_sec: int, window: int) -> float:
        arr = self.data[field]
        best = 0.0
        for a, b in self._ranges(end_sec, window):
            if b > a:
                m = max(arr[a:b])
                if m > best:
                    best = m
        return best

    def get(self, field: str, sec: int) -> Optional[float]:
        i = sec % self.size
        if self.stamp[i] != sec:
            return None
        return self.data[field][i]


class PriceRing:
    """1-second last-value series (e.g. mid price), `size` seconds deep."""

    __slots__ = ("size", "vals", "stamp", "last_sec")

    def __init__(self, size: int = 3600):
        self.size = int(size)
        self.vals = array("d", bytes(8 * self.size))
        self.stamp = array("q", [-1] * self.size)
        self.last_sec = -1

    def put(self, sec: int, value: float) -> None:
        if value is None or value <= 0:
            return
        i = sec % self.size
        self.vals[i] = value
        self.stamp[i] = sec
        if sec > self.last_sec:
            self.last_sec = sec

    def at_or_before(self, sec: int, max_back: int = 120) -> Optional[float]:
        """Value at `sec`, or the nearest earlier one within `max_back` seconds."""
        for s in range(sec, sec - max_back - 1, -1):
            i = s % self.size
            if self.stamp[i] == s:
                return self.vals[i]
        return None

    def latest(self) -> Optional[float]:
        if self.last_sec < 0:
            return None
        return self.at_or_before(self.last_sec, 5)

    def span_seconds(self, now_sec: int) -> int:
        """How many seconds of history are available (approx)."""
        oldest = None
        for i in range(self.size):
            s = self.stamp[i]
            if s >= 0 and s > now_sec - self.size and (oldest is None or s < oldest):
                oldest = s
        return 0 if oldest is None else now_sec - oldest

    def series(self, end_sec: int, window: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        for s in range(end_sec - window + 1, end_sec + 1):
            i = s % self.size
            out.append(self.vals[i] if self.stamp[i] == s else None)
        return out

    def range_pct(self, end_sec: int, window: int) -> Optional[float]:
        lo = hi = None
        for s in range(end_sec - window + 1, end_sec + 1):
            i = s % self.size
            if self.stamp[i] != s:
                continue
            v = self.vals[i]
            if lo is None or v < lo:
                lo = v
            if hi is None or v > hi:
                hi = v
        if lo is None or lo <= 0:
            return None
        return (hi / lo - 1.0) * 100.0


class MinuteRing:
    """Column-oriented per-minute records (e.g. 1440 minutes = 24 h)."""

    __slots__ = ("size", "fields", "cols", "ts", "count", "head")

    def __init__(self, fields: Sequence[str], size: int = 1440):
        self.size = int(size)
        self.fields = tuple(fields)
        self.cols: Dict[str, array] = {f: array("d", bytes(8 * self.size)) for f in self.fields}
        self.ts = array("q", [-1] * self.size)
        self.count = 0
        self.head = -1  # index of newest

    def append(self, minute_ts: int, rec: Dict[str, Optional[float]]) -> None:
        if self.head >= 0 and minute_ts <= self.ts[self.head]:
            # Out-of-order or duplicate minute (e.g. rehydration overlap): overwrite newest.
            if minute_ts == self.ts[self.head]:
                idx = self.head
            else:
                return
        else:
            self.head = (self.head + 1) % self.size
            idx = self.head
            self.count = min(self.count + 1, self.size)
        self.ts[idx] = minute_ts
        for f in self.fields:
            v = rec.get(f)
            self.cols[f][idx] = float("nan") if v is None else float(v)

    def newest_ts(self) -> Optional[int]:
        return None if self.head < 0 else self.ts[self.head]

    def iter_window(self, start_ts: int, end_ts: int) -> Iterable[int]:
        """Indices of records with start_ts <= ts < end_ts (newest first)."""
        idx = self.head
        for _ in range(self.count):
            t = self.ts[idx]
            if t < start_ts:
                break
            if t < end_ts:
                yield idx
            idx = (idx - 1) % self.size

    def values(self, field: str, start_ts: int, end_ts: int,
               valid_field: Optional[str] = None, min_valid: float = 0.0) -> List[float]:
        col = self.cols[field]
        vcol = self.cols[valid_field] if valid_field else None
        out = []
        for i in self.iter_window(start_ts, end_ts):
            if vcol is not None and not (vcol[i] >= min_valid):
                continue
            v = col[i]
            if v == v:  # not NaN
                out.append(v)
        return out

    def record(self, idx: int) -> Dict[str, Optional[float]]:
        return {f: (None if self.cols[f][idx] != self.cols[f][idx] else self.cols[f][idx]) for f in self.fields}
