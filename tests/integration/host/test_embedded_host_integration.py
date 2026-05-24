"""
Zephyr-specific live integration tests for :class:`EmbeddedHost`.

The OS-agnostic contract (every backend must satisfy a basic ``run`` /
``oneshot`` / file-transfer shape) lives in :mod:`test_host_contract` and is
parametrized over Unix and Zephyr backends both. This file covers Zephyr
**implementation-detail** behavior the contract suite intentionally does
not assert on:

- **Signed errno retcodes**: Zephyr returns ``-8`` (``-ENOEXEC``) for an
  unknown command, distinct from Unix bash's ``127``. The shared contract
  only asserts ``Status.Failed``; this file confirms the integer travels.
- **Multi-line shell output stays clean** through :class:`ZephyrSession`'s
  positional parser and ANSI stripping (the prompt is colorized).
- **A Zephyr-stock command runs**: ``kernel uptime`` produces a bare
  integer of microseconds — a sanity check that builtins not exercised by
  the contract suite still work.

Parametrized over the three Zephyr backends (``zephyr_fat`` /
``zephyr_lfs`` / ``zephyr_no_fs``). Carries both ``integration`` and
``embedded`` markers so it is opted into via ``pytest -m embedded``.
"""

import re

import pytest

from otto.utils import Status


_ALL_ZEPHYR = pytest.mark.parametrize(
    "host1",
    [
        pytest.param(
            "zephyr_fat",
            marks=[pytest.mark.integration, pytest.mark.embedded],
        ),
        pytest.param(
            "zephyr_lfs",
            marks=[pytest.mark.integration, pytest.mark.embedded],
        ),
        pytest.param(
            "zephyr_no_fs",
            marks=[pytest.mark.integration, pytest.mark.embedded],
        ),
    ],
    indirect=True,
)


pytestmark = pytest.mark.timeout(30)


# ---------------------------------------------------------------------------
# Signed errno retcodes (Zephyr-isms)
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestSignedRetcode:

    @pytest.mark.asyncio
    async def test_unknown_command_returns_negative_enoexec(self, host1):
        """The Zephyr shell sets ``retval`` to ``-8`` (``-ENOEXEC``) after an
        unknown command — the signed errno convention. Unix bash would use
        ``127`` here, so this distinguishes the framing path."""
        result = (await host1.run("definitely_not_a_zephyr_command")).only
        assert result.status == Status.Failed
        assert result.retcode == -8, (
            f"expected -ENOEXEC (-8) for unknown Zephyr command, "
            f"got {result.retcode}"
        )


# ---------------------------------------------------------------------------
# Multi-line output stays clean through ZephyrSession's positional parser
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestMultilineOutputClean:

    @pytest.mark.asyncio
    async def test_kernel_threads_output_has_no_marker_or_prompt_noise(self, host1):
        """``kernel threads`` produces several lines on the Zephyr shell.
        The positional parser must drop the bracketing prompt lines without
        leaking the BEGIN marker, the ``retval`` line, or any ANSI escapes
        into the captured output."""
        result = (await host1.run("kernel threads")).only
        assert result.status == Status.Success
        # No otto sentinels leaked into the output.
        assert "__OTTO_" not in result.output
        # No `retval` echo (the parser must take only the command's output).
        # `retval` itself as a substring could legitimately appear in other
        # contexts, so we only check for it on a line of its own.
        for line in result.output.splitlines():
            assert line.strip() != "retval", (
                f"retval line leaked into output: {result.output!r}"
            )
        # No raw ANSI escapes (the shell's colored prompt is stripped before
        # parsing).
        assert "\x1b[" not in result.output


# ---------------------------------------------------------------------------
# Stock Zephyr builtins
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestStockBuiltins:

    @pytest.mark.asyncio
    async def test_kernel_uptime_yields_integer_microseconds(self, host1):
        """``kernel uptime`` prints a single bare integer (microseconds since
        boot) on Zephyr 3.7. A clean parse of that integer is a small
        sanity check that ``_parse_output`` and the framing seam agree on
        what counts as "the command's output" for a one-line command."""
        result = (await host1.run("kernel uptime")).only
        assert result.status == Status.Success
        # The output may include a unit-suffix label depending on the build;
        # we just need an integer somewhere in the first line.
        first_line = result.output.splitlines()[0] if result.output else ""
        assert re.search(r"\d+", first_line), (
            f"kernel uptime first line had no integer: {first_line!r}"
        )
