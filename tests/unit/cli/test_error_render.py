"""One renderer for user-facing errors, and it escapes rich markup.

A test asserting on `str(exc)` cannot see this class of bug: the exception is
perfect, and the damage happens in the renderer. So everything here asserts on
CAPTURED OUTPUT through the real print path.
"""

from types import SimpleNamespace

import pytest
import typer

from otto.cli.invoke import fail, print_error, render_instrumentation_refusal

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


# ---------------------------------------------------------------------------
# The coverage refusal: one user-facing line, plus the verdict TABLE
# ---------------------------------------------------------------------------
#
# Same rule as everything above: asserted on CAPTURED OUTPUT through the real
# print path. `InstrumentationReport.table()` returning a well-formed Table
# proves nothing about what the reader sees -- the remedy caption ends in
# "the [[products]] entry", and an unescaped render turns that into
# "the [] entry", which is a live bug this file's whole premise is about.


def _refusal(*verdicts, command="otto test --cov"):
    """Raise and return the real refusal `decide_coverage` produces for *verdicts*.

    Built through `decide_coverage` rather than by constructing the error
    directly, so the `report=` hand-off this rendering depends on is part of
    what every test below exercises.
    """
    from otto.coverage.errors import CoverageNotInstrumentedError
    from otto.coverage.instrumentation import decide_coverage, detect

    hosts = [
        SimpleNamespace(
            id=f"h{i}", products=[SimpleNamespace(name=f"p{i}", instrumented=lambda v=v: v)]
        )
        for i, v in enumerate(verdicts)
    ]
    try:
        decide_coverage(True, detect(hosts), has_cov_config=True, command=command)
    except CoverageNotInstrumentedError as e:
        return e
    raise AssertionError("decide_coverage did not refuse")  # pragma: no cover — guard


def test_refusal_prints_the_table_and_returns_only_the_headline(capsys) -> None:
    """The console gets ONE line plus the table; the listing is not repeated."""
    error = _refusal(False, None)
    headline = render_instrumentation_refusal(error)

    assert headline == "otto test --cov: no instrumented product — coverage cannot be collected."
    assert "\n" not in headline
    out = capsys.readouterr().out
    assert "coverage instrumentation" in out  # the title
    for cell in ("host", "product", "instrumented", "h0", "p0", "no", "h1", "p1", "unknown"):
        assert cell in out
    # The plain `describe()` listing stays in the MESSAGE (the run log, a
    # library caller) but must not also reach the console under the table.
    assert "h0: p0" in str(error)
    assert "h0: p0" not in out


def test_refusal_caption_keeps_the_products_brackets(capsys) -> None:
    """The remedy names `[[products]]`; rich must not eat it down to `[]`.

    This is the bug the table had the moment anything rendered it: the caption
    is the one place the fix is actionable, and "the [] entry" points the
    reader at nothing.
    """
    render_instrumentation_refusal(_refusal(None))
    out = capsys.readouterr().out
    assert "[[products]]" in out
    assert "the [] entry" not in out


def test_refusal_caption_is_absent_when_no_verdict_is_unknown(capsys) -> None:
    """The remedy is for `unknown`; a scanned-and-negative lab has nothing to override."""
    render_instrumentation_refusal(_refusal(False))
    out = capsys.readouterr().out
    assert "coverage instrumentation" in out
    assert "Product.instrumented" not in out


def test_refusal_with_no_products_at_all_prints_no_table(capsys) -> None:
    """An empty report has no rows to tabulate, so the whole message is the answer."""
    error = _refusal(command="otto cov get")
    headline = render_instrumentation_refusal(error)
    assert headline == str(error)
    assert "no products on any coverage host" in headline
    assert capsys.readouterr().out == ""


def test_any_other_error_is_returned_whole_and_prints_nothing(capsys) -> None:
    """The helper is usable unconditionally in an error path."""
    error = ValueError("expected list[str], got dict[str, int]")
    assert render_instrumentation_refusal(error) == str(error)
    assert capsys.readouterr().out == ""


def test_the_boundary_composition_is_one_error_line_over_the_table(capsys) -> None:
    """What `otto.cli.main.entry` actually prints: table first, then the line.

    Pins the two halves TOGETHER, because the failure mode is a caller that
    renders the table and then prints the whole multi-line message under it.
    """
    error = _refusal(False, None)
    print_error(f"error: {render_instrumentation_refusal(error)}")
    out = capsys.readouterr().out
    assert out.index("coverage instrumentation") < out.index("error: otto test --cov")
    assert out.count("coverage cannot be collected") == 1
