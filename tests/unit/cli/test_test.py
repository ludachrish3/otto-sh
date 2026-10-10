"""Unit tests for the ``otto test`` command's run flags.

Tests verify:
  - ``otto test --help`` lists the run flags and the ``test`` verb's options
  - the run directory is ``test/<timestamp>/``
  - type enforcement: Typer rejects invalid values before pytest runs
  - the run flags thread into the ``RunOptions`` handed to
    :func:`otto.suite.run.run_tests`, with their implications and refusals

The run engine itself (``run_tests`` calling ``pytest.main``) is exercised as
a library in ``tests/unit/suite/test_run_api.py``; the command's names, listing
and dry run in ``tests/unit/cli/test_test_command.py``.
"""

from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto import options
from otto.params import register_options
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests._fixtures.gitrepo import TmpGitRepo
from tests.unit.cli.conftest import _flat, _lib_ok_result, _repo_with_tickets_configured

runner = CliRunner()


RULE_TABLE = [
    # (cli flags, expected fields on the RunOptions handed to run_tests) — one row per rule
    #
    # The destination rows target a subdirectory, not tmp_path itself:
    # otto_test_cli's own output_dir (tmp_path / "otto-out") already lives
    # under tmp_path, so a bare tmp_path is never an empty destination.
    (["--cov-report-dir", "{tmp}/report"], {"cov_report": True, "cov": True}),
    (["--cov-tickets-json", "{tmp}/t.json"], {"cov_report": True, "cov": True}),
    (["--cov-report"], {"cov": True}),
    (["--cov-dir", "{tmp}/cov"], {"cov": True}),
    (["--monitor-output", "{tmp}/m.json"], {"monitor": True}),
    (["--monitor-hosts", "router"], {"monitor": True}),
]

CONTRADICTION_TABLE = [
    (["--no-cov", "--cov-dir", "{tmp}"], "--no-cov cannot be combined with --cov-dir"),
    (["--no-cov", "--cov-report"], "--no-cov cannot be combined with"),
    (["--no-random", "--seed", "7"], "--seed cannot be combined with --no-random"),
]


class TestRulesAreTheLibrarys:
    @pytest.mark.parametrize(("flags", "expected"), RULE_TABLE)
    def test_the_cli_hands_run_tests_what_the_class_implies(
        self, capture_cov, tmp_path, flags, expected, monkeypatch
    ):
        patch_bootstrap(monkeypatch, [_repo_with_tickets_configured()])
        args = [f.format(tmp=tmp_path) for f in flags]
        exit_code, run_options, output = capture_cov(args)
        assert exit_code == 0, output
        for name, value in expected.items():
            assert run_options[name] == value, name

    @pytest.mark.parametrize(("flags", "message"), CONTRADICTION_TABLE)
    def test_a_contradiction_exits_2_in_flag_spelling(self, capture_cov, tmp_path, flags, message):
        args = [f.format(tmp=tmp_path) for f in flags]
        exit_code, run_options, output = capture_cov(args)
        assert exit_code == 2
        assert message in output
        assert run_options == {}

    def test_the_leaf_passes_parsed_flags_through_unchanged(
        self, otto_test_cli, monkeypatch, tmp_path
    ):
        """No rule lives in the CLI: the constructor receives exactly the parsed params.

        Asserts the WHOLE kwargs dict the CLI hands the constructor, not a
        subset — a subset check would still pass if the CLI started setting
        an extra field of its own (e.g. ``fields["cov_clean"] = False``),
        which is exactly the kind of drift this test exists to catch.
        """
        from otto.cli.test import _RUN_FIELD_NAMES, _run_params
        from otto.suite import run as run_module

        seen: dict = {}
        real = run_module.RunOptions

        def spy(**kw):
            seen.update(kw)
            return real(**kw)

        monkeypatch.setattr(run_module, "RunOptions", spy)
        monkeypatch.setattr("otto.suite.run.run_tests", lambda names, **kw: _lib_ok_result())
        result = otto_test_cli(
            ["test", "--cov-report-dir", str(tmp_path / "r"), "--iterations", "2", "test_x"]
        )
        assert result.exit_code == 0, result.output
        defaults = {p.name: p.default for p in _run_params() if p.name in _RUN_FIELD_NAMES}
        assert seen == defaults | {"cov_report_dir": tmp_path / "r", "iterations": 2}


# ── Help behaviour ────────────────────────────────────────────────────────────


class TestTestHelp:
    def test_help_flag(self, otto_test_cli):
        assert otto_test_cli(["test", "--help"], lab=None).exit_code == 0

    def test_short_help_flag(self, otto_test_cli):
        assert otto_test_cli(["test", "-h"], lab=None).exit_code == 0

    def test_help_shows_runner_options(self, otto_test_cli):
        result = otto_test_cli(["test", "--help"], lab=None)
        assert result.exit_code == 0
        for flag in ("--iterations", "--duration", "--threshold", "--results", "--markers"):
            assert flag in result.output

    def test_monitor_hosts_help_states_fullmatch_semantics(self, otto_test_cli):
        """``--monitor-hosts`` narrows the fleet the same way ``otto monitor --hosts`` does.

        It reaches the identical ``all_hosts(pattern=...)`` seam, so its help
        has to describe the identical matching rule — a second option still
        promising ``re.search`` is how a reader learns the wrong semantics from
        the tool itself. Asserted on the RENDERED table, widened so truncation
        cannot masquerade as a missing phrase.
        """
        result = runner.invoke(
            __import__("otto.cli.test", fromlist=["test_app"]).test_app,
            ["--help"],
            env={"COLUMNS": "300"},
        )
        assert result.exit_code == 0
        rendered = " ".join(result.output.split())
        assert "--monitor-hosts" in rendered
        assert "(fullmatch)" in rendered

    def test_a_verb_option_and_its_help_show_in_help(self, otto_test_cli):
        """A field annotated with ``typer.Option(help=...)`` shows that text in --help."""

        @options
        class DeviceOpts:
            device_type: Annotated[str, typer.Option(help="Kind of device under test.")] = "router"

        register_options(DeviceOpts, verbs=["test"])
        result = otto_test_cli(["test", "--help"], lab=None)
        assert result.exit_code == 0
        rendered = " ".join(result.output.replace("│", " ").split())
        assert "--device-type" in rendered
        assert "Kind of device under test." in rendered

    def test_an_inherited_verb_option_field_shows_its_help(self, otto_test_cli):
        @options
        class ParentOpts:
            device_type: Annotated[str, typer.Option(help="Inherited device help.")] = "router"

        @options
        class ChildOpts(ParentOpts):
            firmware: Annotated[str, typer.Option(help="Firmware help.")] = "latest"

        register_options(ChildOpts, verbs=["test"])
        result = otto_test_cli(["test", "--help"], lab=None)
        rendered = " ".join(result.output.replace("│", " ").split())
        assert "Inherited device help." in rendered
        assert "Firmware help." in rendered

    def test_an_option_registered_only_for_run_is_not_a_test_flag(self, otto_test_cli):
        @options
        class RunOnly:
            only_run: str = "x"

        register_options(RunOnly, verbs=["run"])
        assert "--only-run" not in otto_test_cli(["test", "--help"], lab=None).output


# ── The run directory ─────────────────────────────────────────────────────────


class TestRunDirectory:
    def test_the_run_directory_is_test_and_a_timestamp(self):
        """The leaf's name is the spec's name, so the run dir is ``test/<timestamp>/``.

        Dispatched through the root ``app`` (which wraps leaves with the
        preamble). ``ensure_cli_session`` / ``ensure_lab_context`` are stubbed
        to isolate the output-dir naming (``create_output_dir('test', None)``).
        """
        from otto.cli.main import app

        with (
            patch("otto.cli.invoke.ensure_cli_session"),
            patch("otto.cli.invoke.ensure_lab_context"),
            patch("otto.logger.management.create_output_dir") as p_create,
            patch("otto.suite.run.run_tests", new=lambda *a, **k: _lib_ok_result()),
        ):
            runner.invoke(app, ["--lab", "x", "test", "test_x"])

        p_create.assert_called_once_with("test", None)


# ── Type enforcement ──────────────────────────────────────────────────────────


class TestTypeEnforcement:
    def test_invalid_iterations_rejected(self, capture_cov):
        exit_code, run_options, _output = capture_cov(["--iterations", "oops"])
        assert exit_code == 2
        assert run_options == {}

    def test_invalid_verb_option_rejected(self, otto_test_cli, monkeypatch):
        @options
        class CountOpts:
            count: int = 1

        register_options(CountOpts, verbs=["test"])
        ran: list = []
        monkeypatch.setattr("otto.suite.run.run_tests", lambda *a, **k: ran.append(a))
        result = otto_test_cli(["test", "test_x", "--count", "not-a-number"])
        assert result.exit_code == 2
        assert ran == []

    def test_verb_option_defaults_applied_when_omitted(self, otto_test_cli, monkeypatch):
        @options
        class RetryOpts:
            max_retries: int = 9

        register_options(RetryOpts, verbs=["test"])
        captured: dict = {}

        def fake_run_tests(names, **kw):
            captured["options"] = kw["options"]
            return _lib_ok_result()

        monkeypatch.setattr("otto.suite.run.run_tests", fake_run_tests)
        assert otto_test_cli(["test", "test_x"]).exit_code == 0
        (opts,) = captured["options"]
        assert opts.max_retries == 9

    def test_an_unrelated_value_error_propagates(self, otto_test_cli, monkeypatch):
        """A generic ValueError from the pipeline is NOT relabeled 'No tests matched'."""

        def boom(*_a, **_k):
            raise ValueError("pipeline exploded")

        monkeypatch.setattr("otto.suite.run.run_tests", boom)
        result = otto_test_cli(["test", "test_x"])
        assert isinstance(result.exception, ValueError)
        assert "pipeline exploded" in str(result.exception)
        assert "No tests matched" not in result.output


# ── Run flags thread into RunOptions ──────────────────────────────────────────


class TestRunOptionsForwarded:
    def test_iterations_forwarded(self, capture_cov):
        assert capture_cov(["--iterations", "5"])[1]["iterations"] == 5

    def test_random_order_defaults_on_with_no_seed(self, capture_cov):
        run_options = capture_cov([])[1]
        assert run_options["random_order"] is True
        assert run_options["seed"] is None

    def test_no_random_forwarded(self, capture_cov):
        assert capture_cov(["--no-random"])[1]["random_order"] is False

    def test_seed_forwarded(self, capture_cov):
        run_options = capture_cov(["--seed", "7"])[1]
        assert run_options["seed"] == 7
        assert run_options["random_order"] is True

    def test_seed_with_no_random_is_a_usage_error(self, capture_cov):
        """A seed with nothing to seed is a contradiction: refuse, don't guess."""
        exit_code, run_options, output = capture_cov(["--no-random", "--seed", "7"])
        assert exit_code == 2
        assert "--seed" in output
        assert "--no-random" in output
        assert run_options == {}

    def test_markers_forwarded(self, capture_cov):
        assert capture_cov(["--markers", "not integration"])[1]["markers"] == "not integration"

    def test_defaults_when_omitted(self, capture_cov):
        run_options = capture_cov([])[1]
        assert run_options["markers"] == ""
        assert run_options["iterations"] == 0
        assert run_options["duration"] == 0
        assert run_options["threshold"] == 100.0
        assert run_options["results"] == ""
        # Monitor defaults: disabled, default interval, no override path / regex.
        assert run_options["monitor"] is False
        assert run_options["monitor_interval"] == 5.0
        assert run_options["monitor_output"] is None
        assert run_options["monitor_hosts"] is None

    def test_monitor_flag_forwarded(self, capture_cov):
        assert capture_cov(["--monitor"])[1]["monitor"] is True

    def test_monitor_options_forwarded(self, capture_cov, tmp_path):
        out = tmp_path / "m.json"
        run_options = capture_cov(
            [
                "--monitor",
                "--monitor-interval",
                "2",
                "--monitor-output",
                str(out),
                "--monitor-hosts",
                "router|switch",
            ]
        )[1]
        assert run_options["monitor"] is True
        assert run_options["monitor_interval"] == 2.0
        assert run_options["monitor_output"] == out
        assert run_options["monitor_hosts"] == "router|switch"

    def test_monitor_implied_by_output_or_hosts(self, capture_cov):
        """--monitor-output or --monitor-hosts alone should imply --monitor."""
        assert capture_cov(["--monitor-hosts", "router"])[1]["monitor"] is True


# ── --cov-dir option (destination override + validation) ─────────────────────


class TestCovDirOption:
    def test_no_flags_leaves_coverage_on_auto(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov([])
        assert exit_code == 0, f"output={output!r}"
        # Neither --cov nor --no-cov: auto — the decision is the run's, taken
        # from the lab's instrumentation (otto.suite.run.resolve_coverage).
        assert ctx_obj["cov"] is None
        assert ctx_obj["cov_dir"] is None

    def test_no_cov_forces_off(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov(["--no-cov"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is False

    def test_no_cov_with_cov_dir_is_a_usage_error(self, capture_cov, tmp_path):
        exit_code, _ctx_obj, output = capture_cov(["--no-cov", "--cov-dir", str(tmp_path / "c")])
        assert exit_code == 2, f"output={output!r}"  # typer/click usage error
        assert "--no-cov" in output
        assert "--cov-dir" in output

    def test_no_cov_with_cov_report_is_a_usage_error(self, capture_cov):
        exit_code, _ctx_obj, output = capture_cov(["--no-cov", "--cov-report"])
        assert exit_code == 2, f"output={output!r}"  # typer/click usage error
        assert "--no-cov" in output
        assert "--cov-report" in output

    def test_cov_flag_only_uses_default_dir(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov(["--cov"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] is None

    def test_cov_dir_implies_cov_and_records_path(self, capture_cov, tmp_path):
        target = tmp_path / "custom"
        exit_code, ctx_obj, output = capture_cov(["--cov-dir", str(target)])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_with_cov_dir_records_path(self, capture_cov, tmp_path):
        target = tmp_path / "both"
        exit_code, ctx_obj, output = capture_cov(
            ["--cov", "--cov-dir", str(target)],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()

    def test_cov_dir_nonempty_without_overwrite_aborts(self, capture_cov, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "leftover.txt").write_text("stale")

        exit_code, ctx_obj, output = capture_cov(["--cov-dir", str(target)])
        assert exit_code != 0
        assert ctx_obj == {}
        assert "pass --overwrite-cov-dir to clear it" in _flat(output)
        # Stale file preserved when we refuse to proceed.
        assert (target / "leftover.txt").exists()

    def test_overwrite_cov_dir_clears_contents(self, capture_cov, tmp_path):
        target = tmp_path / "to_clear"
        target.mkdir()
        (target / "leftover.txt").write_text("stale")
        (target / "sub").mkdir()
        (target / "sub" / "nested.txt").write_text("more stale")

        exit_code, ctx_obj, output = capture_cov(
            ["--cov-dir", str(target), "--overwrite-cov-dir"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_dir"] == target.resolve()
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_cov_dir_pointing_at_file_fails(self, capture_cov, tmp_path):
        target = tmp_path / "not_a_dir"
        target.write_text("i am a file")

        exit_code, _, output = capture_cov(["--cov-dir", str(target)])
        assert exit_code != 0
        assert "--cov-dir" in output
        assert "is a file" in output or "not a directory" in output


class TestInRunReportGeneration:
    """``otto test --cov-report`` renders via the collection-model report path.

    The fetch/metadata/capture collection tail itself now lives in
    ``otto.coverage.collect`` (exercised in ``tests/unit/cov/test_collect.py``)
    and is stubbed out here; this class's subject is the in-run HTML report
    block of ``otto.suite.run._post_run_coverage``.
    """

    @pytest.fixture
    def sut_repo(self, tmp_path):
        """A real tmp_path git repo standing in for the SUT checkout."""
        repo = TmpGitRepo(tmp_path / "sut")
        repo.write("f.c", "int a;\nint b;\n")
        repo.commit("init")
        return repo.root

    def test_in_run_report_uses_configured_tiers(self, tmp_path, sut_repo, monkeypatch):
        """``otto test --cov-report`` must render via the collection-model path,
        not the legacy system-only one: the store.json the in-run report writes
        carries the settings-declared tier precedence."""
        import asyncio
        import json

        from otto.suite.run import RunOptions, _post_run_coverage

        # cov_dir/cov_report_dir now force cov=True too (construction-time rule),
        # so the fetch machinery runs; stub it out — this test's subject is the
        # report block, not collection.
        monkeypatch.setattr("otto.coverage.collect.collect_coverage", AsyncMock())

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        report_dir = tmp_path / "cov_report"

        cov_config = {
            "tiers": {
                "unit": {"kind": "unit", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
                "manual": {"kind": "manual", "precedence": 3},
            }
        }
        repo = MagicMock()
        repo.sut_dir = sut_repo
        repo.name = "repo"
        repo.settings = {"coverage": cov_config}

        opts = RunOptions(
            cov_report=True,
            cov_dir=cov_dir,
            cov_report_dir=report_dir,
        )
        asyncio.run(_post_run_coverage([repo], tmp_path / "log", opts))

        store_json = json.loads((report_dir / "store.json").read_text())
        assert store_json["tier_order"] == ["unit", "system", "manual"]


# ── --cov-report option (report generation alongside collection) ─────────────


class TestCovReportOption:
    def test_no_flags_disables_report(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is False
        assert ctx_obj["cov_report_dir"] is None

    def test_cov_report_flag_enables_report_and_implies_cov(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov(["--cov-report"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_report_dir"] is None

    def test_short_r_flag_enables_report(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov(["-r"])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True

    def test_cov_report_dir_implies_cov_report_and_cov(self, capture_cov, tmp_path):
        target = tmp_path / "report"
        exit_code, ctx_obj, output = capture_cov(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True
        assert ctx_obj["cov_report_dir"] == target.resolve()
        # Validation creates the directory eagerly.
        assert target.is_dir()

    def test_cov_report_dir_nonempty_without_overwrite_aborts(self, capture_cov, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "stale.html").write_text("stale")
        exit_code, ctx_obj, output = capture_cov(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code != 0
        assert ctx_obj == {}
        assert "pass --overwrite-cov-report-dir to clear it" in _flat(output)
        assert (target / "stale.html").exists()

    def test_dry_run_refuses_a_non_empty_report_dir_without_touching_it(
        self, otto_test_cli, tmp_path
    ):
        (tmp_path / "stale.html").write_text("stale")
        result = otto_test_cli(["-n", "test", "--cov-report-dir", str(tmp_path), "test_x"])
        assert result.exit_code == 2
        assert "pass --overwrite-cov-report-dir" in _flat(result.output)
        assert (tmp_path / "stale.html").read_text() == "stale"

    def test_overwrite_cov_report_dir_clears_contents(self, capture_cov, tmp_path):
        target = tmp_path / "to_clear"
        target.mkdir()
        (target / "stale.html").write_text("stale")
        (target / "sub").mkdir()
        (target / "sub" / "nested.html").write_text("nested")

        exit_code, ctx_obj, output = capture_cov(
            ["--cov-report-dir", str(target), "--overwrite-cov-report-dir"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov_report_dir"] == target.resolve()
        assert list(target.iterdir()) == []

    def test_project_name_recorded(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov(
            ["--cov-report", "--project-name", "My App"],
        )
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["project_name"] == "My App"

    def test_project_name_default(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["project_name"] == "Coverage Report"

    def test_cov_report_dir_pointing_at_file_fails(self, capture_cov, tmp_path):
        target = tmp_path / "not_a_dir"
        target.write_text("i am a file")
        exit_code, _, output = capture_cov(
            ["--cov-report-dir", str(target)],
        )
        assert exit_code != 0
        assert "--cov-report-dir" in output
        assert "is a file" in output or "not a directory" in output


# ── --cov-tickets-json option ─────────────────────────────────────────────────


class TestCovTicketsJsonOption:
    def test_no_flag_leaves_cov_tickets_json_none(self, capture_cov):
        exit_code, ctx_obj, output = capture_cov([])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_tickets_json"] is None

    def test_cov_tickets_json_recorded(self, capture_cov, tmp_path, monkeypatch):
        patch_bootstrap(monkeypatch, [_repo_with_tickets_configured()])
        target = tmp_path / "tickets.json"
        exit_code, ctx_obj, output = capture_cov(["--cov-tickets-json", str(target)])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_tickets_json"] == target

    def test_cov_tickets_json_implies_cov_report_and_cov(self, capture_cov, tmp_path, monkeypatch):
        """Mirrors --cov-report-dir: naming a tickets export with no other
        --cov-report flag still implies --cov-report (and --cov) — a bare
        --cov-tickets-json PATH must not silently do nothing."""
        patch_bootstrap(monkeypatch, [_repo_with_tickets_configured()])
        target = tmp_path / "tickets.json"
        exit_code, ctx_obj, output = capture_cov(["--cov-tickets-json", str(target)])
        assert exit_code == 0, f"output={output!r}"
        assert ctx_obj["cov_report"] is True
        assert ctx_obj["cov"] is True

    def test_cov_tickets_json_without_coverage_tickets_config_fails_fast(
        self, capture_cov, tmp_path, monkeypatch
    ):
        """Misconfiguration (flag given, no [coverage.tickets] anywhere) must
        fail BEFORE the (possibly long) test run starts, not silently
        warn-and-skip after it finishes — otherwise a CI pipeline wiring
        --cov-tickets-json gets exit 0, no file, and a warning nobody reads."""
        patch_bootstrap(monkeypatch, [])
        target = tmp_path / "tickets.json"
        exit_code, ctx_obj, output = capture_cov(["--cov-tickets-json", str(target)])
        assert exit_code == 2
        assert ctx_obj == {}
        assert "--cov-tickets-json requires [coverage.tickets] to be configured" in _flat(output)

    def test_cov_tickets_json_with_coverage_but_no_tickets_table_fails_fast(
        self, capture_cov, tmp_path, monkeypatch
    ):
        """[coverage] configured but with no [coverage.tickets] sub-table is
        the same knowable-up-front misconfiguration as no [coverage] at
        all."""
        repo = MagicMock()
        repo.settings = {"coverage": {"tiers": {"system": {"kind": "e2e", "precedence": 1}}}}
        patch_bootstrap(monkeypatch, [repo])
        target = tmp_path / "tickets.json"
        exit_code, ctx_obj, output = capture_cov(["--cov-tickets-json", str(target)])
        assert exit_code != 0
        assert ctx_obj == {}
        assert "[coverage.tickets]" in output
