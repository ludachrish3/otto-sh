"""One renderer for user-facing errors, and it escapes rich markup.

A test asserting on `str(exc)` cannot see this class of bug: the exception is
perfect, and the damage happens in the renderer. So everything here asserts on
CAPTURED OUTPUT through the real print path.
"""

import pytest
import typer

from otto.cli.invoke import fail, print_error

#: Bracket shapes that reach a user-facing message in practice. Rich reads
#: `[word]` as a style tag and deletes it; numeric subscripts survive, which
#: is exactly why "it looked fine when I tried it" is not evidence.
_EATEN_WITHOUT_ESCAPING = [
    "pip install 'otto-sh[monitor]'",
    "expected list[str], got dict[str, int]",
    "1 validation error [type=missing, input_value={}, input_type=dict]",
    "host[eth0] is unreachable",
    # Longer than rich's captured-terminal default of 80 columns, so this one
    # also proves the width pin above is doing its job.
    (
        "--on 'dut9' is not a host in the active lab 'unix'. Available hosts: "
        "['test1', 'local', 'test3', 'test2'] list[str]"
    ),
]


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the width so a wrapped line cannot masquerade as an eaten one.

    Under capture rich falls back to 80 columns and hard-wraps, so a message
    longer than that fails `message in out` for a WRAPPING reason — a false
    red that would send the next reader hunting an escaping bug.
    """
    monkeypatch.setenv("COLUMNS", "300")


@pytest.mark.parametrize("message", _EATEN_WITHOUT_ESCAPING)
def test_print_error_keeps_every_bracket(capsys, message: str) -> None:
    """The whole message survives, brackets included.

    `pip install 'otto-sh[monitor]'` rendering as `pip install 'otto-sh'` is
    the worst shape: not garbled, but a plausible, runnable, WRONG command.
    """
    print_error(message)
    assert message in capsys.readouterr().out


def test_print_error_renders_an_exception_not_just_a_string(capsys) -> None:
    """Callers pass the exception itself; `str()` happens inside."""
    print_error(ValueError("no link 'a[0]--b' (known: dict[str, int])"))
    out = capsys.readouterr().out
    assert "a[0]--b" in out
    assert "dict[str, int]" in out


def test_fail_prints_then_exits_with_the_given_code(capsys) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        fail("expected list[str]", 2)
    assert excinfo.value.exit_code == 2
    assert "expected list[str]" in capsys.readouterr().out


def test_fail_defaults_to_exit_one(capsys) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        fail("boom")
    assert excinfo.value.exit_code == 1
    assert "boom" in capsys.readouterr().out


def test_unescaped_rendering_really_does_eat_them() -> None:
    """The negative control, so the tests above are not merely tautological.

    Without `escape()` these messages come out changed — silently, and in the
    plausible direction. If rich ever stops doing this, this test fails and
    the escaping can be reconsidered rather than cargo-culted.
    """
    import io

    from rich.console import Console

    console = Console(file=io.StringIO(), force_terminal=False, no_color=True, width=200)
    for message in _EATEN_WITHOUT_ESCAPING:
        console.print(f"[red]{message}[/red]")
    rendered = console.file.getvalue()
    for message in _EATEN_WITHOUT_ESCAPING:
        assert message not in rendered, f"rich no longer eats {message!r}"
