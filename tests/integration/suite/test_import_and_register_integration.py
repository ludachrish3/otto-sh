import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestSuiteOptionsPassthrough:
    """Options reach test methods via the suite_options fixture from OttoOptionsPlugin."""

    def test_options_passed_through_to_suite(self, tmp_path: Path) -> None:
        """End-to-end: CLI options propagate to the suite_options fixture.

        Runs a minimal inner pytest session carrying a custom Options instance via
        OttoOptionsPlugin, then asserts that the suite_options fixture provides it
        correctly.
        """
        from otto.context import reset_context, set_context
        from otto.suite.plugin import OttoPlugin
        from otto.suite.register import OttoOptionsPlugin

        @dataclass
        class Opts:
            device_type: str = "router"

        opts = Opts(device_type="switch")

        capture_file = tmp_path / "captured.txt"
        test_file = tmp_path / "test_capture.py"
        test_file.write_text(f"""\
import pathlib
from otto.suite.suite import OttoSuite

class TestCapture(OttoSuite):
    async def test_capture_suite_options(self, suite_options) -> None:
        pathlib.Path({str(capture_file)!r}).write_text(
            suite_options.device_type if suite_options is not None else "NONE"  # type: ignore
        )
        assert suite_options is not None, "suite_options was not provided by OttoOptionsPlugin"
        assert suite_options.device_type == "switch"  # type: ignore
""")

        # Inject a minimal OttoContext so OttoSuite.setup_method can call
        # get_context().output_dir (Task 3 migration: output_dir lives on
        # OttoContext, not on the logger).
        mock_ctx = MagicMock()
        mock_ctx.output_dir = tmp_path
        token = set_context(mock_ctx)
        try:
            exit_code = pytest.main(
                [str(test_file), "-o", "asyncio_mode=auto",
                 "-o", "asyncio_default_fixture_loop_scope=function",
                 "--no-cov", "--override-ini", "addopts="],
                plugins=[OttoPlugin(), OttoOptionsPlugin(opts)],
            )
        finally:
            reset_context(token)
            # Evict the generated module (keyed by stem) so a second run
            # of this test in the same process -- e.g. under
            # `pytest --count` -- re-imports it instead of hitting
            # "import file mismatch". clean_registry only sweeps
            # `_otto_suite_*` keys, not this one.
            sys.modules.pop(test_file.stem, None)

        assert capture_file.exists(), "test_capture_suite_options never ran"
        assert capture_file.read_text() == "switch"
        assert exit_code == pytest.ExitCode.OK
