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
   slips into the no-testbed gate untagged.
3. **Stamp the browser-suite xdist grouping policy** (see the block comment
   at ``_BROWSER_SUITE_GROUPS``).

Runs ``tryfirst`` so the guard sees every collected item before ``-m``
deselection, the ``e2e`` stamp lands before any marker-based filtering — and
the browser groups land before pytest-xdist's worker plugin reads them to
annotate test ids for the ``loadgroup`` scheduler. That ordering is why the
policy lives HERE and not in the root conftest: the root conftest registers
at config load and its hook therefore runs *after* xdist's (LIFO), where a
stamp is silently invisible to the scheduler (proven empirically: same-file
tests landed on different workers). Deeper conftests register during
collection and run first — same reason the per-device embedded groups are
stamped in ``tests/integration/host/conftest.py``.
"""

import os
from pathlib import Path

import pytest

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


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    shard = os.environ.get("OTTO_BROWSER_SHARD") == "1"
    offenders: list[str] = []
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
        # (2) Enforce exactly one primary resource marker on real collected items.
        # Minimal/synthetic items (e.g. the unit test that exercises only the
        # stamp) may not expose the marker API — stamp them and move on.
        if not hasattr(item, "iter_markers"):
            continue
        names = {m.name for m in item.iter_markers()}
        primary = names & _PRIMARY
        if len(primary) != 1:
            offenders.append(
                f"{item.nodeid}: resource markers={sorted(primary)} "
                f"(need exactly one of {sorted(_PRIMARY)})"
            )
        if "hops" in names and "integration" not in names:
            offenders.append(f"{item.nodeid}: 'hops' requires 'integration'")
    if offenders:
        raise pytest.UsageError(
            "tests/e2e resource-marker rule violated:\n  " + "\n  ".join(offenders)
        )
