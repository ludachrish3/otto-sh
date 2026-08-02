"""Seed contract: reproducible when pinned, printed always, bounded offsets."""

import random

from tests.e2e.chaos._seed import offset_in, resolve_seed


def test_seed_env_pin_wins(monkeypatch):
    monkeypatch.setenv("OTTO_CHAOS_SEED", "12345")
    assert resolve_seed() == 12345


def test_unpinned_seeds_vary_and_are_ints(monkeypatch):
    monkeypatch.delenv("OTTO_CHAOS_SEED", raising=False)
    seeds = {resolve_seed() for _ in range(8)}
    assert all(isinstance(s, int) for s in seeds)
    assert len(seeds) > 1  # os.urandom-backed — collisions across 8 draws mean it's broken


def test_same_seed_same_offsets():
    a = [offset_in(random.Random(99), 0.0, 5.0) for _ in range(20)]  # noqa: S311 — reproducibility, not security (seeded PRNG for chaos offsets)
    b = [offset_in(random.Random(99), 0.0, 5.0) for _ in range(20)]  # noqa: S311 — reproducibility, not security (seeded PRNG for chaos offsets)
    assert a == b
    assert all(0.0 <= x <= 5.0 for x in a)
