"""The repo-aware logging install, decided by the library."""

import logging
from types import SimpleNamespace

import pytest

from otto.logger import management
from otto.session import LoggingLevelsConflictError, install_logging, merge_logging_levels


class _LevelsRepo:
    """A repo double carrying only what ``merge_logging_levels`` reads."""

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
    repos = [
        SimpleNamespace(name=f"r{i}", logging_levels=dict(table)) for i, table in enumerate(tables)
    ]
    monkeypatch.setattr("otto.config.get_repos", lambda: repos)
    return repos


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
    console = management._state.console_handler
    assert console.level == logging.DEBUG
    assert console._log_render.show_time is True


@pytest.mark.usefixtures("_clean_logging")
def test_the_console_has_no_timestamp_by_default(monkeypatch):
    _repos(monkeypatch)
    install_logging()
    assert management._state.console_handler._log_render.show_time is False


@pytest.mark.usefixtures("_clean_logging")
def test_a_prior_cli_install_leaves_exactly_one_host_filter(monkeypatch):
    from otto.host import HostFilter

    _repos(monkeypatch)
    management.install("INFO")
    management.attach_console_suppress_filter(HostFilter())
    install_logging()
    console = management._state.console_handler
    assert sum(isinstance(f, HostFilter) for f in console.filters) == 1
