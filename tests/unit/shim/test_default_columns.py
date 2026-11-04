"""The shim's ``COLUMNS`` default: the policy, and the wrapper that reads the descriptors (#785).

``_default_columns`` is the pure rule (spec D1, steps 1-5); ``_apply_columns_default``
is the one place that probes fds 0, 1 and 2 and writes the answer into the
environment. The root conftest's ``COLUMNS`` guard puts the variable back after
every test, so a wrapper test leaks nothing.
"""

import os

import pytest

from otto._shim import NON_TTY_COLUMNS, _apply_columns_default, _default_columns
from tests._fixtures.terminals import fake_terminals

_STDOUT_TTY = (120, None)
_OTHER_TTY_ONLY = (None, 100)
_NO_TTY = (None, None)
_ZERO_STDOUT_100_STDERR = (None, 100)
"""A 0x0 stdout is not a usable terminal, so it reaches the policy as no stdout terminal."""

_UNUSABLE = ["0", "²", "\uff11\uff12\uff10", " 120"]  # the third is full-width 120

_CASES = [
    # COLUMNS absent
    (None, _STDOUT_TTY, None),
    (None, _OTHER_TTY_ONLY, "100"),
    (None, _NO_TTY, "200"),
    (None, _ZERO_STDOUT_100_STDERR, "100"),
    # a usable COLUMNS is retained, whatever the descriptors
    ("150", _STDOUT_TTY, "150"),
    ("150", _OTHER_TTY_ONLY, "150"),
    ("150", _NO_TTY, "150"),
    ("150", _ZERO_STDOUT_100_STDERR, "150"),
] + [
    # an unusable one is dropped on every configuration, then the descriptor rules apply
    row
    for value in _UNUSABLE
    for row in (
        (value, _STDOUT_TTY, None),
        (value, _OTHER_TTY_ONLY, "100"),
        (value, _NO_TTY, "200"),
        (value, _ZERO_STDOUT_100_STDERR, "100"),
    )
]


@pytest.mark.parametrize(("columns", "descriptors", "expected"), _CASES)
def test_the_columns_policy(columns, descriptors, expected) -> None:
    environ = {} if columns is None else {"COLUMNS": columns}
    stdout_columns, other_columns = descriptors
    assert _default_columns(environ, stdout_columns, other_columns) == expected


def test_the_no_terminal_width_is_200() -> None:
    assert NON_TTY_COLUMNS == 200


@pytest.mark.parametrize(
    ("columns", "sizes", "expected"),
    [
        (None, {1: None, 2: (100, 30)}, "100"),
        (None, {}, "200"),
        (None, {1: (120, 30)}, None),
        ("0", {1: (120, 30)}, None),
    ],
    ids=["zero-stdout-100-stderr", "no-terminal", "stdout-tty", "unusable-dropped-on-a-tty"],
)
def test_the_wrapper_sets_or_pops_columns(monkeypatch, columns, sizes, expected) -> None:
    if columns is None:
        monkeypatch.delenv("COLUMNS", raising=False)
    else:
        monkeypatch.setenv("COLUMNS", columns)
    fake_terminals(monkeypatch, sizes)
    _apply_columns_default()
    assert os.environ.get("COLUMNS") == expected
