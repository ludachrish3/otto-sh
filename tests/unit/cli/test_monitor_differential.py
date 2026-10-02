"""THE SPLIT-BRAIN GUARD for ``otto monitor``: the leaf hands ``run_live`` and
``serve_review`` exactly the parsed flags and reports exactly the library's
refusals, in flag spelling. No rule may live in the leaf.

Verified red when written: making the leaf pass ``interval=5.0`` to ``run_live``
regardless of ``--interval`` failed two rows: ``every-flag`` on the kwargs
equality (``{'interval': 5.0} != {'interval': 2.5}``), and the ``interval`` usage
row on ``assert result.exit_code == 2`` (``assert 1 == 2``: the real library,
handed 5.0, went on to refuse the empty lab). Moving ``MonitorInputError`` and
``ReviewSourceError`` into the ``fail`` arm failed the ``interval``, ``hosts``
and bad-source rows, each on ``assert 1 == 2``.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.bootstrap import ProjectScopeError
from otto.cli.monitor import monitor_app
from otto.config.scope import EmptySelectionError
from otto.monitor.errors import MonitorTlsError, NoMonitorableHostsError
from otto.monitor.live import LiveReport
from tests._fixtures.dispatch import DispatchRunner
from tests.unit.cli.conftest import _flat

_NOW = datetime.now(tz=timezone.utc)
_REPORT = LiveReport(session_id="s", hosts=["h"], db=None, start=_NOW, end=_NOW)


@pytest.fixture(autouse=True)
def _no_preamble(monkeypatch):
    """The CLI preamble's slices are tested elsewhere: stub them, never the library."""
    monkeypatch.setattr("otto.cli.invoke.ensure_lab_session", lambda *a, **k: None)
    monkeypatch.setattr("otto.cli.invoke.ensure_cli_session", lambda *a, **k: None)


def _invoke(argv):
    return DispatchRunner().invoke(monitor_app, argv, spec_name="monitor")


LIVE_ROWS = [
    (["--live"], {"hosts": None, "interval": 5.0, "db": None, "label": None, "note": None}),
    (
        [
            "--live",
            "--hosts",
            "web.*",
            "--interval",
            "2.5",
            "--db",
            "{tmp}/m.db",
            "--label",
            "L",
            "--note",
            "N",
        ],
        {"hosts": "web.*", "interval": 2.5, "db": "{tmp}/m.db", "label": "L", "note": "N"},
    ),
]


@pytest.mark.parametrize(("argv", "kwargs"), LIVE_ROWS, ids=["bare", "every-flag"])
def test_live_hands_run_live_exactly_its_flags(argv, kwargs, tmp_path):
    argv = [a.format(tmp=tmp_path) for a in argv]
    expected = {
        k: (Path(v.format(tmp=tmp_path)) if k == "db" and v else v) for k, v in kwargs.items()
    }
    fake = AsyncMock(return_value=_REPORT)
    with patch("otto.monitor.live.run_live", fake):
        result = _invoke(argv)
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    assert fake.await_args.args == ()
    assert fake.await_args.kwargs == expected


def test_review_hands_serve_review_the_source_and_the_repos(tmp_path, monkeypatch):
    src = tmp_path / "x.json"
    src.write_text("{}")
    repos = [object()]
    monkeypatch.setattr("otto.config.get_repos", lambda: repos)
    fake = AsyncMock(return_value=None)
    with patch("otto.monitor.review.serve_review", fake):
        result = _invoke([str(src)])
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    assert fake.await_args.args == (src,)
    assert fake.await_args.kwargs == {"repos": repos}


# The REAL library refuses these before touching any lab: the row proves the
# library's message reaches the user in flag spelling, exit 2.
USAGE_ROWS = [
    (["--live", "--interval", "0.5"], "--interval", "at least 1.0s"),
    (["--live", "--hosts", "("], "--hosts", "not a valid regex"),
]


@pytest.mark.parametrize(("argv", "flag", "text"), USAGE_ROWS, ids=["interval", "hosts"])
def test_library_input_refusals_are_usage_errors_in_flag_spelling(argv, flag, text, monkeypatch):
    monkeypatch.setattr("otto.config.get_repos", list)
    result = _invoke(argv)
    assert result.exit_code == 2, result.output
    flat = _flat(result.output)
    assert f"Invalid value for {flag}" in flat
    assert text in flat


def test_a_bad_review_source_is_a_usage_error_naming_source(tmp_path, monkeypatch):
    monkeypatch.setattr("otto.config.get_repos", list)
    src = tmp_path / "x.csv"
    src.write_text("a")
    result = _invoke([str(src)])
    assert result.exit_code == 2, result.output
    flat = _flat(result.output)
    assert "Invalid value for SOURCE" in flat
    assert "suffix '.csv'" in flat


FAIL_ROWS = [
    EmptySelectionError("x", 3),
    ProjectScopeError("/repo", "the [project] scope admits no host here"),
    NoMonitorableHostsError(["z1"]),
    MonitorTlsError("[monitor] TLS settings disagree", setting=None, repos=["a", "b"]),
]


@pytest.mark.parametrize("exc", FAIL_ROWS, ids=lambda e: type(e).__name__)
def test_framed_refusals_exit_1_with_the_librarys_words(exc):
    with patch("otto.monitor.live.run_live", AsyncMock(side_effect=exc)):
        result = _invoke(["--live"])
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, type(exc)), "the refusal reached typer unframed"
    assert _flat(str(exc)) in _flat(result.output)


def test_review_tls_refusal_is_framed_exit_1(tmp_path):
    src = tmp_path / "x.json"
    src.write_text("{}")
    exc = MonitorTlsError(
        "[monitor] tls_cert /nope does not exist", setting="tls_cert", repos=["a"]
    )
    with patch("otto.monitor.review.serve_review", AsyncMock(side_effect=exc)):
        result = _invoke([str(src)])
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, MonitorTlsError), "the refusal reached typer unframed"
    assert _flat(str(exc)) in _flat(result.output)
