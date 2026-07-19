"""Pin the browser-suite xdist grouping policy (tests/e2e/conftest.py's pure helper).

Serial (default) mode must reproduce the historical group names exactly —
"dashboard" / "covreport" appear in JUnit classnames and the Makefile's
comments. Shard mode (OTTO_BROWSER_SHARD=1, CI only) splits the two audited
suites per-file so ``--dist loadgroup`` can spread modules across workers
while module-scoped fixtures still land on a single worker. Any browser suite
NOT in the audited map stays serial in BOTH modes — sharding is opt-in per
suite, after auditing it for parallel safety (port=0 servers, collision-free
coverage dumps).
"""

from tests.e2e.conftest import _browser_group_key


def test_dashboard_serial_group_matches_historical_name() -> None:
    nodeid = "tests/e2e/monitor/dashboard/test_review_shell.py::test_grid"
    assert _browser_group_key(nodeid, shard=False) == "dashboard"


def test_covreport_serial_group_matches_historical_name() -> None:
    nodeid = "tests/e2e/cov/report_browser/test_report_index.py::test_index"
    assert _browser_group_key(nodeid, shard=False) == "covreport"


def test_shard_mode_splits_a_suite_per_file() -> None:
    a = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t", shard=True)
    b = _browser_group_key("tests/e2e/monitor/dashboard/test_b.py::t", shard=True)
    assert a != b
    assert a.startswith("dashboard::")
    assert b.startswith("dashboard::")


def test_shard_mode_keeps_one_file_in_one_group() -> None:
    a = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t1", shard=True)
    b = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t2[chromium]", shard=True)
    assert a == b


def test_unaudited_browser_suite_stays_serial_in_both_modes() -> None:
    nodeid = "tests/e2e/somewhere/test_new_browser_suite.py::t"
    assert _browser_group_key(nodeid, shard=True) == "browser-serial"
    assert _browser_group_key(nodeid, shard=False) == "browser-serial"
