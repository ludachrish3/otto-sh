"""End-to-end tier conftest — three path-keyed responsibilities.

All key off membership in the ``tests/e2e/`` tree:

1. **Auto-stamp the ``e2e`` level marker** on every test here (mirrors
   ``tests/integration/conftest.py``; additive and idempotent — explicit
   resource markers are left untouched). ``e2e`` is a *level* marker,
   orthogonal to the resource axis.
2. **Enforce the resource-marker rule:** every e2e test must declare exactly
   one *primary* bed marker from ``{hostless, integration, embedded}``, with
   ``hops`` permitted only as an additive refinement of ``integration``. All
   other axes (``e2e`` level, ``xdist_group``, ``browser``, ``stability``,
   ``timeout``, ``retry``) are ignored. This keeps the tier deliberately sorted — nothing
   slips into the no-testbed gate untagged. Enforcement is *deferred*:
   collection stamps each violation on the offending item (and re-appends
   offenders that ``-m``/``-k`` filtering removed, so no lane can deselect
   a violation into silence), and the ``pytest_runtest_setup`` hook fails
   that item before any fixture runs — see its docstring for why raising
   at collection time instead is an xdist controller crash.
3. **Stamp the browser-suite xdist grouping policy** (see the block comment
   at ``_BROWSER_SUITE_GROUPS``).

The collection hook is a ``tryfirst`` *wrapper*: its pre-``yield`` half runs
before every non-wrapper impl, so the guard sees every collected item before
``-m`` deselection, the ``e2e`` stamp lands before any marker-based
filtering — and the browser groups land before pytest-xdist's worker plugin
reads them to annotate test ids for the ``loadgroup`` scheduler; its
post-``yield`` half runs after all of them, which is where filtered
offenders are re-appended. That ordering is why the policy lives HERE and
not in the root conftest: the root conftest registers at config load and its
hook therefore runs *after* xdist's (LIFO), where a stamp is silently
invisible to the scheduler (proven empirically: same-file tests landed on
different workers). Deeper conftests register during collection and run
first — same reason the per-device embedded groups are stamped in
``tests/integration/host/conftest.py``.
"""

from pathlib import Path

import pytest

from tests._ambient_env import ambient

_E2E_ROOT = Path(__file__).parent
_PRIMARY = {"hostless", "integration", "embedded"}

# ── Browser-suite xdist grouping policy ────────────────────────────────────
# The two Playwright suites are parallel-safe BY CONSTRUCTION: every test
# binds its MonitorServer to port=0 (tests/_fixtures/_dashboard_harness.py)
# and CDP coverage dumps are keyed pid+uuid (tests/_fixtures/_ts_coverage.py).
# Their single-worker pinning is a resource POLICY — originally "never
# parallel browsers on the 3GB dev VM" (plan 2026-07-02) — not a correctness
# constraint, so it is stamped here instead of hard-coded per module.
# OTTO_BROWSER_SHARD=1 relaxes the pin to per-FILE groups: `--dist loadgroup`
# then spreads modules across workers while any module-scoped fixture still
# instantiates on one worker. CI's dashboard jobs set the env explicitly;
# the Makefile's browser lane sets it whenever the host passes its cores+RAM
# gate (see BROWSER_WORKERS there — the serial pin remains the fallback for
# small hosts and for ad-hoc pytest runs, which leave the env unset).
# Suites not in the map stay serial in both modes — sharding is opt-in per
# suite, after auditing it for parallel safety. An explicit xdist_group mark
# on a test/module always wins (e.g. dashboard/test_harness.py's non-browser
# wire-contract pins keep their historical group).
_BROWSER_SUITE_GROUPS: dict[str, str] = {
    "tests/e2e/monitor/dashboard/": "dashboard",
    "tests/e2e/cov/report_browser/": "covreport",
}


def _browser_group_key(nodeid: str, *, shard: bool) -> str:
    """Return the xdist_group name for a browser-marked item.

    Pure helper — no pytest dependency — so it can be imported and tested
    directly in ``tests/unit/test_browser_group_policy.py``.
    """
    path = nodeid.split("::", 1)[0]
    for prefix, group in _BROWSER_SUITE_GROUPS.items():
        if path.startswith(prefix):
            return f"{group}::{path}" if shard else group
    return "browser-serial"


def _resource_marker_violations(names: "set[str]") -> list[str]:
    """Return the resource-marker rule's violation messages for one item's marker names.

    Pure helper (mirrors ``_browser_group_key``) — tested directly in
    ``tests/unit/test_resource_marker_policy.py``. Messages carry no nodeid:
    they are reported BY the offending item, which pytest already names.
    """
    violations: list[str] = []
    primary = names & _PRIMARY
    if len(primary) != 1:
        violations.append(
            f"resource markers={sorted(primary)} (need exactly one of {sorted(_PRIMARY)})"
        )
    if "hops" in names and "integration" not in names:
        violations.append("'hops' requires 'integration'")
    return violations


# Stamped on an item by pytest_collection_modifyitems, read back by the
# pytest_runtest_setup hook below. Item-scoped state, so it lives in this
# conftest with the two hooks that share it.
_MARKER_VIOLATIONS: "pytest.StashKey[list[str]]" = pytest.StashKey()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    shard = ambient("OTTO_BROWSER_SHARD") == "1"
    offenders = []
    for item in items:
        if _E2E_ROOT not in item.path.parents:
            continue
        # (1) Auto-stamp the e2e level marker (additive, idempotent).
        item.add_marker("e2e")
        # (3) Browser-suite grouping policy (explicit xdist_group pins win).
        if (
            hasattr(item, "get_closest_marker")
            and item.get_closest_marker("browser") is not None
            and item.get_closest_marker("xdist_group") is None
        ):
            item.add_marker(pytest.mark.xdist_group(_browser_group_key(item.nodeid, shard=shard)))
        # (2) Check the resource-marker rule on real collected items — only
        # STAMP violations here; pytest_runtest_setup below reports them.
        # Minimal/synthetic items (e.g. the unit test that exercises only the
        # stamp) may not expose the marker API — stamp them and move on.
        if not hasattr(item, "iter_markers"):
            continue
        violations = _resource_marker_violations({m.name for m in item.iter_markers()})
        if violations:
            item.stash[_MARKER_VIOLATIONS] = violations
            offenders.append(item)
    result = yield
    # After every inner impl has run — including the mark plugin's -m/-k
    # deselection — re-append any offender that filtering removed. Without
    # this, a violation whose markers a lane's -m expression excludes (e.g. a
    # test mistagged hostless+integration on the HOSTLESS lane, the only lane
    # CI runs) is deselected into permanent silence — the mistag would only
    # surface where a lab is up. An offender anywhere in tests/e2e therefore
    # fails every session, whatever the selection; the terminal may count the
    # same nodeid both deselected and errored, which is the honest state.
    # Deterministic (same stamp pass + same append order on every xdist
    # worker), so distributed collection stays consistent.
    for item in offenders:
        if item not in items:
            items.append(item)
    return result


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):  # type: ignore[no-untyped-def]
    """Fail any item the collection hook stamped a resource-marker violation on.

    Enforcement is split across collection and setup because the obvious
    single-site spelling — ``raise pytest.UsageError`` from
    ``pytest_collection_modifyitems`` — crashes the xdist CONTROLLER under
    this repo's default ``-n auto``: any exception from a hook that fires
    after a worker's ``pytest_sessionstart`` (every collection-time hook
    does) dies as ``INTERNALERROR`` / ``AssertionError`` in
    ``xdist/dsession.py``, and the crash blames whichever innocent item the
    dead worker held, not the offender. The full empirical write-up lives
    with the web-dist guard in
    ``tests/e2e/monitor/dashboard/conftest.py``; the reproduction is pinned
    by ``tests/e2e/test_marker_rule_deferral.py``.

    Reporting from THIS hook (``tryfirst``, not an autouse fixture) makes
    the violation the OFFENDER'S own failure, xdist-safe by construction,
    and fires before any fixture setup — so a mistagged test cannot first
    touch a testbed, pay expensive session fixtures, or have its message
    swallowed by an unrelated higher-scoped fixture failure. The trade,
    made deliberately: ``--collect-only`` no longer aborts — a run
    precondition must not fire when nothing runs (the #196 doctrine) —
    while deselection cannot hide an offender (the collection wrapper
    re-appends filtered offenders; see above). Neither ``skip`` nor
    ``xfail`` marks can hide it either: this hook registers after
    ``_pytest.skipping``'s tryfirst hook and therefore runs FIRST, failing
    the item before skip marks are evaluated or ``xfailed_key`` is stashed
    (verified empirically at landing).
    """
    violations = item.stash.get(_MARKER_VIOLATIONS, None)
    if violations:
        pytest.fail(
            "tests/e2e resource-marker rule violated:\n  " + "\n  ".join(violations),
            pytrace=False,
        )
