"""
Unit tests for ``otto.suite.suite``.

Tests verify:
  - ``_sanitize_node_name`` replaces filesystem-unsafe characters
  - The two autouse fixtures (``_otto_test_dir``,
    ``_otto_monitor_events``) work correctly when split from the former
    monolithic ``_test_lifecycle``
  - ``@pytest.mark.parametrize`` produces distinct ``testDir`` per parameter
  - ``suite_options`` fixture injection works in test method parameters
  - ``teardown_method`` is called after each test
  - ``expect()`` records non-fatal failures without stopping execution
"""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import otto.suite.suite as suite_module
from otto.suite.plugin import OttoPlugin
from otto.suite.register import OttoOptionsPlugin
from otto.suite.suite import _sanitize_node_name


# ── _sanitize_node_name ──────────────────────────────────────────────────────

class TestSanitizeNodeName:

    def test_brackets_replaced(self):
        assert _sanitize_node_name("test_foo[router-True]") == "test_foo_router-True_"

    def test_slashes_replaced(self):
        assert _sanitize_node_name("test/foo") == "test_foo"

    def test_multiple_unsafe_chars(self):
        assert _sanitize_node_name('a[b]<c>d:e"f|g?h*i\\j/k') == "a_b__c_d_e_f_g_h_i_j_k"

    def test_plain_name_unchanged(self):
        assert _sanitize_node_name("test_simple_name") == "test_simple_name"

    def test_empty_string(self):
        assert _sanitize_node_name("") == ""

    def test_hyphens_and_underscores_preserved(self):
        assert _sanitize_node_name("test_foo-bar_baz") == "test_foo-bar_baz"


# ── Inner pytest session helpers ─────────────────────────────────────────────

def _run_inner_pytest(test_file: Path, tmp_path: Path,
                      options: object | None = None) -> int:
    """Run an inner pytest session with OttoPlugin + OttoOptionsPlugin."""
    mock_logger = MagicMock()
    mock_logger.output_dir = tmp_path

    with patch.object(suite_module, "logger", mock_logger):
        exit_code = pytest.main(
            [str(test_file), "-o", "asyncio_mode=auto",
             "-o", "asyncio_default_fixture_loop_scope=function",
             "--no-cov", "--override-ini", "addopts=", "-x"],
            plugins=[OttoPlugin(), OttoOptionsPlugin(options)],
        )
    return exit_code


# ── Autouse fixtures ─────────────────────────────────────────────────────────

class TestOttoTestDir:

    def test_test_dir_created_per_test(self, tmp_path: Path) -> None:
        """Each test gets a unique testDir under suiteDir/tests/."""
        capture_file = tmp_path / "dirs.txt"
        test_file = tmp_path / "test_dirs.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestDirs(OttoSuite):
    async def test_alpha(self) -> None:
        CAPTURE.write_text(str(self.testDir))

    async def test_beta(self) -> None:
        with CAPTURE.open("a") as f:
            f.write("\\n" + str(self.testDir))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = capture_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert lines[0] != lines[1]
        assert "test_alpha" in lines[0]
        assert "test_beta" in lines[1]

    def test_parametrized_names_sanitized(self, tmp_path: Path) -> None:
        """Parametrized test names have brackets replaced in testDir."""
        capture_file = tmp_path / "param_dirs.txt"
        test_file = tmp_path / "test_param_dirs.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParamDirs(OttoSuite):
    @pytest.mark.parametrize("val", ["a", "b"])
    async def test_param(self, val: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(str(self.testDir) + "\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = [l for l in capture_file.read_text().strip().split("\n") if l]
        assert len(lines) == 2
        # Brackets should be sanitized
        for line in lines:
            assert "[" not in line
            assert "]" not in line


# ── suite_options fixture ────────────────────────────────────────────────────

class TestSuiteOptionsFixture:

    def test_suite_options_injected_via_fixture(self, tmp_path: Path) -> None:
        """Tests can request suite_options as a fixture parameter."""
        @dataclass
        class Opts:
            device_type: str = "router"

        opts = Opts(device_type="switch")
        capture_file = tmp_path / "opts.txt"
        test_file = tmp_path / "test_opts.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestOpts(OttoSuite):
    async def test_get_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(suite_options.device_type)
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=opts)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "switch"

    def test_suite_options_none_when_no_plugin_options(self, tmp_path: Path) -> None:
        """suite_options is None when OttoOptionsPlugin has no options."""
        capture_file = tmp_path / "none_opts.txt"
        test_file = tmp_path / "test_none_opts.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestNoneOpts(OttoSuite):
    async def test_none_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(str(suite_options))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=None)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "None"


# ── teardown_method ──────────────────────────────────────────────────────────

class TestTeardownMethod:

    def test_teardown_method_called(self, tmp_path: Path) -> None:
        """teardown_method() is called after each test."""
        capture_file = tmp_path / "teardown.txt"
        test_file = tmp_path / "test_teardown.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestTeardown(OttoSuite):
    def teardown_method(self, method=None) -> None:
        with CAPTURE.open("a") as f:
            f.write("torn_down\\n")

    async def test_one(self) -> None:
        assert True

    async def test_two(self) -> None:
        assert True
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = [l for l in capture_file.read_text().strip().split("\n") if l]
        assert len(lines) == 2
        assert all(l == "torn_down" for l in lines)


# ── Parametrize ──────────────────────────────────────────────────────────────

class TestParametrize:

    def test_parametrize_runs_all_variants(self, tmp_path: Path) -> None:
        """@pytest.mark.parametrize produces one test per parameter value."""
        capture_file = tmp_path / "params.txt"
        test_file = tmp_path / "test_parametrize.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParams(OttoSuite):
    @pytest.mark.parametrize("val", ["alpha", "beta", "gamma"])
    async def test_values(self, val: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(val + "\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        lines = sorted(l for l in capture_file.read_text().strip().split("\n") if l)
        assert lines == ["alpha", "beta", "gamma"]

    def test_parametrize_with_options(self, tmp_path: Path) -> None:
        """Parametrized tests can also receive suite_options fixture."""
        @dataclass
        class Opts:
            prefix: str = "hello"

        opts = Opts(prefix="world")
        capture_file = tmp_path / "param_opts.txt"
        test_file = tmp_path / "test_param_opts.py"
        test_file.write_text(f"""\
import pathlib
import pytest
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestParamOpts(OttoSuite):
    @pytest.mark.parametrize("suffix", ["1", "2"])
    async def test_combined(self, suite_options, suffix: str) -> None:
        with CAPTURE.open("a") as f:
            f.write(f"{{suite_options.prefix}}-{{suffix}}\\n")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path, options=opts)
        assert exit_code == pytest.ExitCode.OK
        lines = sorted(l for l in capture_file.read_text().strip().split("\n") if l)
        assert lines == ["world-1", "world-2"]


# ── expect() non-fatal assertions ───────────────────────────────────────────

class TestExpect:

    def test_passing_expect_does_not_fail(self, tmp_path: Path) -> None:
        """A truthy expect() should not cause the test to fail."""
        test_file = tmp_path / "test_pass.py"
        test_file.write_text("""\
from otto.suite.suite import OttoSuite

class TestPass(OttoSuite):
    async def test_ok(self) -> None:
        self.expect(True)
        self.expect(1 == 1)
        self.expect("hello")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK

    def test_failing_expect_continues_execution(self, tmp_path: Path) -> None:
        """A failing expect() does not stop the test; later code still runs."""
        capture_file = tmp_path / "continued.txt"
        test_file = tmp_path / "test_continue.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestContinue(OttoSuite):
    async def test_continues(self) -> None:
        self.expect(False)
        CAPTURE.write_text("reached")
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "reached"

    def test_multiple_failures_all_reported(self, tmp_path: Path) -> None:
        """All failing expects appear in the final error message."""
        capture_file = tmp_path / "count.txt"
        test_file = tmp_path / "test_multi.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMulti(OttoSuite):
    async def test_three_failures(self) -> None:
        self.expect(False, "first")
        self.expect(False, "second")
        self.expect(False, "third")
        CAPTURE.write_text(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "3"

    def test_failure_includes_source_line(self, tmp_path: Path) -> None:
        """The failure report includes the source filename and line."""
        capture_file = tmp_path / "report.txt"
        test_file = tmp_path / "test_source.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestSource(OttoSuite):
    async def test_source_info(self) -> None:
        x = 42
        self.expect(x == 99)
        CAPTURE.write_text(self._expect_failures[0])
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        assert "test_source.py" in report
        assert "self.expect(x == 99)" in report
        assert "x = 42" in report

    def test_custom_msg_alongside_source(self, tmp_path: Path) -> None:
        """A custom msg appears alongside (not instead of) source info."""
        capture_file = tmp_path / "msg.txt"
        test_file = tmp_path / "test_msg.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMsg(OttoSuite):
    async def test_custom_msg(self) -> None:
        val = 42
        self.expect(val == 99, "hostname missing from config")
        CAPTURE.write_text(self._expect_failures[0])
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        report = capture_file.read_text()
        # msg is present
        assert "hostname missing from config" in report
        # source info is also present (not replaced by msg)
        assert "self.expect(val == 99" in report
        assert "val = 42" in report

    def test_mix_of_pass_and_fail(self, tmp_path: Path) -> None:
        """Only failing expects are recorded; passing ones are ignored."""
        capture_file = tmp_path / "mix.txt"
        test_file = tmp_path / "test_mix.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestMix(OttoSuite):
    async def test_mixed(self) -> None:
        self.expect(True)
        self.expect(False, "one")
        self.expect(True)
        self.expect(False, "two")
        self.expect(True)
        CAPTURE.write_text(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.TESTS_FAILED
        assert capture_file.read_text() == "2"

    def test_failures_reset_between_tests(self, tmp_path: Path) -> None:
        """Each test starts with a fresh _expect_failures list."""
        capture_file = tmp_path / "reset.txt"
        test_file = tmp_path / "test_reset.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

CAPTURE = pathlib.Path({str(capture_file)!r})

class TestReset(OttoSuite):
    async def test_first(self) -> None:
        self.expect(True)

    async def test_second(self) -> None:
        with CAPTURE.open("w") as f:
            f.write(str(len(self._expect_failures)))
""")
        exit_code = _run_inner_pytest(test_file, tmp_path)
        assert exit_code == pytest.ExitCode.OK
        assert capture_file.read_text() == "0"
