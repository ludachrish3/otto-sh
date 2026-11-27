"""The nightly cut sweep's shards partition its cases: none dropped, none run twice."""

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT
from tests.cut_sweep.test_probe_line_cut_sweep import all_cuts, shard

NIGHTLY_SHARDS = 4
"""The shard count of nightly's ``probe-cut-sweep`` matrix (``.github/workflows/nightly.yml``)."""


def _ids(cases: list) -> list[str]:
    return [case.id for case in cases]


def test_the_nightly_shards_partition_every_case():
    every = _ids(all_cuts())
    shards = [
        _ids(shard(all_cuts(), f"{i}/{NIGHTLY_SHARDS}")) for i in range(1, NIGHTLY_SHARDS + 1)
    ]
    assert sorted(id_ for one in shards for id_ in one) == sorted(every)  # union, no repeats
    assert len(set(every)) == len(every)
    for a in range(NIGHTLY_SHARDS):
        for b in range(a + 1, NIGHTLY_SHARDS):
            assert not set(shards[a]) & set(shards[b])
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


def test_no_shard_is_every_case():
    assert _ids(shard(all_cuts(), None)) == _ids(all_cuts())


@pytest.mark.parametrize("spec", ["0/4", "5/4", "1/0", "2", "a/4"])
def test_a_malformed_shard_or_one_outside_its_count_is_refused(spec):
    with pytest.raises(ValueError, match="CUT_SWEEP_SHARD"):
        shard(all_cuts(), spec)


def test_the_workflow_runs_every_shard():
    text = (CHECKOUT_ROOT / ".github/workflows/nightly.yml").read_text()
    assert f"shard: [{', '.join(str(i) for i in range(1, NIGHTLY_SHARDS + 1))}]" in text
    assert f"CUT_SWEEP_SHARD=${{{{ matrix.shard }}}}/{NIGHTLY_SHARDS}" in text
