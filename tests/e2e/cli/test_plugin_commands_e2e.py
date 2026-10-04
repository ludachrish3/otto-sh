"""Hostless e2e: third-party top-level commands + bootstrap containment."""

from pathlib import Path

import pytest

from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import PROJECT_ROOT, REPO_E2E, assert_no_output_dir, run_otto

REPO_BROKEN = PROJECT_ROOT / "tests" / "repo_broken"

pytestmark = pytest.mark.hostless


class TestPluginCommands:
    def test_plugin_leaf_dispatches(self, tmp_path: Path) -> None:
        r = run_otto(["e2e-hello", "--who", "otto"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "hello otto" in r.stdout
        assert_no_output_dir(tmp_path)  # lab_free + no output dir declared

    def test_plugin_group_dispatches(self, tmp_path: Path) -> None:
        r = run_otto(["e2etool", "ping"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "pong" in r.stdout

    def test_plugin_commands_listed_in_root_help(self, tmp_path: Path) -> None:
        cold = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert cold.returncode == 0
        assert "e2e-hello" in cold.stdout
        assert "e2etool" in cold.stdout
        # SECOND run, same per-test OTTO_HOME: served from the names section,
        # not a fresh bootstrap. The WARM screen must keep the @cli_command
        # leaf, not just the group — a collector that misclassifies decorated
        # leaves as built-ins passes the cold run and fails only here.
        warm = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert warm.returncode == 0
        assert "e2e-hello" in warm.stdout
        assert "e2etool" in warm.stdout


class TestBootstrapContainment:
    def test_broken_repo_degrades_help_with_framed_warning(self, tmp_path: Path) -> None:
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode == 0  # help still renders
        # A test file is never imported to build help (or the cache it seeds):
        # test names come from a static parse, and nothing registers from one.
        assert "failed to load test_syntax_error.py" not in r.stderr
        assert "run" in r.stdout  # first-party intact

    # A broken TEST file is only ever met by a pytest collection, which `otto
    # test` runs; every other command neither pays for it nor is blocked by it.

    def test_broken_test_file_does_not_block_unrelated_dispatch(self, tmp_path: Path) -> None:
        # `-R` + a lab the fixture declares, so the run completes without a
        # host or a reservation check and the exit code is the dispatch's own.
        r = run_otto(
            ["run", "noop"],
            xdir=tmp_path,
            sut_dirs=f"{REPO_E2E},{REPO_BROKEN}",
            lab="unix",
            extra_argv_prefix=["-R"],
        )
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "Cannot run commands while a repo fails to load" not in out
        assert "failed to load test_syntax_error.py" not in out

    def test_broken_test_file_does_not_block_show_lab_or_list_hosts(self, tmp_path: Path) -> None:
        for flag in ("--show-lab", "--list-hosts"):
            r = run_otto([flag], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
            assert "Cannot run commands while a repo fails to load" not in r.stdout + r.stderr, flag

    def test_broken_test_file_does_not_block_schema_export(self, tmp_path: Path) -> None:
        r = run_otto(
            ["schema", "export", "--out", str(tmp_path / "schema")],
            xdir=tmp_path,
            sut_dirs=f"{REPO_E2E},{REPO_BROKEN}",
        )
        assert r.returncode == 0, r.stdout + r.stderr

    def test_broken_test_file_is_a_collection_error_the_run_survives(self, tmp_path: Path) -> None:
        """The broken file is logged and the named tests still run; the run exits 1, as pytest's.

        Every repo's session collects its whole tree, since any repo may hold
        a name (#592), so ``repo_broken``'s collection error is the run's too.
        """
        r = run_otto(
            ["test", "TestE2EFixture"],
            xdir=tmp_path,
            sut_dirs=f"{REPO_E2E},{REPO_BROKEN}",
            lab="unix",
            extra_argv_prefix=["-R"],
        )
        out = r.stdout + r.stderr
        assert r.returncode == 1, out
        assert "1 passed" in out, out
        assert "test_syntax_error.py" in out
        assert "Cannot run commands while a repo fails to load" not in out

    def test_broken_test_file_does_not_block_otto_test_help(self, tmp_path: Path) -> None:
        r = run_otto(["test", "--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "failed to load test_syntax_error.py" not in r.stderr, r.stderr


class TestDiscoveryContainment:
    """Phase-1 containment: malformed config DATA degrades like broken user CODE."""

    @pytest.fixture
    def bad_toml_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo_bad_toml"
        (repo / ".otto").mkdir(parents=True)
        # The unparseable settings.toml IS this fixture's subject — make_sut_repo
        # only ever emits well-formed TOML, so the raw write has to stay.
        (repo / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML
            "this is [not valid toml\n",
        )
        return repo

    def test_malformed_settings_degrades_help_with_framed_warning(
        self, tmp_path: Path, bad_toml_repo: Path
    ) -> None:
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{bad_toml_repo}")
        assert r.returncode == 0  # help still renders
        assert "Traceback" not in r.stderr
        assert "settings.toml" in r.stderr  # framed warning names the culprit
        assert "run" in r.stdout  # first-party intact
        assert "e2e-hello" in r.stdout  # healthy repo's plugins intact

    def test_malformed_settings_fails_real_dispatch_loud(
        self, tmp_path: Path, bad_toml_repo: Path
    ) -> None:
        r = run_otto(["run", "noop"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{bad_toml_repo}")
        assert r.returncode != 0
        assert "settings.toml" in r.stderr + r.stdout

    def test_missing_sut_dir_fails_clean_one_liner(self, tmp_path: Path) -> None:
        # Env-level failure: nothing user-specific can load, so there is no
        # "degraded help" to offer — fail loud but CLEAN (no traceback).
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=tmp_path / "nope")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "does not exist" in r.stderr

    def test_unscoped_provider_repo_refuses_clean(self, tmp_path: Path) -> None:
        """D2's refusal is a message to act on, so it must not arrive under a traceback.

        The check RAISES out of ``bootstrap()`` rather than joining the
        contained errors above, which puts it on a path where nothing had been
        catching it — the console entry printed a stack trace with the TOML
        block the user is meant to paste buried at the bottom of it.
        """
        repo = make_sut_repo(
            tmp_path / "unscoped",
            name="unscoped",
            extra='libs = ["lib"]\ninit = ["unscoped_init"]\n',
            files={
                "lib/unscoped_init.py": (
                    "from otto.host.product import register_product_provider\n\n"
                    "register_product_provider(lambda host: [])\n"
                )
            },
        )
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=repo)
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "unscoped" in r.stderr
        assert 'lab_patterns = [".*"]' in r.stderr


class TestOttoErrorFraming:
    """An OttoError that reaches the CLI boundary is a message, not a stack.

    The leaf here (``e2e-raises``) deliberately does NOT catch its own error,
    standing in for the class of bug that shipped twice for real: ``otto
    monitor`` and ``otto cov`` both let ``EmptySelectionError`` escape and
    tracebacked at whoever typed the command.
    """

    def test_an_uncaught_otto_error_is_framed_not_tracebacked(self, tmp_path: Path) -> None:
        r = run_otto(["e2e-raises"], xdir=tmp_path, sut_dirs=REPO_E2E)

        out = r.stdout + r.stderr
        assert r.returncode == 1
        assert "the lab is on fire" in out
        assert "Traceback (most recent call last)" not in out
        assert "OttoError" not in out  # the class name is otto's business, not the user's

    def test_the_stack_is_one_env_var_away(self, tmp_path: Path) -> None:
        """Framing must not DESTROY the traceback — DEBUG still prints it.

        A maintainer chasing an OttoError raised from somewhere it has no
        business being needs the frames, so the boundary demotes the stack
        rather than dropping it. Without this the fix would trade one bad
        failure mode for another.

        Written to stderr directly, not via ``logger.debug``: otto's log sinks
        belong to a RUN, and a lab-free leaf like this one never opens any —
        the logging route printed nothing at all here (measured), which is
        what a promise of "the stack is one env var away" must not do.
        """
        r = run_otto(
            ["e2e-raises"],
            xdir=tmp_path,
            sut_dirs=REPO_E2E,
            extra_env={"OTTO_LOG_LEVEL": "DEBUG"},
        )

        out = r.stdout + r.stderr
        assert r.returncode == 1
        assert "Traceback (most recent call last)" in out
        assert "plugin_commands.py" in out  # the frame that actually raised
        # AND still framed. Asserting the stack alone would pass against a
        # boundary that does not frame at ALL — an unhandled OttoError prints a
        # traceback too, so "there is a stack" is true either way. The
        # lower-case `error:` prefix comes only from print_error; the crash
        # route spells it `otto.errors.OttoError: ...`.
        assert "error: the lab is on fire" in out

    def test_the_flag_spelling_of_the_knob_arms_the_stack_too(self, tmp_path: Path) -> None:
        """``--log-level debug`` must mean what ``OTTO_LOG_LEVEL=DEBUG`` means.

        They are two spellings of ONE knob, and a user who typed the flag and
        got no traceback would reasonably conclude otto had none to give. The
        flag reaches the ``otto`` logger via init_cli_logging rather than the
        environment, so the boundary has to ask both.
        """
        r = run_otto(
            ["--log-level", "debug", "e2e-raises"],
            xdir=tmp_path,
            sut_dirs=REPO_E2E,
        )

        out = r.stdout + r.stderr
        assert r.returncode == 1
        assert "Traceback (most recent call last)" in out
        assert "plugin_commands.py" in out
        assert "error: the lab is on fire" in out  # framed, not merely crashed

    def test_a_working_command_is_untouched(self, tmp_path: Path) -> None:
        """The frame must be invisible to every command that does not fail."""
        r = run_otto(["e2e-hello", "--who", "otto"], xdir=tmp_path, sut_dirs=REPO_E2E)

        assert r.returncode == 0, r.stderr
        assert "hello otto" in r.stdout
