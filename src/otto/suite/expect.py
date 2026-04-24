"""Standalone non-fatal expectation collector.

The :class:`ExpectCollector` accumulates failing checks without stopping
execution, then reports all failures together at the end.  It is
framework-agnostic — use it inside pytest suites, unittest cases, or
plain scripts.

Quick start::

    from otto.suite.expect import ExpectCollector

    collector = ExpectCollector()
    collector.expect(1 == 1)          # passes — no-op
    collector.expect(2 + 2 == 5)      # fails — recorded
    collector.raise_if_failures()     # raises AssertionError with report

Or use the module-level convenience function::

    from otto.suite.expect import ExpectCollector, expect

    collector = ExpectCollector()
    expect(1 == 1, collector=collector)
    expect(2 + 2 == 5, collector=collector)
    collector.raise_if_failures()
"""

from __future__ import annotations

import inspect
import logging
import os


class ExpectCollector:
    """Collects non-fatal expectation failures.

    Args:
        logger: Optional :class:`logging.Logger` for warning output when a
            check fails.  When *None*, failures are still recorded but not
            logged.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.failures: list[str] = []
        """Recorded failure reports for the current run."""

        self.logger = logger

    def expect(
        self,
        condition: object,
        msg: str | None = None,
        _stack_offset: int = 1,
    ) -> None:
        """Record a non-fatal expectation.

        Unlike ``assert``, a failing ``expect()`` does **not** stop
        execution.  All failures are collected and can be inspected via
        :attr:`failures` or raised together with :meth:`raise_if_failures`.

        Args:
            condition: Any truthy/falsy expression to evaluate.
            msg: Optional human-friendly message printed alongside the
                auto-captured source line and locals — not a replacement.
            _stack_offset: How many frames to skip when capturing caller
                context.  Callers that wrap this method should increase
                the offset so the report points at *their* caller.

        Examples:
            Direct usage::

                collector = ExpectCollector()
                x = 42
                collector.expect(x == 99, "math is broken")
                assert len(collector.failures) == 1
                assert "x = 42" in collector.failures[0]

        .. note::
            The auto-captured source line and locals are best-effort.
            Provide *msg* when the expression alone isn't self-explanatory.
        """
        if condition:
            return

        # Capture caller context for the failure message
        frame_info = inspect.stack(context=1)[_stack_offset]
        filename = os.path.basename(frame_info.filename)
        lineno = frame_info.lineno
        source_line = (frame_info.code_context or [''])[0].strip()

        # Build a summary of the caller's local variables
        caller_locals = frame_info.frame.f_locals
        locals_summary = ', '.join(
            f'{k} = {v!r}'
            for k, v in caller_locals.items()
            if not k.startswith('_') and k != 'self'
        )

        # Assemble the failure report
        header = f'{filename}:{lineno}'
        parts = [header, f'  {source_line}']
        if msg:
            parts.append(f'  Message: {msg}')
        if locals_summary:
            parts.append(f'  Locals: {locals_summary}')
        report = '\n'.join(parts)

        self.failures.append(report)
        if self.logger is not None:
            log_msg = f'[bold yellow]EXPECT FAILED[/bold yellow]  {header}\n  {source_line}'
            if msg:
                log_msg += f'\n  Message: {msg}'
            self.logger.warning(log_msg)

    def reset(self) -> None:
        """Clear all recorded failures."""
        self.failures.clear()

    def raise_if_failures(self) -> None:
        """Raise :class:`AssertionError` if any failures were recorded.

        The error message contains all failure reports joined together.
        This is the recommended way to surface failures outside of pytest.
        """
        if self.failures:
            summary = '\n\n'.join(self.failures)
            raise AssertionError(
                f'{len(self.failures)} expectation(s) failed:\n\n{summary}'
            )


def expect(
    condition: object,
    msg: str | None = None,
    *,
    collector: ExpectCollector,
) -> None:
    """Module-level convenience wrapper around :meth:`ExpectCollector.expect`.

    Behaves identically to ``collector.expect(condition, msg)`` but uses a
    functional call style.  The *collector* keyword argument is required so
    that failure state is always explicit — there is no hidden global.

    Args:
        condition: Any truthy/falsy expression to evaluate.
        msg: Optional human-friendly message.
        collector: The :class:`ExpectCollector` that will record any failure.
    """
    collector.expect(condition, msg, _stack_offset=2)
