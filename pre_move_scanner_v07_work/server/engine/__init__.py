"""Market engine (books, flows, baselines, features, scoring)."""
import gc


def tune_gc() -> None:
    """Move long-lived startup objects out of GC scanning and make gen-0 collections rarer.

    The scanner holds many long-lived objects (rings, baselines, labels); full
    collections that re-scan them caused tick-time spikes at Top-100 load.
    """
    gc.collect()
    gc.freeze()
    gc.set_threshold(50_000, 20, 100)
