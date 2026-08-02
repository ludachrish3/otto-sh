"""Seed-reproducible injection offsets (chaos spec, Tier 3).

The ONLY sanctioned randomness in the chaos lane. Every scenario draws its
injection offset from the per-test ``chaos_rng`` fixture; the seed prints on
every run as ``chaos seed: N (reproduce with OTTO_CHAOS_SEED=N)`` — captured
output surfaces it on failure, which is the reproduce path.
"""

import os
import random

_ENV = "OTTO_CHAOS_SEED"


def resolve_seed() -> int:
    pinned = os.environ.get(_ENV)
    if pinned:
        return int(pinned)
    return int.from_bytes(os.urandom(4), "big")


def offset_in(rng: random.Random, lo: float, hi: float) -> float:
    """Uniform offset within a phase window [lo, hi] seconds."""
    return rng.uniform(lo, hi)
