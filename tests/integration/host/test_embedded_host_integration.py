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

import asyncio
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


# ---------------------------------------------------------------------------
# Single-console caveat
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestSingleConsole:
    """An embedded target exposes a single shell console.
    :meth:`EmbeddedHost.open_session` documents that opening a second named
    session is not concurrency-safe — the Zephyr telnet backend
    (``CONFIG_SHELL_BACKEND_TELNET``) accepts only one client at a time.
    These tests pin down the observed behavior so a silent regression — a
    second connection being accepted and working, or worse, succeeding but
    quietly clobbering the first — is caught."""

    @pytest.mark.asyncio
    async def test_second_concurrent_session_is_not_silent(self, host1):
        """Opening a second named session while the default session is live
        must not silently succeed.

        **Observed behavior (Zephyr 3.7.2, qemu_x86 telnet shell):**
        :class:`ConnectionManager` caches the underlying ``TelnetClient`` and
        returns the same one to the second ``open_session``. The new
        :class:`ZephyrSession`'s readiness handshake then writes its READY
        marker onto the shared byte stream — which the first session is also
        reading — and waits for an echo it never gets. The result is a
        hang, which our bounded ``wait_for`` surfaces as a cancellation.

        This test pins down that "not silent" property as a regression
        guard. If a future change opens a *second* TCP connection (which
        the device would close immediately, per raw-telnet testing), the
        failure mode would shift to ``ConnectionError`` /
        ``IncompleteReadError`` — both equally acceptable. What is NOT
        acceptable is the second ``open_session`` returning a working
        session that quietly shares state with the first."""
        # Warm the default session so it holds the device's one telnet slot.
        warmup = (await host1.run("kernel version")).only
        assert warmup.status == Status.Success

        # Bounded wait_for so a "hangs forever" regression fails the test
        # rather than the CI job. 5 s is comfortably longer than a real
        # connection-failure path (~ms) and short enough to keep the
        # suite fast.
        with pytest.raises((
            asyncio.TimeoutError,
            asyncio.CancelledError,
            ConnectionError,
            asyncio.IncompleteReadError,
        )):
            await asyncio.wait_for(host1.open_session("aux"), timeout=5.0)

    @pytest.mark.asyncio
    async def test_default_session_survives_second_open_attempt(self, host1):
        """The critical safety property: after a rejected/hung second-
        session attempt, the **default** session must still be usable.
        Otherwise a user catching the second-open exception would be left
        with a host they can't drive any further."""
        # Establish the default session.
        before = (await host1.run("kernel version")).only
        assert before.status == Status.Success

        # Best-effort second open — we expect failure (see above test for
        # the exhaustive list of acceptable failure modes); the assertion
        # is on what comes after, not on the exception type.
        with pytest.raises(BaseException):
            await asyncio.wait_for(host1.open_session("aux"), timeout=5.0)

        # Default session must still work.
        after = (await host1.run("kernel uptime")).only
        assert after.status == Status.Success, (
            f"default session broke after second-open attempt: {after.output!r}"
        )
