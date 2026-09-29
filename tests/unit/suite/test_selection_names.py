"""Name forms (spec §3; Review Focus 5)."""

import pytest

from otto.suite.selection import matches_name


@pytest.mark.parametrize(
    ("wanted", "classes", "name", "expected"),
    [
        ("test_x", [], "test_x", True),
        ("test_x", ["TestA"], "test_x[p1]", True),
        ("TestA", ["TestA"], "test_x", True),
        ("TestA::test_x", ["TestA"], "test_x", True),
        ("TestA::test_y", ["TestA"], "test_x", False),
        ("TestInner", ["TestOuter", "TestInner"], "test_x", True),
        ("TestOuter", ["TestOuter", "TestInner"], "test_x", True),
        ("TestOuter::TestInner::test_x", ["TestOuter", "TestInner"], "test_x", True),
        ("TestInner::test_x", ["TestOuter", "TestInner"], "test_x", True),
        ("TestOuter::test_x", ["TestOuter", "TestInner"], "test_x", False),
        ("test_x", ["TestA"], "test_xy", False),
        # One name that is both a class and a test selects both.
        ("check", ["check"], "test_1", True),
        ("check", [], "check[p]", True),
    ],
)
def test_name_forms(wanted, classes, name, expected):
    assert matches_name(wanted, classes, name) is expected
