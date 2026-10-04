"""The repo-aware logging install, decided by the library."""

import io
import logging
from datetime import datetime, timezone

import pytest
from rich.console import Console

from otto.logger import management
from otto.logger.formatters import format_log_time
from otto.session import LoggingLevelsConflictError, install_logging, merge_logging_levels


class _LevelsRepo:
    """A repo double carrying only what ``merge_logging_levels`` and ``install_logging`` read."""

    def __init__(self, name: str, levels: dict[str, str]) -> None:
        self.name = name
        self.logging_levels = levels


class TestLoggingLevelsMerge:
    """Design 2026-08-30 §4.2: repo tables union; a disagreement is an error."""

    def test_tables_union_across_repos(self):
        merged = merge_logging_levels(
            [
                _LevelsRepo("alpha", {"asyncssh": "DEBUG"}),
                _LevelsRepo("beta", {"vendor": "ERROR"}),
            ]
        )
        assert merged == {"asyncssh": "DEBUG", "vendor": "ERROR"}

    def test_the_same_level_twice_is_not_a_conflict(self):
        """A shared vendor SDK quieted by two repos must not error."""
        merged = merge_logging_levels(
            [
                _LevelsRepo("alpha", {"vendor": "ERROR"}),
                _LevelsRepo("beta", {"vendor": "ERROR"}),
            ]
        )
        assert merged == {"vendor": "ERROR"}

    def test_a_conflict_names_both_repos_and_the_logger(self):
        with pytest.raises(LoggingLevelsConflictError) as exc:
            merge_logging_levels(
                [
                    _LevelsRepo("alpha", {"vendor": "DEBUG"}),
                    _LevelsRepo("beta", {"vendor": "ERROR"}),
                ]
            )
        message = str(exc.value)
        # The operator has to know WHICH two files to reconcile, and over what.
        assert "alpha" in message
        assert "beta" in message
        assert "vendor" in message
        assert "DEBUG" in message
        assert "ERROR" in message

    def test_the_conflict_names_the_repo_that_established_the_value(self):
        """The operator has to be sent to the file that actually set it.

        With A and B agreeing and C differing, crediting the LAST agreeing repo
        would point at B — the operator edits B, and the same error re-fires
        naming A and C. The middle repo is the discriminator, so it is here.
        """
        with pytest.raises(LoggingLevelsConflictError) as exc:
            merge_logging_levels(
                [
                    _LevelsRepo("alpha", {"vendor": "DEBUG"}),
                    _LevelsRepo("beta", {"vendor": "DEBUG"}),
                    _LevelsRepo("gamma", {"vendor": "ERROR"}),
                ]
            )
        message = str(exc.value)
        assert "alpha" in message
        assert "gamma" in message
        assert "beta" not in message, (
            "the message credited a repo that merely agreed with the established value"
        )


@pytest.fixture
def _clean_logging():
    management.reset()
    yield
    management.reset()


def _repos(monkeypatch, *tables):
    repos = [_LevelsRepo(f"r{i}", dict(table)) for i, table in enumerate(tables)]
    monkeypatch.setattr("otto.config.bootstrapped.get_repos", lambda: repos)
    return repos


# The record's timestamp, fixed so the time column it would render is known
# exactly. Rich renders it in local time.
_CREATED = datetime(2021, 6, 15, 12, 34, 56, 789000, tzinfo=timezone.utc).timestamp()
_LOCAL = datetime.fromtimestamp(_CREATED, tz=timezone.utc).astimezone()
_TIME_COLUMN = format_log_time(_LOCAL).plain
_DATE = _LOCAL.strftime("%Y-%m-%d")


def _render_through_console_handler() -> str:
    """Log one record through the installed console handler into a captured console."""
    handler = management._state.console_handler
    assert handler is not None
    captured = io.StringIO()
    handler.console = Console(file=captured, width=200, color_system=None)
    handler.handle(
        logging.makeLogRecord(
            {
                "name": "probe",
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": "probe-message",
                "created": _CREATED,
            }
        )
    )
    return captured.getvalue()


@pytest.mark.usefixtures("_clean_logging")
def test_install_logging_applies_every_repos_levels(monkeypatch):
    _repos(monkeypatch, {"vendor.sdk": "ERROR"}, {"other": "WARNING"})
    install_logging()
    assert logging.getLogger("vendor.sdk").level == logging.ERROR
    assert logging.getLogger("other").level == logging.WARNING


@pytest.mark.usefixtures("_clean_logging")
def test_an_explicit_override_wins_over_a_repo(monkeypatch):
    _repos(monkeypatch, {"vendor.sdk": "ERROR"})
    install_logging(overrides={"vendor.sdk": "DEBUG"})
    assert logging.getLogger("vendor.sdk").level == logging.DEBUG


@pytest.mark.usefixtures("_clean_logging")
def test_two_repos_disagreeing_refuse_before_anything_is_installed(monkeypatch):
    _repos(monkeypatch, {"x": "ERROR"}, {"x": "INFO"})
    with pytest.raises(LoggingLevelsConflictError, match="r0 says ERROR, r1 says INFO"):
        install_logging()
    assert management._state.console_handler is None


@pytest.mark.usefixtures("_clean_logging")
def test_the_host_filter_goes_on_the_console_only(monkeypatch, tmp_path):
    from otto.host import HostFilter

    _repos(monkeypatch)
    install_logging(output_dir=tmp_path)
    console = management._state.console_handler
    assert any(isinstance(f, HostFilter) for f in console.filters)
    verbose = management._state.verbose_handler
    assert verbose is not None
    assert not any(isinstance(f, HostFilter) for f in verbose.filters)


@pytest.mark.usefixtures("_clean_logging")
def test_log_level_and_show_time_reach_the_console_handler(monkeypatch):
    _repos(monkeypatch)
    install_logging(log_level="DEBUG", show_time=True)
    assert management._state.console_handler.level == logging.DEBUG
    line = _render_through_console_handler()
    assert "probe-message" in line
    assert _TIME_COLUMN in line, line


@pytest.mark.usefixtures("_clean_logging")
def test_the_console_has_no_timestamp_by_default(monkeypatch):
    _repos(monkeypatch)
    install_logging()
    line = _render_through_console_handler()
    assert "probe-message" in line
    assert _DATE not in line, line


@pytest.mark.usefixtures("_clean_logging")
def test_a_prior_cli_install_leaves_exactly_one_host_filter(monkeypatch):
    from otto.host import HostFilter

    _repos(monkeypatch)
    management.install("INFO")
    management.attach_console_suppress_filter(HostFilter())
    install_logging()
    console = management._state.console_handler
    assert sum(isinstance(f, HostFilter) for f in console.filters) == 1
