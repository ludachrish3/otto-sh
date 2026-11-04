"""``otto._terminal``: the one "usable terminal" decision and the interactive size (#785)."""

import os

import pytest

from otto._terminal import (
    first_usable_terminal_size,
    interactive_terminal_size,
    usable_columns,
    usable_terminal_size,
)
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.terminals import fake_terminals

SRC_HOST = CHECKOUT_ROOT / "src" / "otto" / "host"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120),
        ("1", 1),
        ("0", None),
        ("-5", None),
        ("²", None),
        ("\uff11\uff12\uff10", None),  # full-width 120
        (" 120", None),
        ("120 ", None),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_usable_columns_is_ascii_digits_at_least_one(value, expected) -> None:
    assert usable_columns(value) == expected


@pytest.mark.parametrize(
    ("sizes", "expected"),
    [
        ({1: (100, 30)}, (100, 30)),
        ({1: None}, None),
        ({}, None),
        ({1: OSError(5, "Input/output error")}, None),
    ],
    ids=["tty-100x30", "tty-0x0", "not-a-tty", "size-raises"],
)
def test_usable_terminal_size(monkeypatch, sizes, expected) -> None:
    fake_terminals(monkeypatch, sizes)
    assert usable_terminal_size(1) == (os.terminal_size(expected) if expected else None)


def test_the_first_usable_descriptor_wins_in_the_order_given(monkeypatch) -> None:
    fake_terminals(monkeypatch, {0: (100, 30), 2: (90, 20)})
    assert first_usable_terminal_size((2, 0)) == (90, 20)
    assert first_usable_terminal_size((1,)) is None


def test_the_interactive_size_ignores_columns_and_lines(monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("LINES", "99")
    fake_terminals(monkeypatch, {0: (100, 30)})
    assert interactive_terminal_size() == (100, 30)


def test_the_interactive_size_falls_back_to_stderr(monkeypatch) -> None:
    fake_terminals(monkeypatch, {2: (90, 20)})
    assert interactive_terminal_size() == (90, 20)


def test_an_unreadable_terminal_asks_for_the_fallback(monkeypatch) -> None:
    fake_terminals(monkeypatch, {0: OSError(5, "Input/output error")})
    fallback = os.terminal_size((400, 24))
    assert interactive_terminal_size(fallback=fallback) == (400, 24)


def test_no_terminal_gives_80_by_24(monkeypatch) -> None:
    fake_terminals(monkeypatch, {})
    assert interactive_terminal_size() == (80, 24)


def test_no_host_module_sizes_from_the_environment() -> None:
    """Every host-side terminal size goes through ``interactive_terminal_size``.

    ``shutil.get_terminal_size`` and ``os.get_terminal_size`` both read
    ``COLUMNS`` (the first) or a single descriptor (the second); a host module
    calling either would bring back the frozen-remote-pty bug.
    """
    offenders = [
        str(path.relative_to(SRC_HOST))
        for path in sorted(SRC_HOST.rglob("*.py"))
        if "get_terminal_size(" in path.read_text()
    ]
    assert offenders == []
