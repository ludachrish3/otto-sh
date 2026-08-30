"""``otto.suite.markers``: the ensure path grammar (spec §4.1) and the built-in marker table."""

import pytest

from otto.suite.markers import (
    ENSURE_NONE,
    ENSURE_VERBS,
    OTTO_MARKERS,
    ensure_path,
    ensure_path_problem,
)


def test_vocabulary_is_the_three_converge_verbs() -> None:
    assert ENSURE_VERBS == {
        "installed": "ensure_installed",
        "uninstalled": "ensure_uninstalled",
        "clean": "ensure_clean",
    }
    assert ENSURE_NONE == "none"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("installed",), ["installed"]),
        (("clean", "installed"), ["clean", "installed"]),
        (("none",), []),
    ],
)
def test_ensure_path_keeps_order_and_maps_none_to_nothing(args, expected) -> None:
    assert ensure_path(args) == expected


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ((), "at least one step"),
        (("bogus",), "unknown step 'bogus'"),
        (("none", "installed"), "'none' is a complete path"),
        ((42,), "unknown step '42'"),
    ],
)
def test_ensure_path_problem_names_the_defect(args, fragment) -> None:
    problem = ensure_path_problem(args)
    assert problem is not None
    assert fragment in problem
    assert "installed, uninstalled, clean, none" in problem


def test_valid_paths_have_no_problem() -> None:
    assert ensure_path_problem(("clean", "installed")) is None
    assert ensure_path_problem(("none",)) is None


def test_builtin_marker_table_registers_ensure_and_retry() -> None:
    assert set(OTTO_MARKERS) == {"ensure", "retry"}
    assert OTTO_MARKERS["ensure"].startswith("ensure(*steps):")
    assert OTTO_MARKERS["retry"].startswith("retry(n):")
