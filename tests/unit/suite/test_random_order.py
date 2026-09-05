"""``--random / --no-random`` and ``--seed``: what the runner hands pytest, and the reproduce line.

pytest-randomly is a runtime dependency and pytest auto-loads it, so a
``run_suite`` with random order ON adds nothing for the plugin — the seed is
the only argument — and OFF unregisters it with ``-p no:randomly``. The
runner passes ``--no-header``, which hides the plugin's own seed line, so
otto's plugin logs the effective seed itself, phrased as the ``otto test``
flag that reproduces the order.
"""

import logging
import sys
import textwrap

import pytest

from otto.suite.run import RunOptions, run_suite


class _LibSuite:
    pass


@pytest.fixture(autouse=True)
def _no_repos(monkeypatch):
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)


@pytest.fixture
def captured_argv(monkeypatch):
    """Replace ``pytest.main`` with a recorder; the session never runs."""
    calls: list[list[str]] = []

    def fake_main(args, **_kw):
        calls.append(list(args))
        return 0

    monkeypatch.setattr("pytest.main", fake_main)
    return calls


class TestRunnerArgv:
    def test_default_is_random_with_no_seed_argument(self, tmp_path, captured_argv):
        run_suite(_LibSuite, output_dir=tmp_path)
        (argv,) = captured_argv
        assert "no:randomly" not in argv
        assert not any(a.startswith("--randomly-seed") for a in argv)

    def test_no_random_unregisters_the_plugin(self, tmp_path, captured_argv):
        run_suite(_LibSuite, output_dir=tmp_path, run_options=RunOptions(random_order=False))
        (argv,) = captured_argv
        assert argv[argv.index("no:randomly") - 1] == "-p"
        assert not any(a.startswith("--randomly-seed") for a in argv)

    def test_seed_is_forwarded(self, tmp_path, captured_argv):
        run_suite(_LibSuite, output_dir=tmp_path, run_options=RunOptions(seed=1234))
        (argv,) = captured_argv
        assert "--randomly-seed=1234" in argv
        assert "no:randomly" not in argv

    def test_run_options_defaults_mirror_the_cli(self):
        opts = RunOptions()
        assert opts.random_order is True
        assert opts.seed is None


def _write_suite(tmp_path, name):
    # Unique basename per test: both inner sessions run IN-PROCESS, and a
    # module already imported under one basename cannot be collected again
    # from a different directory ("import file mismatch"). Unique-per-test is
    # not enough on its own -- the same test running twice in one process gets
    # a fresh tmp_path under the same basename -- so _inner_session evicts the
    # module afterwards.
    suite = tmp_path / f"test_{name}.py"
    suite.write_text(
        textwrap.dedent(
            """
            def test_a():
                pass

            def test_b():
                pass
            """
        )
    )
    return suite


def _inner_session(suite_path, extra, plugin):
    from otto.suite.run import ASYNCIO_LOOP_ARGS

    try:
        return pytest.main(
            [
                "-s",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:playwright",
                "--override-ini",
                "addopts=",
                "-o",
                "asyncio_mode=auto",
                *ASYNCIO_LOOP_ARGS,
                *extra,
                str(suite_path),
            ],
            plugins=[plugin],
        )
    finally:
        # This in-process pytest.main() imports the generated suite as a
        # top-level module keyed by stem. Evict it so a second run of this
        # test in the same process (the unit-repeat isolation lane runs
        # `pytest --count=2 --repeat-scope=session`) imports a fresh module
        # from its own tmp_path instead of hitting "import file mismatch".
        sys.modules.pop(suite_path.stem, None)


class TestSeedIsAnnounced:
    def test_random_run_logs_the_reproduce_flag(self, tmp_path, caplog):
        from otto.suite.plugin import OttoPlugin

        suite = _write_suite(tmp_path, "randorder_announced")
        with caplog.at_level(logging.INFO, logger="otto.suite.plugin"):
            rc = _inner_session(suite, ["--randomly-seed=4242"], OttoPlugin())
        assert rc == 0
        lines = [r.getMessage() for r in caplog.records if "seed" in r.getMessage()]
        assert lines, "no seed line logged"
        assert "--seed 4242" in lines[0]

    def test_no_random_run_logs_nothing_about_a_seed(self, tmp_path, caplog):
        from otto.suite.plugin import OttoPlugin

        suite = _write_suite(tmp_path, "randorder_silent")
        with caplog.at_level(logging.INFO, logger="otto.suite.plugin"):
            rc = _inner_session(suite, ["-p", "no:randomly"], OttoPlugin())
        assert rc == 0
        assert not [r for r in caplog.records if "seed" in r.getMessage()]
