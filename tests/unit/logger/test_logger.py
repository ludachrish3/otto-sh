import os
import time
from logging import getLogger
from pathlib import Path

import pytest

import otto.logger.management as management_mod
from otto.logger import management

logger = getLogger("otto")


@pytest.fixture(autouse=True)
def create_logger(tmpdir):
    """Initialize management state and create output dir before every test."""
    management.init_cli_logging(xdir=tmpdir, log_level="INFO", keep_days=7)
    management.create_output_dir(command="pytest", subcommand="logger_test")


def _backdate(directory: Path, seconds: float) -> None:
    """Set ``directory``'s mtime ``seconds`` into the past.

    ``remove_old_logs`` keys off ``st_mtime``; backdating explicitly makes the
    age cutoff deterministic instead of racing a sub-second wall-clock window.
    """
    past = time.time() - seconds
    os.utime(directory, (past, past))


def test_remove_old_logs_ignores_non_output_entries():
    """Stray files and non-output directories must never be pruned.

    Regression guard: with a misconfigured ``xdir`` (e.g. left pointing at the
    repo root by a leaked global), the old code walked real content and called
    ``rmtree('docs/conf.py')`` → NotADirectoryError. Only timestamped output
    directories may be pruned, even when the strays are older than the cutoff.
    """
    cmd_dir = management._state.xdir / "pytest"
    stray_file = cmd_dir / "conf.py"
    stray_file.write_text("not a log dir")
    stray_dir = cmd_dir / "guide"
    stray_dir.mkdir()
    _backdate(stray_file, seconds=3600)
    _backdate(stray_dir, seconds=3600)

    # Must not raise, and must leave both strays untouched.
    management.remove_old_logs(seconds=60)

    assert stray_file.exists(), "remove_old_logs deleted a stray file"
    assert stray_dir.exists(), "remove_old_logs deleted a non-output directory"


def test_remove_old_logs_old_logs_do_not_exist(tmpdir, caplog):

    xdir = management._state.xdir

    assert len(list(xdir.iterdir())) == 1
    management.remove_old_logs(seconds=60)
    assert len(list(xdir.iterdir())) == 1

    assert len(caplog.records) == 0


def test_remove_old_logs_xdir_does_not_exist(tmpdir, caplog):
    """remove_old_logs returns cleanly when xdir does not exist."""
    management._state.xdir = Path(tmpdir) / "nonexistent"
    management.remove_old_logs(seconds=60)
    assert len(caplog.records) == 0


# TODO: Look into a better way to automate verification of this test.
# Currently does not colorize or any other rich formatting
# Maybe verify stdout content?
def test_log_formatting(tmpdir, caplog):
    with caplog.at_level("INFO", logger="otto"):
        logger.info("[magenta]This is important")
    assert len(caplog.records) == 1


def test_remove_old_logs_respects_time_budget(monkeypatch, caplog):
    """The scan stops once the time budget is exceeded and resumes next run."""
    cmd_dir = management._state.xdir / "pytest"
    olds = []
    for i in range(6):
        d = cmd_dir / f"20200101_0000{i:02d}_000"
        d.mkdir()
        _backdate(d, seconds=3600)
        olds.append(d)

    # Fake monotonic clock advancing 1.0s per call: start=0.0, then the inner
    # checks see 1.0, 2.0, 3.0, ... so a 2.5s budget trips on the 3rd check.
    ticks = iter([float(n) for n in range(1000)])
    monkeypatch.setattr(management_mod.time, "monotonic", lambda: next(ticks))

    with caplog.at_level("DEBUG", logger="otto"):
        management.remove_old_logs(seconds=60, time_budget=2.5)

    assert [d for d in olds if d.exists()], "budget should stop before removing all dirs"
    assert any("time budget" in r.message for r in caplog.records)

    # A second pass with a non-advancing clock (elapsed always 0) drains the rest.
    monkeypatch.setattr(management_mod.time, "monotonic", lambda: 0.0)
    management.remove_old_logs(seconds=60, time_budget=2.5)
    assert not [d for d in olds if d.exists()], "remaining old dirs should drain on the next run"


def test_remove_old_logs_no_budget_message_on_normal_run(caplog):
    """A small tree finishes well under budget — no truncation message."""
    cmd_dir = management._state.xdir / "pytest"
    d = cmd_dir / "20200101_000000_000"
    d.mkdir()
    _backdate(d, seconds=3600)

    with caplog.at_level("DEBUG", logger="otto"):
        management.remove_old_logs(seconds=60)  # default 5.0s budget, real clock

    assert not d.exists()
    assert not any("time budget" in r.message for r in caplog.records)
