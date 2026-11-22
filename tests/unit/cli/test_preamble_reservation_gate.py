"""Unit tests for the reservation-gate CLI adapter.

``ReservationGate.evaluate()`` itself (the outcome matrix) is tested in
``tests/unit/reservations/test_gate.py``. These tests cover the CLI-adapter
wiring: reading ``ctx.meta["otto_reservation"]``, calling ``.evaluate()``,
and — when the outcome carries a warning — presenting it wrapped in
``[bold red]...[/bold red]`` markup.

That presentation now lives in exactly one place,
:func:`otto.cli.invoke.present_reservation_gate`, called by both
``command_preamble`` (tested here indirectly, through the existing
``test_gate_*`` cases below) and ``otto monitor``'s live branch, both through
:func:`otto.cli.invoke.open_live_lab` (``tests/unit/cli/test_monitor.py::TestGatePerMode``). The
``TestPresentReservationGate*`` classes below test the helper directly.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from otto.cli import invoke
from otto.cli.registry import CommandSpec
from otto.reservations import MissingReservationError, ReservationGateResult
from tests._fixtures.preamble_ctx import FakeLeafCtx


def test_gate_noop_when_no_reservation_in_meta(preamble_gate, capsys):
    """gate=True but ensure_lab_context never populated otto_reservation (e.g. a stubbed no-op in tests) -> no crash, nothing printed."""  # noqa: E501 — descriptive docstring
    spec = CommandSpec(name="run", loader=None, gate=True)
    invoke.command_preamble(FakeLeafCtx(spec))  # ty: ignore[invalid-argument-type]
    assert capsys.readouterr().out == ""


def test_gate_false_skips_reservation_lookup_entirely(preamble_gate, capsys):
    spec = CommandSpec(name="run", loader=None, gate=False)
    mock_gate = MagicMock()
    invoke.command_preamble(FakeLeafCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
    mock_gate.evaluate.assert_not_called()


def test_gate_evaluates_and_prints_warning(preamble_gate, capsys):
    spec = CommandSpec(name="run", loader=None, gate=True)
    warning = (
        "\N{WARNING SIGN}  Reservation check SKIPPED for user 'alice' "
        "on lab 'test_lab'. Required resources: []"
    )
    mock_gate = MagicMock()
    mock_gate.evaluate.return_value = ReservationGateResult(
        checked=False, skipped=True, warning=warning
    )
    invoke.command_preamble(FakeLeafCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
    mock_gate.evaluate.assert_called_once()
    out = capsys.readouterr().out
    # rich strips the [bold red] markup itself when rendering to a non-tty
    # capture (confirmed separately) and may word-wrap at the console width —
    # compare with whitespace normalized so a wrap point doesn't fail the assertion.
    assert " ".join(warning.split()) in " ".join(out.split())


def test_gate_checked_true_prints_nothing(preamble_gate, capsys):
    spec = CommandSpec(name="run", loader=None, gate=True)
    mock_gate = MagicMock()
    mock_gate.evaluate.return_value = ReservationGateResult(
        checked=True, skipped=False, warning=None
    )
    invoke.command_preamble(FakeLeafCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
    mock_gate.evaluate.assert_called_once()
    assert capsys.readouterr().out == ""


def test_an_unsatisfied_dependency_refuses_before_the_gate(preamble_gate, monkeypatch):
    """The same input ``otto monitor --live`` is refused on (tests/unit/cli/test_monitor.py)."""
    import typer

    def refuse() -> None:
        raise typer.Exit(1)

    monkeypatch.setattr(invoke, "refuse_unsatisfied_dependencies", refuse)
    spec = CommandSpec(name="run", loader=None, gate=True)
    mock_gate = MagicMock()
    with pytest.raises(typer.Exit) as excinfo:
        invoke.command_preamble(FakeLeafCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
    assert excinfo.value.exit_code == 1
    mock_gate.evaluate.assert_not_called()


# ── present_reservation_gate: markup pinning + exception propagation ────────
#
# The tests above assert on ``capsys``-captured output, whitespace-normalized
# — but rich strips markup and word-wraps by the time it reaches a captured
# (non-tty) stream, so those assertions would pass identically whether or not
# the ``[bold red]...[/bold red]`` wrapping was ever applied. They cannot
# catch someone silently dropping the markup. These tests patch ``rich.print``
# directly (the same target ``present_reservation_gate``'s local
# ``from rich import print as rprint`` resolves against at call time) and
# assert on the literal, unrendered argument instead.


class TestPresentReservationGateStyle:
    def test_warning_is_a_literal_bold_red_text(self):
        warning = "some plain-text warning, no markup of its own"
        mock_gate = MagicMock()
        mock_gate.evaluate.return_value = ReservationGateResult(
            checked=False, skipped=True, warning=warning
        )
        ctx = SimpleNamespace(meta={"otto_reservation": mock_gate})

        with patch("rich.print") as mock_print:
            invoke.present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]

        # Pins the EXACT composition (the warning as one literal Text span,
        # styled bold red) so dropping or altering the style fails this test
        # even though a capsys-based assertion could not tell the difference.
        mock_print.assert_called_once()
        (text,) = mock_print.call_args.args
        assert text.plain == warning
        assert [(span.start, span.end, span.style) for span in text.spans] == [
            (0, len(warning), "bold red")
        ]

    def test_no_warning_means_rich_print_never_called(self):
        mock_gate = MagicMock()
        mock_gate.evaluate.return_value = ReservationGateResult(
            checked=True, skipped=False, warning=None
        )
        ctx = SimpleNamespace(meta={"otto_reservation": mock_gate})

        with patch("rich.print") as mock_print:
            invoke.present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]

        mock_print.assert_not_called()


class TestPresentReservationGatePropagation:
    def test_missing_reservation_error_propagates_uncaught(self):
        mock_gate = MagicMock()
        mock_gate.evaluate.side_effect = MissingReservationError("resource held by bob")
        ctx = SimpleNamespace(meta={"otto_reservation": mock_gate})

        with pytest.raises(MissingReservationError, match="resource held by bob"):
            invoke.present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]

    def test_noop_when_no_reservation_in_meta(self):
        ctx = SimpleNamespace(meta={})
        invoke.present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]  # must not raise
