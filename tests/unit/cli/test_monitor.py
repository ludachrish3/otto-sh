"""
Unit tests for the ``otto monitor`` leaf: the rules that are about the
command's own shape, and its per-mode preamble.

What the leaf hands the library and how it reports the library's refusals is
pinned by ``test_monitor_differential.py``; the behaviour behind those calls
(selection, scope, TLS, review sources, the live run) is the monitor
library's and is tested under ``tests/unit/monitor/``. Every test here goes
through :class:`~tests._fixtures.dispatch.DispatchRunner` with the library
calls faked, so the leaf runs under the same dispatch seam as on the real CLI.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.cli.monitor import monitor_app
from otto.monitor.live import LiveReport
from otto.reservations import ReservationGateResult
from tests._fixtures.dispatch import DispatchRunner
from tests.unit.cli.conftest import _flat

_NOW = datetime.now(tz=timezone.utc)


def _report(db=None) -> LiveReport:
    return LiveReport(session_id="sess-1", hosts=["h"], db=db, start=_NOW, end=_NOW)


def _invoke(argv, **kwargs):
    return DispatchRunner().invoke(monitor_app, argv, spec_name="monitor", **kwargs)


@pytest.fixture(autouse=True)
def preamble(monkeypatch):
    """Stub the preamble's session and lab slices, recording which ones the leaf calls."""
    calls = []
    monkeypatch.setattr(
        "otto.cli.invoke.ensure_lab_session", lambda *a, **k: calls.append("lab_session")
    )
    monkeypatch.setattr(
        "otto.cli.invoke.ensure_cli_session", lambda *a, **k: calls.append("cli_session")
    )
    monkeypatch.setattr("otto.config.get_repos", list)
    return calls


@pytest.fixture
def run_live():
    fake = AsyncMock(return_value=_report())
    with patch("otto.monitor.live.run_live", fake):
        yield fake


@pytest.fixture
def serve_review():
    fake = AsyncMock(return_value=None)
    with patch("otto.monitor.review.serve_review", fake):
        yield fake


# ── Help ──────────────────────────────────────────────────────────────────────


class TestMonitorHelp:
    def test_help_flag(self):
        result = _invoke(["--help"])
        assert result.exit_code == 0

    def test_help_mentions_interval(self):
        result = _invoke(["--help"])
        assert "--interval" in result.output

    def test_help_states_the_interval_floor(self):
        """The floor is the library's rule, but the help still tells the user what it is."""
        result = _invoke(["--help"], env={"COLUMNS": "300"})
        assert "(at least 1.0s)" in _flat(result.output)

    def test_help_states_fullmatch_semantics(self):
        """The rendered help says how ``--hosts`` matches — asserted on the OUTPUT.

        Grepping the source would pass on a help string typer never renders
        (an option in a table too narrow for it, a leaf whose help is
        overridden). ``COLUMNS`` is widened so a truncated cell cannot read as
        a missing phrase.
        """
        result = _invoke(["--help"], env={"COLUMNS": "300"})
        assert "(fullmatch)" in _flat(result.output)


# ── The command's shape: one mode per invocation ─────────────────────────────


class TestOneModePerInvocation:
    def test_live_and_a_source_together_is_a_usage_error(self, tmp_path, run_live, serve_review):
        src = tmp_path / "m.json"
        src.write_text("{}")
        result = _invoke(["--live", str(src)])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output
        run_live.assert_not_awaited()
        serve_review.assert_not_awaited()

    def test_neither_mode_prints_usage(self, run_live, serve_review):
        result = _invoke([])
        assert result.exit_code == 2
        assert "--live" in result.output
        run_live.assert_not_awaited()
        serve_review.assert_not_awaited()


# ── The preamble: one slice per mode ─────────────────────────────────────────


class TestPreamblePerMode:
    """Review reads a local file, so it gets the CLI session only; --live loads the lab."""

    def test_review_runs_only_the_cli_session_slice(self, tmp_path, preamble, serve_review):
        src = tmp_path / "m.json"
        src.write_text("{}")
        result = _invoke([str(src)])
        assert result.exit_code == 0, result.output
        assert preamble == ["cli_session"]

    def test_live_runs_the_lab_session_slice(self, preamble, run_live):
        result = _invoke(["--live"])
        assert result.exit_code == 0, result.output
        assert preamble == ["lab_session"]


# ── Parsing that reaches the library ─────────────────────────────────────────


class TestIntervalOption:
    def test_short_flag(self, run_live):
        result = _invoke(["--live", "-i", "3"])
        assert result.exit_code == 0, result.output
        assert run_live.await_args.kwargs["interval"] == 3.0

    def test_below_the_floor_is_not_refused_by_the_parser(self, run_live):
        """The floor is enforced once, in the library: the parser passes 0.5 through."""
        result = _invoke(["--live", "--interval", "0.5"])
        assert result.exit_code == 0, result.output
        assert run_live.await_args.kwargs["interval"] == 0.5


# ── The archive line ─────────────────────────────────────────────────────────


class TestArchiveReport:
    def test_a_db_run_logs_where_the_session_was_archived(self, tmp_path, run_live, caplog):
        db = tmp_path / "m.db"
        run_live.return_value = _report(db)
        with caplog.at_level(logging.INFO, logger="otto.cli.monitor"):
            result = _invoke(["--live", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert f"Monitor session sess-1 archived to {db}" in caplog.text

    def test_a_run_without_db_logs_no_archive(self, run_live, caplog):
        with caplog.at_level(logging.INFO, logger="otto.cli.monitor"):
            result = _invoke(["--live"])
        assert result.exit_code == 0, result.output
        assert "archived" not in caplog.text


# ── Reservation gate: per mode, not uniform ──────────────────────────────────
#
# monitor registers gate=False (see builtin_commands.py) and gates itself:
# reviewing a saved <source> reads a local file and never touches live
# hardware, so it is gate-exempt by design; --live collection still gates.
# The reservation state is what the lab slice stashes in production, so the
# stubbed lab slice seeds it here.


@pytest.fixture
def reservation(monkeypatch):
    res = MagicMock()
    res.evaluate.return_value = ReservationGateResult(checked=True, skipped=False, warning=None)

    def lab_session(ctx, spec):
        del spec
        ctx.meta["otto_reservation"] = res

    monkeypatch.setattr("otto.cli.invoke.ensure_lab_session", lab_session)
    return res


class TestGatePerMode:
    def test_review_does_not_evaluate_the_gate(self, tmp_path, reservation, serve_review):
        src = tmp_path / "m.json"
        src.write_text("{}")
        result = _invoke([str(src)])
        assert result.exit_code == 0, result.output
        serve_review.assert_awaited_once()
        reservation.evaluate.assert_not_called()

    def test_live_evaluates_the_gate_before_the_run(self, reservation, run_live):
        order = []
        reservation.evaluate.side_effect = lambda: (
            order.append("gate"),
            ReservationGateResult(checked=True, skipped=False, warning=None),
        )[1]
        run_live.side_effect = lambda **_: order.append("run") or _report()
        result = _invoke(["--live"])
        assert result.exit_code == 0, result.output
        assert order == ["gate", "run"]

    def test_live_prints_the_gate_warning(self, reservation, run_live):
        warning = (
            "\N{WARNING SIGN}  Reservation check SKIPPED for user 'alice' "
            "on lab 'x'. Required resources: []"
        )
        reservation.evaluate.return_value = ReservationGateResult(
            checked=False, skipped=True, warning=warning
        )
        result = _invoke(["--live"])
        assert result.exit_code == 0, result.output
        # rich strips the [bold red] markup and may word-wrap at the console
        # width, so compare with whitespace normalized.
        assert _flat(warning) in _flat(result.output)

    def test_live_without_reservation_state_is_a_no_op(self, run_live):
        result = _invoke(["--live"])
        assert result.exit_code == 0, result.output
        assert result.output == ""
