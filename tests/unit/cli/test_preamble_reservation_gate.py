"""Unit tests for the reservation-gate CLI adapter.

``ReservationGate.evaluate()`` itself (the outcome matrix) is tested in
``tests/unit/reservations/test_gate.py``. These tests cover the CLI-adapter
wiring: reading ``ctx.meta["otto_reservation"]``, calling ``.evaluate()``,
and — when the outcome carries a warning — presenting it wrapped in
``[bold red]...[/bold red]`` markup.

That presentation now lives in exactly one place,
:func:`otto.cli.invoke.present_reservation_gate`, called by both
``command_preamble`` (tested here indirectly, through the existing
``test_gate_*`` cases below) and ``otto monitor``'s live branch
(``tests/unit/cli/test_monitor.py::TestGatePerBranch``). The
``TestPresentReservationGate*`` classes below test the helper directly.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from otto.cli import invoke
from otto.cli.registry import CommandSpec
from otto.reservations import MissingReservationError, ReservationGateResult


class _FakeCtx:
    """Minimal stand-in for a click leaf context the preamble inspects."""

    def __init__(self, spec: CommandSpec, reservation: object | None = None) -> None:
        callback = SimpleNamespace(__cli_output_dir__=True)
        self.command = SimpleNamespace(name=spec.name, callback=callback)
        self.meta: dict[str, Any] = {
            "_otto_command_spec": spec,
            "_otto_root_options": object(),
        }
        if reservation is not None:
            self.meta["otto_reservation"] = reservation


@pytest.fixture
def preamble_gate(monkeypatch):
    """Isolate ``command_preamble`` down to its reservation-gate branch.

    Stubs bootstrap (no errors), session/lab setup, and output-dir creation so
    the only observable effect is whatever the reservation-gate block does.
    """

    def _noop(ctx: Any) -> None:
        pass

    monkeypatch.setattr(invoke, "ensure_cli_session", _noop)
    monkeypatch.setattr(invoke, "ensure_lab_context", _noop)
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: SimpleNamespace(errors=[], repos=[]))
    monkeypatch.setattr("otto.logger.management.create_output_dir", lambda *a, **k: None)
    monkeypatch.setattr("otto.context.get_context", lambda: SimpleNamespace(output_dir=None))


def test_gate_noop_when_no_reservation_in_meta(preamble_gate, capsys):
    """gate=True but ensure_lab_context never populated otto_reservation (e.g. a stubbed no-op in tests) -> no crash, nothing printed."""  # noqa: E501 — descriptive docstring
    spec = CommandSpec(name="run", loader=None, gate=True)
    invoke.command_preamble(_FakeCtx(spec))  # ty: ignore[invalid-argument-type]
    assert capsys.readouterr().out == ""


def test_gate_false_skips_reservation_lookup_entirely(preamble_gate, capsys):
    spec = CommandSpec(name="run", loader=None, gate=False)
    mock_gate = MagicMock()
    invoke.command_preamble(_FakeCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
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
    invoke.command_preamble(_FakeCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
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
    invoke.command_preamble(_FakeCtx(spec, reservation=mock_gate))  # ty: ignore[invalid-argument-type]
    mock_gate.evaluate.assert_called_once()
    assert capsys.readouterr().out == ""


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


class TestPresentReservationGateMarkup:
    def test_warning_is_wrapped_in_bold_red_markup(self):
        warning = "some plain-text warning, no markup of its own"
        mock_gate = MagicMock()
        mock_gate.evaluate.return_value = ReservationGateResult(
            checked=False, skipped=True, warning=warning
        )
        ctx = SimpleNamespace(meta={"otto_reservation": mock_gate})

        with patch("rich.print") as mock_print:
            invoke.present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]

        # Pins the EXACT composition — f"[bold red]{warning}[/bold red]" — so
        # dropping (or altering) the markup fails this test even though a
        # capsys-based assertion could not tell the difference.
        mock_print.assert_called_once_with(f"[bold red]{warning}[/bold red]")

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
