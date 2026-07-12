"""Drift guard: committed web/fixtures/ must match regeneration exactly.

If this fails, the generator or the export models changed without
re-stamping the fixtures — run ``make monitor-fixtures`` and commit the
result (the byte-identical guarantee is what makes the fixtures a reliable
contract artifact for the web tests).
"""

from pathlib import Path

import pytest

from scripts.gen_monitor_fixtures import build_all, dumps

_FIXTURE_DIR = Path(__file__).parents[3] / "web" / "fixtures"


@pytest.mark.parametrize("stem", ["kitchen-sink", "minimal", "drift", "cascade"])
def test_committed_fixture_is_fresh(stem: str):
    committed = (_FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8")
    assert committed == dumps(build_all()[stem]), (
        f"web/fixtures/{stem}.json is stale — run 'make monitor-fixtures' and commit"
    )
