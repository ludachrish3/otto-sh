"""Truth table for the e2e resource-marker rule's pure decision helper.

``_resource_marker_violations`` (tests/e2e/conftest.py) decides which marker
sets violate the exactly-one-primary rule; the collection hook stamps its
output on offending items and the tryfirst ``pytest_runtest_setup`` hook
fails them at setup. The
deferral machinery is pinned end-to-end by
``tests/e2e/test_marker_rule_deferral.py``; this table pins the decision
itself, the same split as ``_browser_group_key`` /
``tests/unit/test_browser_group_policy.py``.
"""

import pytest

from tests.e2e.conftest import _resource_marker_violations

pytestmark = pytest.mark.hostless


@pytest.mark.parametrize(
    "names",
    [
        {"hostless"},
        {"integration"},
        {"embedded"},
        {"integration", "hops"},
        # Non-primary axes never count toward the primary requirement.
        {"hostless", "e2e", "browser", "xdist_group", "timeout"},
    ],
    ids=lambda names: "+".join(sorted(names)),
)
def test_compliant_marker_sets_have_no_violations(names: set) -> None:
    assert _resource_marker_violations(names) == []


def test_no_primary_marker_is_a_violation() -> None:
    [violation] = _resource_marker_violations({"e2e", "timeout"})
    assert "need exactly one of" in violation
    assert "embedded" in violation
    assert "hostless" in violation
    assert "integration" in violation


def test_two_primary_markers_are_a_violation() -> None:
    [violation] = _resource_marker_violations({"hostless", "integration"})
    assert "need exactly one of" in violation
    # The offending pair is named so the fix is obvious from the message.
    assert "'hostless'" in violation
    assert "'integration'" in violation


def test_hops_without_integration_is_a_violation() -> None:
    [violation] = _resource_marker_violations({"hostless", "hops"})
    assert violation == "'hops' requires 'integration'"


def test_mistagged_item_can_violate_both_clauses_at_once() -> None:
    violations = _resource_marker_violations({"hops"})
    assert len(violations) == 2
    assert any("need exactly one of" in v for v in violations)
    assert "'hops' requires 'integration'" in violations
