"""Drift guard: committed web/fixtures/ must match regeneration exactly.

If this fails, the generator or the export models changed without
re-stamping the fixtures — run ``make monitor-fixtures`` and commit the
result (the byte-identical guarantee is what makes the fixtures a reliable
contract artifact for the web tests).

Both directions are guarded, and both derive from ``build_all()``. A
hand-written stem list here would silently stop covering the next fixture
someone adds — which is the whole defect class this file used to have.
"""

from pathlib import Path

import pytest

from scripts.gen_monitor_fixtures import build_all, dumps

_FIXTURE_DIR = Path(__file__).parents[3] / "web" / "fixtures"


@pytest.mark.parametrize("stem", sorted(build_all()))
def test_committed_fixture_is_fresh(stem: str):
    committed = (_FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8")
    assert committed == dumps(build_all()[stem]), (
        f"web/fixtures/{stem}.json is stale — run 'make monitor-fixtures' and commit"
    )


def test_fixture_dir_matches_the_generator():
    """The other direction. The freshness guard above walks the GENERATOR's keys,
    so it is blind to a committed .json that ``build_all()`` no longer produces —
    an orphan would sit there stale forever, still imported by name from the web
    tests. Compare the two inventories directly."""
    on_disk = {path.stem for path in _FIXTURE_DIR.glob("*.json")}
    assert on_disk == set(build_all()), (
        "web/fixtures/ and build_all() disagree — either an orphan file whose "
        "generator was removed, or a fixture that was generated but never "
        "committed. Run 'make monitor-fixtures'."
    )
