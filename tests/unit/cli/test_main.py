"""
Unit tests for the main CLI entry-point argument parsing.

Tests cover:
  - Eager options that exit before the main callback (--version, --list-labs)
  - Global options forwarded to init_cli_logging (--show-time, --log-level, --log-days, --xdir)
  - Lab-loading arguments (--lab, --show-lab, --list-hosts)
  - Validation of numeric constraints (--log-days min=0, --lab-depth min=0)
  - --field / --debug toggle
"""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.invoke import LoggingLevelsConflictError, merge_logging_levels
from otto.cli.main import app
from otto.cli.registry import register_cli_command
from otto.logger import management
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.bootstrapstub import bootstrap_stub

runner = CliRunner()


# Task 7: lab loading is now lazy — it runs in the leaf-invoke preamble, not the
# root callback. Tests that assert lab-load / logging side-effects must therefore
# dispatch a real (non-help, non-lab-free) leaf so the preamble fires. This
# scratch command is that leaf: its body is a no-op, but invoking `otto --lab X
# _main_probe` drives ensure_cli_session (logging) + ensure_lab_context
# (lab load + reservation state) + the per-command output dir + the gate.
async def _main_probe() -> CommandResult:
    return CommandResult(Status.Success, value="", command="probe", retcode=0)


# gate=False: these tests exercise logging/lab/context, not the reservation
# gate (which has its own coverage); keeping it off avoids building a real
# reservation backend in a mock-lab environment.
register_cli_command("_main_probe", _main_probe, help="internal test probe", gate=False)


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def main_mocks(tmp_path):
    """
    Create main parser mocks

    Patch every external dependency touched by the main callback so tests
    don't need a real lab file, logger, or config module.
    """
    mock_lab = MagicMock()
    mock_lab.hosts = {}
    mock_config = MagicMock()
    mock_config.lab = mock_lab

    # Clear OTTO_* env vars so Typer envvar= defaults aren't overridden
    # by the user's shell environment; point OTTO_XDIR at tmp_path so logger
    # side-effects land there instead of the project root (--xdir is optional
    # and defaults to CWD, which we don't want tests writing to).
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    clean_env["OTTO_XDIR"] = str(tmp_path)

    from otto import bootstrap as bs

    bs._reset()
    # The lab / session work is lazy (Task 7) and lives in otto.cli.invoke, which
    # imports get_repos / load_lab from otto.config at call time — patch the
    # source so both the root-callback (--show-lab) and preamble paths see mocks.
    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("otto.logger.management.init_cli_logging") as p_logger,
        patch("otto.config.get_repos", return_value=[]),
        patch("otto.config.load_lab", return_value=mock_lab) as p_getlab,
    ):
        yield {
            "init_cli_logging": p_logger,
            "load_lab": p_getlab,
            "lab": mock_lab,
            "config": mock_config,
        }
    bs._reset()


def _invoke(extra_args: list[str]):
    """
    Invoke the main app, driving a real leaf so the lazy preamble fires.

    ``--lab test_lab`` is pre-filled, and the ``_main_probe`` leaf is appended so
    that ensure_cli_session / ensure_lab_context run (they are lazy since Task 7
    and no longer live in the root callback). Root action flags like ``--show-lab``
    / ``--list-hosts`` still short-circuit in the root callback before the probe
    dispatches, so those tests observe only the lab load, as before.
    """
    return runner.invoke(app, ["--lab", "test_lab", *extra_args, "_main_probe"])


# ── Eager / early-exit options ────────────────────────────────────────────────


class TestEagerOptions:
    """Options that exit before the main callback body runs."""

    def test_version_exits_zero(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0

    def test_version_prints_version_string(self):
        result = runner.invoke(app, ["--version"])
        assert "version" in result.output.lower()

    def test_help_short_flag(self):
        result = runner.invoke(app, ["-h"])
        assert result.exit_code == 0

    def test_help_long_flag(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_help_mentions_otto(self):
        result = runner.invoke(app, ["-h"])
        assert "otto" in result.output.lower() or "OTTO" in result.output

    def test_root_reservation_flag_untouched(self, monkeypatch: pytest.MonkeyPatch):
        # Task 8 pin: `--as-user` on the root command is reservation identity
        # (see otto.reservations.identity) — a different concept from the
        # per-call `user=`/`--user` Tasks 1-7 threaded through host verbs.
        # Task 1 renamed the host-verb flag to `--user`; the root reservation
        # flag keeps its own name and must not be touched by that rename.
        # COLUMNS pinned wide for the same reason as
        # test_lab_help_advertises_the_plus_separator above (GH issue #89):
        # at the default width rich can truncate a long option for display,
        # which would make this substring check width-dependent.
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--as-user" in result.output

    def test_list_labs_exits_zero(self):
        # get_repos() returns [] in test env; just verifies the flag is accepted
        result = runner.invoke(app, ["--list-labs"])
        assert result.exit_code == 0

    def test_lab_help_advertises_the_plus_separator(self, monkeypatch: pytest.MonkeyPatch):
        # Pin the rich console width: under CliRunner (non-tty) rich resolves
        # width from COLUMNS, defaulting to 80. At narrow widths (e.g. 56) rich
        # squeezes the metavar column and folds "LAB[+LAB...]" mid-string,
        # making the substring assertion width-dependent (GH issue #89).
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "LAB[+LAB...]" in result.output
        assert "COMMA SEPARATED LIST" not in result.output


# ── Argument validation ───────────────────────────────────────────────────────


class TestArgumentValidation:
    """Typer/Click constraint enforcement for the main callback options."""

    def test_missing_lab_option_is_rejected(self):
        """--lab is required; without it (and without OTTO_LAB env var) the CLI must fail."""
        result = runner.invoke(app, [], env={"OTTO_LAB": ""})
        assert result.exit_code != 0

    def test_lab_needing_path_without_lab_reports_missing_option(self):
        """A non-lab-free invocation without --lab errors with the clear message."""
        result = runner.invoke(app, ["--show-lab"], env={"OTTO_LAB": ""})
        assert result.exit_code != 0
        # click 8.2+ (Typer 0.26) routes usage errors to stderr, not stdout.
        assert "Missing option '--lab'" in result.stderr

    def test_negative_log_days_rejected(self, main_mocks):
        result = _invoke(["--log-days", "-1"])
        assert result.exit_code == 2

    def test_zero_log_days_accepted(self, main_mocks):
        result = _invoke(["--log-days", "0"])
        assert result.exit_code == 0

    def test_positive_log_days_accepted(self, main_mocks):
        result = _invoke(["--log-days", "7"])
        assert result.exit_code == 0


# ── Lab-free subcommands ──────────────────────────────────────────────────────


class TestLabFreeSubcommands:
    """`otto schema` introspects otto itself and must run without a lab."""

    def test_schema_export_runs_without_lab(self, tmp_path):
        out = tmp_path / "schemas"
        result = runner.invoke(
            app,
            ["schema", "export", "--out", str(out), "--builtins-only"],
            env={"OTTO_LAB": ""},
        )
        assert result.exit_code == 0, result.output
        assert (out / "lab.schema.json").is_file()


# ── Lab-free flags: --help and --list-* discovery ────────────────────────────


class TestLabFreeFlags:
    """Subcommand --help and --list-* discovery flags must work without --lab.

    These flags inspect otto itself (registered suites / instructions) and
    touch no host resources, so forcing --lab would be a pointless barrier.

    Boundary: actual command execution (``otto test <Suite>``) and
    ``otto --list-hosts`` still require --lab because they operate on real
    lab state.
    """

    def test_test_help_exits_zero_without_lab(self):
        """``otto test --help`` must succeed with no --lab."""
        result = runner.invoke(app, ["test", "--help"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_test_short_help_exits_zero_without_lab(self):
        """``otto test -h`` must succeed with no --lab."""
        result = runner.invoke(app, ["test", "-h"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_run_help_exits_zero_without_lab(self):
        """``otto run --help`` must succeed with no --lab."""
        result = runner.invoke(app, ["run", "--help"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0, result.output

    def test_test_list_suites_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-suites`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-suites"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_test_list_tests_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-tests`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-tests"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_test_list_markers_exits_zero_without_lab(self, tmp_path):
        """``otto test --list-markers`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["test", "--list-markers"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    def test_run_list_instructions_exits_zero_without_lab(self, tmp_path):
        """``otto run --list-instructions`` must succeed with no --lab."""
        result = runner.invoke(
            app,
            ["run", "--list-instructions"],
            env={"OTTO_LAB": "", "OTTO_XDIR": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output

    # ── Boundary: these still require --lab ───────────────────────────────────

    def test_actual_suite_run_still_requires_lab(self):
        """``otto test <Suite>`` (no --help/--list-*) must still exit 2 without --lab.

        With lazy loading (Task 7), the ``--lab`` requirement is enforced by the
        leaf-invoke preamble rather than the root callback, so this must exercise a
        *real* registered suite leaf (an unknown name would error at parse time with
        "No such command", never reaching the preamble). A registered suite reaches
        the preamble, which enforces ``--lab`` and exits 2.
        """
        from otto.suite import OttoSuite
        from otto.suite.register import register_suite_class

        class _LabReqSuite(OttoSuite):
            pass

        register_suite_class(_LabReqSuite)

        # suite_app resolves _LabReqSuite lazily from the SUITES registry —
        # no explicit attach step needed.
        result = runner.invoke(app, ["test", "_LabReqSuite"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2, result.output
        assert "--lab" in result.stderr or "--lab" in result.output

    def test_list_hosts_still_requires_lab(self):
        """``otto --list-hosts`` queries lab state; must still exit 2 without --lab."""
        result = runner.invoke(app, ["--list-hosts"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2
        assert "--lab" in result.stderr or "--lab" in result.output


# ── Logger arguments ──────────────────────────────────────────────────────────


class TestLoggerArguments:
    """Verify that parsed CLI values flow through init_cli_logging to real logger state.

    init_cli_logging runs for real here.  Assertions check observable logger
    state (level, xdir, rich_logging) and the I/O-boundary mocks
    (RichHandler constructor args, remove_old_logs call args).
    """

    # One invocation builds the console handler TWICE since spec 2026-08-30
    # §3.1: the root callback installs it as soon as --log-level is parsed, and
    # `ensure_cli_session` re-affirms it idempotently once repo settings exist.
    # So these check the latest call and that every call AGREES — the flag must
    # reach both installs, or the second silently undoes the first.
    # TestEarlyConsoleHandler pins that only one handler survives on root.
    def test_show_time_default_is_false(self, real_main_mocks):
        _invoke([])
        rich = real_main_mocks["RichHandler"]
        rich.assert_called_with(
            level=ANY,
            console=ANY,
            show_time=False,
            tracebacks_max_frames=ANY,
            tracebacks_show_locals=ANY,
            markup=ANY,
            highlighter=ANY,
            show_path=ANY,
            enable_link_path=ANY,
            log_time_format=ANY,
            omit_repeated_times=ANY,
        )
        assert {c.kwargs["show_time"] for c in rich.call_args_list} == {False}

    def test_show_time_flag(self, real_main_mocks):
        _invoke(["--show-time"])
        rich = real_main_mocks["RichHandler"]
        rich.assert_called_with(
            level=ANY,
            console=ANY,
            show_time=True,
            tracebacks_max_frames=ANY,
            tracebacks_show_locals=ANY,
            markup=ANY,
            highlighter=ANY,
            show_path=ANY,
            enable_link_path=ANY,
            log_time_format=ANY,
            omit_repeated_times=ANY,
        )
        assert {c.kwargs["show_time"] for c in rich.call_args_list} == {True}

    def test_show_time_flag_replaces_verbose(self):
        result = runner.invoke(app, ["--help"])
        assert "--show-time" in result.output
        assert "--verbose" not in result.output

    def test_lab_depth_flag_present(self):
        result = runner.invoke(app, ["--help"])
        assert "--lab-depth" in result.output

    def test_rich_log_file_default_is_false(self, real_main_mocks):
        _invoke([])
        assert management._state.rich_log_file is False

    def test_rich_log_file_true(self, real_main_mocks):
        _invoke(["--rich-log-file"])
        assert management._state.rich_log_file is True

    def test_rich_log_file_explicit_false(self, real_main_mocks):
        _invoke(["--no-rich-log-file"])
        assert management._state.rich_log_file is False

    # --log-level sets the ROOT logger to the verbose floor: otto configures
    # root, and the 'otto' logger keeps its library-citizen NOTSET.
    def test_log_level_default_is_info(self, real_main_mocks):
        _invoke([])
        assert logging.getLogger().level == logging.INFO

    def test_log_level_custom(self, real_main_mocks):
        _invoke(["--log-level", "DEBUG"])
        assert logging.getLogger().level == logging.DEBUG

    def test_log_level_custom_lower_case(self, real_main_mocks):
        _invoke(["--log-level", "debug"])
        assert logging.getLogger().level == logging.DEBUG

    def test_log_days_default(self, real_main_mocks):
        _invoke([])
        assert management._state.keep_seconds == 30 * 24 * 60 * 60

    def test_log_days_custom(self, real_main_mocks):
        _invoke(["--log-days", "14"])
        assert management._state.keep_seconds == 14 * 24 * 60 * 60

    def test_xdir_from_env(self, real_main_mocks):
        # real_main_mocks pre-sets OTTO_XDIR to tmp_path; the callback should
        # pick that up without an explicit --xdir on the command line.
        _invoke([])
        assert management._state.xdir == real_main_mocks["tmp_path"]

    def test_xdir_custom_path(self, real_main_mocks, tmp_path):
        custom_xdir = tmp_path / "custom_xdir"
        custom_xdir.mkdir()
        _invoke(["--xdir", str(custom_xdir)])
        assert management._state.xdir == custom_xdir

    def test_xdir_default_when_neither_flag_nor_env(self, real_main_mocks, monkeypatch):
        """--xdir is optional: with neither flag nor OTTO_XDIR it defaults to CWD.

        The CWD default is safe because ``remove_old_logs`` only rmtree's entries
        matching otto's timestamped log-dir name pattern (see management.py), so a
        CWD-pointed xdir can no longer walk foreign trees at startup.
        """
        monkeypatch.delenv("OTTO_XDIR", raising=False)
        result = _invoke([])
        assert result.exit_code == 0
        assert management._state.xdir == Path()


# ── The [logging.levels] noise floor ─────────────────────────────────────────


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

    def test_a_repo_table_reaches_the_live_logger(self, real_main_mocks):
        """The WIRING: ensure_cli_session merges repo tables onto otto's floor.

        Without this, every assertion above would still pass with the call
        missing from ``ensure_cli_session`` entirely.
        """
        real_main_mocks["repo"].logging_levels = {
            "vendor_under_test": "ERROR",
            # An override of an otto default, proving the merge order.
            "asyncssh": "DEBUG",
        }
        result = _invoke([])
        assert result.exit_code == 0
        assert logging.getLogger("vendor_under_test").level == logging.ERROR
        assert logging.getLogger("asyncssh").level == logging.DEBUG


# ── The early console handler ────────────────────────────────────────────────


class TestEarlyConsoleHandler:
    """Design 2026-08-30 §3.1: the console goes up in the ROOT CALLBACK.

    Not in ``ensure_cli_session``, which runs several gates later and not at
    all for a command that exits inside one of them. Everything between the
    two — the ``-I``/``-E`` validator, the bootstrap gate, and every preflight
    and lab probe they lead to — is inside the window this pins.
    """

    def test_a_warning_before_the_session_reaches_the_operator(self, main_mocks, monkeypatch):
        """A logger.warning from a pre-session gate must land in the output.

        ``main_mocks`` patches ``init_cli_logging`` out, so the ONLY thing that
        can put a handler on root in this run is the root callback's own
        ``install_console`` — which is exactly the claim. The warning is
        INJECTED at a real pre-session call site (the bootstrap gate, which
        ``command_preamble`` runs before it opens the session) rather than
        borrowed from a real one: otto's own gates there print instead of
        logging for reasons that survive this change, so inheriting one of
        those would pin nothing.
        """
        import otto.cli.invoke as invoke_mod

        real_gate = invoke_mod.fail_loud_on_bootstrap_errors

        def _warning_gate(ctx=None):
            logging.getLogger("pre_session_gate").warning("a gate spoke before the session")
            real_gate(ctx)

        monkeypatch.setattr(invoke_mod, "fail_loud_on_bootstrap_errors", _warning_gate)

        result = _invoke([])

        assert result.exit_code == 0, result.output
        assert "a gate spoke before the session" in result.output

    def test_the_second_install_leaves_one_handler_on_root(self, real_main_mocks):
        """The callback installs, then the session installs again — idempotently.

        ``real_main_mocks`` lets the real ``init_cli_logging`` run, so this
        invocation calls ``install_console`` twice. Two console handlers on
        root would print every subsequent record twice.
        """
        result = _invoke([])

        assert result.exit_code == 0, result.output
        marked = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, management.OTTO_HANDLER_ATTR, False)
        ]
        assert len(marked) == 1, marked


# ── Lab loading ───────────────────────────────────────────────────────────────


class TestLabLoading:
    """Verify lab loading produces real Lab objects with correct hosts.

    load_lab runs for real here, reading lab.json from the tmp_path fixture.
    The fixture data has three hosts across two labs:
      - test_lab: host1, host2
      - lab2: host2, host3
    """

    def test_single_lab_loads_correct_hosts(self, real_main_mocks):
        result = _invoke([])
        assert result.exit_code == 0
        from otto.config import get_lab

        lab = get_lab()
        assert lab.name == "test_lab"
        # `local` is the built-in host injected into every lab by load_lab.
        assert set(lab.hosts.keys()) == {"host1", "host2", "local"}

    def test_multiple_labs_combine_on_plus(self, real_main_mocks):
        # Append the probe leaf so the lazy preamble loads the lab (Task 7).
        result = runner.invoke(app, ["--lab", "test_lab+lab2", "_main_probe"])
        assert result.exit_code == 0
        from otto.config import get_lab

        lab = get_lab()
        assert set(lab.hosts.keys()) == {"host1", "host2", "host3", "local"}

    def test_parse_lab_selection_accumulates_and_splits(self):
        """`--lab a+b --lab c` selects all three: repeats accumulate AND each value splits."""
        from otto.cli.main import parse_lab_selection

        assert parse_lab_selection(["a+b", "c"]) == ["a", "b", "c"]

    def test_parse_lab_selection_passes_none_through(self):
        """Unset --lab (and OTTO_LAB="") arrive as None and must stay None, never []."""
        from otto.cli.main import parse_lab_selection

        assert parse_lab_selection(None) is None

    def test_comma_is_not_a_separator(self, real_main_mocks):
        """The comma lost its meaning: `a,b` is one lab name, and no such lab exists."""
        result = runner.invoke(app, ["--lab", "test_lab,lab2", "_main_probe"])
        assert result.exit_code != 0

    @pytest.mark.parametrize("bad", ["a++b", "+a", "a+", ""])
    def test_malformed_lab_selection_is_a_usage_error(
        self, bad, real_main_mocks, monkeypatch: pytest.MonkeyPatch
    ):
        # Pin the rich console width: Rich renders BadParameter inside a
        # bordered, word-wrapped panel. At COLUMNS=100 the fold lands between
        # "lab" and "name", making the substring assertion below width-dependent
        # (GH issue #89) even though CI's 80-column default happens to pass.
        monkeypatch.setenv("COLUMNS", "300")
        result = runner.invoke(app, ["--lab", bad, "_main_probe"])
        assert result.exit_code == 2, result.output
        assert "empty lab name" in (result.output + (result.stderr or ""))
        # repr("") is `''`, too weak to search for — only pin the echoed value
        # for the non-empty malformed cases.
        if bad:
            assert repr(bad) in (result.output + (result.stderr or ""))

    def test_lab_selection_from_env_var_splits_on_plus(self, real_main_mocks):
        result = runner.invoke(app, ["_main_probe"], env={"OTTO_LAB": "test_lab+lab2"})
        assert result.exit_code == 0
        from otto.config import get_lab

        assert set(get_lab().hosts.keys()) == {"host1", "host2", "host3", "local"}

    def test_empty_env_var_still_means_no_lab(self, real_main_mocks):
        """OTTO_LAB="" must still be treated as "no lab selected".

        parse_lab_selection returns None for a falsy value, and the preamble's
        ``if not opts.labs:`` check (otto/cli/invoke.py) raises the same exit-2
        "Missing option '--lab'" usage error for None. This test pins the
        callback's None contract for the empty-string-env-var input; the
        contract itself (None, never []) is pinned directly by
        test_parse_lab_selection_passes_none_through.
        """
        result = runner.invoke(app, ["_main_probe"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2
        assert "--lab" in (result.output + (result.stderr or ""))

    def test_multiple_lab_flags(self, real_main_mocks):
        # Append the probe leaf so the lazy preamble loads the lab (Task 7).
        result = runner.invoke(app, ["--lab", "test_lab", "--lab", "lab2", "_main_probe"])
        assert result.exit_code == 0
        from otto.config import get_lab

        lab = get_lab()
        assert set(lab.hosts.keys()) == {"host1", "host2", "host3", "local"}

    def test_host_objects_have_correct_ip(self, real_main_mocks):
        _invoke([])
        from otto.config import get_lab

        lab = get_lab()
        assert lab.hosts["host1"].ip == "10.0.0.1"
        assert lab.hosts["host2"].ip == "10.0.0.2"

    def test_show_lab_exits_zero(self, real_main_mocks):
        result = _invoke(["--show-lab"])
        assert result.exit_code == 0

    def test_show_lab_lab_depth_zero_maps_to_unlimited(self, real_main_mocks):
        """--lab-depth 0 must reach pprint as max_depth=None (unlimited)."""
        # The --show-lab block does `from rich.pretty import pprint` locally,
        # so patch where it is looked up.
        with patch("rich.pretty.pprint") as spy:
            result = _invoke(["--show-lab", "--lab-depth", "0"])
        assert result.exit_code == 0
        spy.assert_called_once()
        assert spy.call_args.kwargs["max_depth"] is None

    def test_show_lab_lab_depth_value_passed_to_pprint(self, real_main_mocks):
        """--lab-depth N (N > 0) must reach pprint as max_depth=N."""
        with patch("rich.pretty.pprint") as spy:
            result = _invoke(["--show-lab", "--lab-depth", "2"])
        assert result.exit_code == 0
        spy.assert_called_once()
        assert spy.call_args.kwargs["max_depth"] == 2

    def test_list_hosts_exits_zero(self, real_main_mocks):
        result = _invoke(["--list-hosts"])
        assert result.exit_code == 0

    def test_list_hosts_output_contains_host_ids(self, real_main_mocks):
        result = _invoke(["--list-hosts"])
        assert "host1" in result.output
        assert "host2" in result.output


# ── Field / debug product mode ────────────────────────────────────────────────


class TestFieldDebugMode:
    """--field/--debug is a boolean toggle; verify both flags are accepted."""

    def test_default_mode_exits_zero(self, main_mocks):
        result = _invoke([])
        assert result.exit_code == 0

    def test_field_flag_accepted(self, main_mocks):
        result = _invoke(["--field"])
        assert result.exit_code == 0

    def test_debug_flag_accepted(self, main_mocks):
        result = _invoke(["--debug"])
        assert result.exit_code == 0


# ── Dry-run mode ─────────────────────────────────────────────────────────────


class TestDryRunMode:
    """Verify --dry-run flag is accepted and propagates to hosts."""

    def test_dry_run_flag_accepted(self, main_mocks):
        result = _invoke(["--dry-run"])
        assert result.exit_code == 0

    def test_dry_run_short_flag_accepted(self, main_mocks):
        result = _invoke(["-n"])
        assert result.exit_code == 0

    def test_dry_run_sets_context_flag(self, main_mocks):
        """--dry-run should enable dry_run on the active OttoContext."""
        from otto.host.host import is_dry_run

        _invoke(["--dry-run"])
        assert is_dry_run() is True


# ── --clear-autocomplete-cache ───────────────────────────────────────────────


class TestClearAutocompleteCache:
    """One flag clears BOTH completion caches: the main file and the sidecar."""

    @pytest.fixture
    def caches(self, tmp_path, monkeypatch):
        main = tmp_path / ".otto" / "completion_cache.json"
        main.parent.mkdir(parents=True)
        monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: main)
        return main, main.with_name("remote_completion_cache.json")

    def _run(self, capsys):
        import typer

        from otto.cli.main import clear_autocomplete_cache_callback

        with pytest.raises(typer.Exit) as excinfo:
            clear_autocomplete_cache_callback(True)
        # The escape hatch exits SUCCESSFULLY after clearing — bare Exit is 0.
        assert excinfo.value.exit_code == 0
        # rich hard-wraps long paths at the terminal width; unwrap before matching.
        return capsys.readouterr().out.replace("\n", "")

    def test_both_caches_removed(self, caches, capsys):
        main, sidecar = caches
        main.write_text("{}")
        sidecar.write_text("{}")

        out = self._run(capsys)

        assert not main.exists()
        assert not sidecar.exists(), "sidecar survived --clear-autocomplete-cache"
        assert "completion_cache.json" in out
        assert "remote_completion_cache.json" in out

    def test_sidecar_alone_is_removed_and_reported(self, caches, capsys):
        """The main cache being absent must not short-circuit the sidecar's removal."""
        _main, sidecar = caches
        sidecar.write_text("{}")

        out = self._run(capsys)

        assert not sidecar.exists()
        assert "remote_completion_cache.json" in out

    def test_nothing_to_remove_reports_the_main_path(self, caches, capsys):
        main, _sidecar = caches
        out = self._run(capsys)
        assert "No completion cache found" in out
        assert str(main) in out

    def test_caching_disabled_reports_xdir(self, monkeypatch, capsys):
        monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: None)
        out = self._run(capsys)
        assert "OTTO_XDIR" in out


# ── Project activation switches: the WIRING (-I / -E) ────────────────────────


class TestProjectSwitchWiring:
    """That -I/-E are CONNECTED, not merely that the parse helpers are correct.

    ``test_project_switches.py`` covers the three pure functions. Nothing there
    can tell whether the option is spelled ``-I``, whether Typer was actually
    given ``callback=parse_project_list``, whether the conflict check is still
    called, or whether the tuples reach the runtime context — every one of those
    can be deleted with that file still fully green. These drive the REAL root
    callback and the REAL ``ensure_lab_context`` through the ``_main_probe``
    leaf, so each of those seams has a witness.
    """

    @pytest.fixture(autouse=True)
    def _world_knows_these_names(self, monkeypatch):
        """Let discovery find every repo name these tests type.

        The preamble validates ``-I``/``-E`` against the discovered repo set
        and exits 2 on a name nothing declares
        (``otto.cli.invoke.validate_project_switches``). ``main_mocks`` leaves
        that set empty, so without this every test below would stop at the
        validator with exit 2 and witness none of the wiring it exists for.
        Names are the NORMALIZED spellings the validator compares against.
        """
        repos = [SimpleNamespace(name=n) for n in ("repo-a", "other-repo", "a", "b", "c")]
        monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: bootstrap_stub(repos))

    def _capture_root_options(self, monkeypatch):
        """Spy RootOptions, returning a dict that fills in with the built instance."""
        import otto.cli.invoke as invoke_mod

        captured: dict = {}
        real = invoke_mod.RootOptions

        def _spy(**kwargs):
            opts = real(**kwargs)
            captured["opts"] = opts
            return opts

        # main() imports RootOptions from .invoke inside its body, so the
        # attribute is resolved at call time and patching the source works.
        monkeypatch.setattr("otto.cli.invoke.RootOptions", _spy)
        return captured

    def _capture_context(self, monkeypatch):
        """Spy set_cli_context, still installing the real context so get_context works."""
        import otto.context as context_mod

        captured: dict = {}
        real_set = context_mod.set_cli_context

        def _spy(ctx):
            captured["ctx"] = ctx
            return real_set(ctx)

        monkeypatch.setattr("otto.context.set_cli_context", _spy)
        return captured

    def test_switches_reach_root_options_normalized(self, main_mocks, monkeypatch):
        """`-I`/`-E` are parsed by the callback and stored PEP-503-normalized.

        A user spelling is used on purpose: raw passthrough (a missing
        ``callback=``) and a renamed short option both fail here, and both are
        otherwise invisible.
        """
        captured = self._capture_root_options(monkeypatch)

        result = _invoke(["-I", "Repo.A", "-E", "other_repo"])

        assert result.exit_code == 0, result.output
        opts = captured["opts"]
        assert opts.include_projects == ("repo-a",)
        assert opts.exclude_projects == ("other-repo",)

    def test_long_spellings_reach_root_options_too(self, main_mocks, monkeypatch):
        captured = self._capture_root_options(monkeypatch)

        result = _invoke(["--include-projects", "a,b", "--exclude-projects", "c"])

        assert result.exit_code == 0, result.output
        assert captured["opts"].include_projects == ("a", "b")
        assert captured["opts"].exclude_projects == ("c",)

    def test_absent_switches_store_empty_tuples(self, main_mocks, monkeypatch):
        """The default is (), not None — Task 3 compares these tuples directly."""
        captured = self._capture_root_options(monkeypatch)

        result = _invoke([])

        assert result.exit_code == 0, result.output
        assert captured["opts"].include_projects == ()
        assert captured["opts"].exclude_projects == ()

    def test_switches_reach_the_runtime_context(self, main_mocks, monkeypatch):
        """ensure_lab_context must thread both tuples onto the installed OttoContext.

        This is the seam where everything the parse layer produced actually
        meets ``otto.config.scope.active``. Dropping the two kwargs leaves every
        other test in the suite green.
        """
        captured = self._capture_context(monkeypatch)

        result = _invoke(["-I", "Repo.A", "-E", "other_repo"])

        assert result.exit_code == 0, result.output
        ctx = captured["ctx"]
        assert ctx.include_projects == ("repo-a",)
        assert ctx.exclude_projects == ("other-repo",)

    def test_the_context_sees_the_switches_through_the_predicate(self, main_mocks, monkeypatch):
        """End to end: a name typed at -E makes scope.active() say False.

        Asserts the CONSEQUENCE rather than the field, so the wiring is pinned
        to the thing it exists for.
        """
        from otto.config import scope

        captured = self._capture_context(monkeypatch)

        result = _invoke(["-E", "other_repo"])

        assert result.exit_code == 0, result.output
        ctx = captured["ctx"]
        assert scope.active("other-repo", ctx) is False
        assert scope.switched_off("other-repo", ctx) is True
        assert scope.active("some-other-repo", ctx) is True

    def test_an_unknown_name_stops_the_real_cli_at_two(self, main_mocks):
        """The preamble really calls the validator — not just a fake ctx in a unit test.

        Every other test in this class only needs the validator to PASS, so
        deleting ``validate_project_switches(ctx)`` from ``command_preamble``
        leaves them all green. This one needs it to have RUN.
        """
        result = _invoke(["-E", "ghost"])

        assert result.exit_code == 2
        assert "no project 'ghost'" in result.output

    def test_show_lab_validates_the_names_too(self, main_mocks):
        """``--show-lab`` short-circuits in the root callback and gets the same check.

        It never reaches ``command_preamble``, so the branch carries its own
        call; without it a typo'd name would surface as a confusing lab dump.
        """
        result = runner.invoke(app, ["--lab", "test_lab", "-E", "ghost", "--show-lab"])

        assert result.exit_code == 2
        assert "no project 'ghost'" in result.output

    def _world_with_one_broken_repo(self, monkeypatch):
        """Install a discovered world where ``repo-a`` failed to load."""
        from otto.bootstrap import BootstrapError

        broken = SimpleNamespace(name="repo-a", sut_dir=Path("/repos/repo-a"), project_scope=None)
        monkeypatch.setattr(
            "otto.bootstrap.bootstrap",
            lambda: bootstrap_stub(
                [broken],
                errors=[BootstrapError(broken.sut_dir, "repo_a_init", ImportError("paramiko"))],
            ),
        )

    def test_an_excluded_repos_bootstrap_error_does_not_fail_the_run(self, main_mocks, monkeypatch):
        """The preamble hands *ctx* to the gate — otherwise this run would exit 1.

        Dropping the argument at that call site leaves every direct unit test
        of the gate green, because they pass their own ctx.
        """
        self._world_with_one_broken_repo(monkeypatch)

        result = _invoke(["-E", "repo-a"])

        assert result.exit_code == 0, result.output

    def test_show_lab_demotes_an_excluded_repos_error_as_well(self, main_mocks, monkeypatch):
        """The ``--show-lab`` branch passes *ctx* to the gate too, not just to the validator."""
        self._world_with_one_broken_repo(monkeypatch)

        result = runner.invoke(app, ["--lab", "test_lab", "-E", "repo-a", "--show-lab"])

        assert result.exit_code == 0, result.output

    def test_contradictory_switches_exit_two(self, main_mocks):
        """The conflict check still runs in the root callback (usage error = 2)."""
        result = _invoke(["-I", "repo-a", "-E", "repo-a"])

        assert result.exit_code == 2
        assert "repo-a" in result.output + result.stderr

    def test_whitespace_contradiction_exits_two_through_the_real_cli(self, main_mocks):
        """`-I "repo-a" -E " repo-a"` is one name twice — the CLI must refuse it.

        Only true while the parse callback strips before the check compares.
        """
        result = _invoke(["-I", "repo-a", "-E", " repo-a"])

        assert result.exit_code == 2
        assert "repo-a" in result.output + result.stderr
