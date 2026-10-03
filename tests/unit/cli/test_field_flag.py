"""``--field/--debug`` and ``OTTO_FIELD_PRODUCTS`` set the run's variant; nothing else does."""

import re

import pytest
from typer.testing import CliRunner

from otto import context
from otto.cli.main import app

runner = CliRunner()


@pytest.fixture
def seen_variant(monkeypatch):
    """Record ``variant()`` as the listing runs — after the root callback, before any lab."""
    seen: list[str] = []
    monkeypatch.setattr(
        "otto.cli.listing.show_listing", lambda ctx, seam: seen.append(context.variant())
    )
    return seen


def _invoke(*args, env=None):
    base = {"OTTO_LAB": "", "OTTO_FIELD_PRODUCTS": "", "OTTO_FIELD_DEFAULT": ""}
    return runner.invoke(app, [*args, "--list-products"], env={**base, **(env or {})})


def test_the_default_is_debug(seen_variant):
    assert _invoke().exit_code == 0
    assert seen_variant == ["debug"]


def test_the_field_flag_selects_field(seen_variant):
    assert _invoke("--field").exit_code == 0
    assert seen_variant == ["field"]


def test_the_debug_flag_selects_debug(seen_variant):
    assert _invoke("--debug").exit_code == 0
    assert seen_variant == ["debug"]


def test_the_env_var_selects_field(seen_variant):
    assert _invoke(env={"OTTO_FIELD_PRODUCTS": "1"}).exit_code == 0
    assert seen_variant == ["field"]


def test_the_env_var_false_is_debug(seen_variant):
    assert _invoke(env={"OTTO_FIELD_PRODUCTS": "false"}).exit_code == 0
    assert seen_variant == ["debug"]


def test_the_flag_beats_the_env_var(seen_variant):
    assert _invoke("--debug", env={"OTTO_FIELD_PRODUCTS": "1"}).exit_code == 0
    assert seen_variant == ["debug"]


def test_otto_field_default_is_not_a_switch(seen_variant):
    # Deleted outright: set to anything, it changes nothing.
    assert _invoke(env={"OTTO_FIELD_DEFAULT": "1"}).exit_code == 0
    assert seen_variant == ["debug"]


def test_the_root_options_carry_the_field_bit(monkeypatch):
    from otto.cli.invoke import root_options

    seen: list[bool] = []
    monkeypatch.setattr(
        "otto.cli.listing.show_listing", lambda ctx, seam: seen.append(root_options(ctx).field)
    )
    assert _invoke("--field").exit_code == 0
    assert _invoke().exit_code == 0
    assert seen == [True, False]


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_the_help_shows_the_products_table_name_unmangled():
    """Rich markup would eat an unescaped ``[[products]]`` and print ``a [] entry``."""
    result = runner.invoke(app, ["--help"], env={"OTTO_LAB": ""})
    assert result.exit_code == 0
    plain = _ANSI.sub("", result.output)
    for chrome in "│╭╮╰╯─":
        plain = plain.replace(chrome, " ")
    assert "[[products]]" in " ".join(plain.split())
