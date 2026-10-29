"""Standalone non-fatal expectation collector.

The :class:`ExpectCollector` accumulates failing checks without stopping
execution, then reports all failures together at the end.  It is
framework-agnostic — use it inside pytest suites, unittest cases, or
plain scripts.

Quick start::

    from otto.suite.expect import ExpectCollector

    collector = ExpectCollector()
    collector.expect(1 == 1)  # passes — no-op
    collector.expect(2 + 2 == 5)  # fails — recorded
    collector.raise_if_failures()  # raises AssertionError with report

Or use the module-level convenience function::

    from otto.suite.expect import ExpectCollector, expect

    collector = ExpectCollector()
    expect(1 == 1, collector=collector)
    expect(2 + 2 == 5, collector=collector)
    collector.raise_if_failures()
"""

import ast
import functools
import inspect
import itertools
import linecache
import logging
import re
import reprlib
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol, runtime_checkable

from typing_extensions import override

from otto.utils import ellipsize

_VALUE_CAP = 100
"""The most characters a plain value's repr may take in a report."""

_CREDENTIAL_NAME = re.compile(
    r"passw(or)?d|passphrase|secret|token|(?:^|_)pwd?(?:_|$)", re.IGNORECASE
)
"""A variable name or mapping key that looks like it holds a credential.

Searched, so ``db_password`` and ``tokens_left`` match too; ``pw`` and ``pwd``
match as a whole underscore-separated segment (``db_pwd``, ``ssh_pw``), not
inside a word (``pwdx``, ``upward``); ``passed`` and ``bypass`` do not match.
Its value is shown as :data:`_MASKED`, whatever it is.
"""

_MASKED = "'***'"
"""What a report shows in place of a credential-looking name's value."""


@runtime_checkable
class OttoSummary(Protocol):
    """A value that tells a failing ``expect()`` report how to show it in one line.

    otto's hosts, :class:`~otto.labs.loader.Lab`, the run context and
    :class:`~otto.result.CommandResult` implement it; a user class may define
    the method too. The summary is shown as is, never cut.
    """

    def __otto_summary__(self) -> str:
        """Return a short identity such as ``<UnixHost router1>``, holding no secret."""
        ...


class _MaskingRepr(reprlib.Repr):
    """A :class:`reprlib.Repr` that keeps values short and masks credential-keyed entries.

    Any mapping (a ``dict`` or not) is rendered entry by entry at every depth
    the repr reaches, and the value under a ``str`` key that looks like a
    credential is shown as ``'***'``. Strings and other reprs are bounded
    well past the report's cap so :func:`otto.utils.ellipsize` makes the one
    visible cut.
    """

    def __init__(self) -> None:
        super().__init__()
        self.maxstring = self.maxother = self.maxlong = 4 * _VALUE_CAP
        self.maxlist = self.maxtuple = self.maxset = self.maxfrozenset = 6
        self.maxdeque = self.maxarray = 6
        self.maxdict = 4
        self.maxlevel = 2

    @override
    def repr1(self, x: object, level: int) -> str:
        if isinstance(x, Mapping):
            return self._repr_mapping(x, level)
        return super().repr1(x, level)

    def _repr_mapping(self, x: Mapping[object, object], level: int) -> str:
        """Render *x* as ``{key: value, ...}``, masking credential-keyed values."""
        if not x:
            body = ""
        elif level <= 0:
            body = "..."
        else:
            pieces = []
            for key in itertools.islice(x, self.maxdict):
                masked = isinstance(key, str) and _CREDENTIAL_NAME.search(key)
                shown = _MASKED if masked else self.repr1(x[key], level - 1)
                pieces.append(f"{self.repr1(key, level - 1)}: {shown}")
            if len(x) > self.maxdict:
                pieces.append("...")
            body = ", ".join(pieces)
        return f"{{{body}}}" if type(x) is dict else f"{type(x).__name__}({{{body}}})"


_REPR = _MaskingRepr()


@dataclass(frozen=True)
class _ParsedSource:
    """One file's source text, its lines and its syntax tree."""

    source: str
    lines: list[str]
    tree: ast.Module


def _parse(lines: list[str]) -> _ParsedSource | None:
    """Parse *lines*; ``None`` when they are not valid Python."""
    source = "".join(lines)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return _ParsedSource(source=source, lines=lines, tree=tree)


@functools.lru_cache(maxsize=32)
def _parse_file(filename: str, size: int, mtime: float) -> _ParsedSource | None:
    """Parse a file linecache holds; keyed on its size and mtime so an edit reparses."""
    del size, mtime  # the cache key only
    return _parse(linecache.getlines(filename))


def _read_source(frame: FrameType) -> _ParsedSource | None:
    """Return *frame*'s parsed file source, memoised per file version; ``None`` if unreadable."""
    filename = frame.f_code.co_filename
    linecache.checkcache(filename)
    lines = linecache.getlines(filename, frame.f_globals)
    if not lines:
        return None
    entry = linecache.cache.get(filename)
    # A full entry is (size, mtime, lines, fullname); a lazy one has one item.
    if entry is not None and len(entry) > 1 and entry[1] is not None:
        return _parse_file(filename, entry[0], entry[1])
    return _parse(lines)  # doctest examples, loader-served source: no file to key on


@dataclass(frozen=True)
class _CallSite:
    """The one call a failing frame is executing, as read from its source."""

    source: str
    """The call's full source text, dedented, every line of it."""

    names: list[str]
    """The plain variable names its arguments read, in first-read order."""


def _find_call(frame: FrameType) -> _CallSite | None:
    """Locate the call *frame* is executing on its current line, or ``None``.

    The candidates are the outermost calls whose lines span the frame's line.
    No source, unparsable source, or anything but exactly one candidate (two
    calls on one line) is ``None``: the report then shows no values rather
    than guess.
    """
    parsed = _read_source(frame)
    if parsed is None:
        return None
    lineno = frame.f_lineno
    spanning = [
        node
        for node in ast.walk(parsed.tree)
        if isinstance(node, ast.Call) and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    inner = {id(sub) for node in spanning for sub in ast.walk(node) if sub is not node}
    outermost = [node for node in spanning if id(node) not in inner]
    if len(outermost) != 1:
        return None
    (call,) = outermost
    segment = ast.get_source_segment(parsed.source, call)
    if segment is None:
        return None
    names: list[str] = []
    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        for node in ast.walk(argument):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id not in names
            ):
                names.append(node.id)
    # col_offset counts UTF-8 bytes; the indent must count the characters before the call.
    before = parsed.lines[call.lineno - 1].encode()[: call.col_offset].decode(errors="replace")
    text = textwrap.dedent(" " * len(before) + segment)
    return _CallSite(source=text, names=names)


def _render(name: str, value: object) -> str:
    """One value as a report shows it: masked, summarised, or a capped repr.

    Never raises: a summary or a repr that fails falls back to the class name.
    """
    if _CREDENTIAL_NAME.search(name):
        return _MASKED
    try:
        if isinstance(value, OttoSummary):
            return str(value.__otto_summary__())
        return ellipsize(_REPR.repr(value), _VALUE_CAP)
    except Exception:  # noqa: BLE001 — a report must never fail on the value it reports
        return f"<{type(value).__name__}>"


def _values_line(frame: FrameType, names: list[str]) -> str | None:
    """``name = value, ...`` for the *names* that *frame* can resolve, or ``None``.

    Locals then module globals; a builtin, a module, a function or a class is
    left out, since it says nothing about why the check failed.
    """
    rendered: list[str] = []
    for name in names:
        if name in frame.f_locals:
            value = frame.f_locals[name]
        elif name in frame.f_globals:
            value = frame.f_globals[name]
        else:
            continue
        if inspect.ismodule(value) or inspect.isroutine(value) or inspect.isclass(value):
            continue
        rendered.append(f"{name} = {_render(name, value)}")
    return ", ".join(rendered) or None


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

        A failure report holds the call's source (every line of a multi-line
        call), *msg*, and ``Values:``: only the plain variable names the
        call's arguments read, never attribute chains, so no property runs.
        A value implementing :class:`OttoSummary` (hosts, labs, the context,
        command results, or a user class defining ``__otto_summary__``) shows
        that one-line summary; anything else shows a :mod:`reprlib` repr cut
        at 100 characters. Masking is by name and by mapping key: a name that
        looks like a credential (``password``, ``passwd``, ``passphrase``,
        ``secret``, ``token``, or ``pw``/``pwd`` as a whole underscore-separated
        segment such as ``db_pwd``) shows ``'***'`` whatever it
        holds, and so does the value under such a ``str`` key in any mapping
        the repr reaches.

        Args:
            condition: Any truthy/falsy expression to evaluate.
            msg: Optional human-friendly message printed alongside the
                auto-captured call source and values — not a replacement.
            _stack_offset: How many frames to skip when capturing caller
                context.  Callers that wrap this method should increase
                the offset so the report points at *their* caller.

        Examples:
            Direct usage::

                collector = ExpectCollector()
                x = 42
                collector.expect(x == 99, "math is broken")
                assert len(collector.failures) == 1
                assert "Values: x = 42" in collector.failures[0]

        .. note::
            When the call cannot be found unambiguously (no readable source,
            or two calls on one line) the report omits ``Values:``. Provide
            *msg* when the expression alone isn't self-explanatory.
        """
        if condition:
            return

        frame = inspect.currentframe()
        for _ in range(_stack_offset):
            frame = frame.f_back if frame is not None else None
        if frame is None:  # pragma: no cover — no Python frame above us
            self._record(msg, "<unknown>", "", None)
            return
        try:
            header = f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}"
            source, values = "", None
            try:
                call = _find_call(frame)
                if call is not None:
                    source, values = call.source, _values_line(frame, call.names)
            except Exception:  # noqa: BLE001 — the user's failure is recorded whatever the report hits
                source, values = "", None
            if not source:
                source = linecache.getline(
                    frame.f_code.co_filename, frame.f_lineno, frame.f_globals
                ).strip()
        finally:
            del frame
        self._record(msg, header, source, values)

    def _record(self, msg: str | None, header: str, source: str, values: str | None) -> None:
        """Append one failure report and log it, if a logger was given."""
        indented = textwrap.indent(source, "  ") if source else "  "
        parts = [header, indented]
        if msg:
            parts.append(f"  Message: {msg}")
        if values:
            parts.append(f"  Values: {values}")
        self.failures.append("\n".join(parts))
        if self.logger is not None:
            from rich.markup import escape

            # The report is the test's own text: escaped, so its brackets print.
            log_msg = (
                f"[bold yellow]EXPECT FAILED[/bold yellow]  {escape(header)}\n{escape(indented)}"
            )
            if msg:
                log_msg += f"\n  Message: {escape(msg)}"
            self.logger.warning(log_msg, extra={"markup": True, "emoji": False})

    def __call__(self, condition: object, msg: str | None = None) -> None:
        """``expect(condition, msg)`` — the form the ``expect`` fixture hands a test.

        Identical to :meth:`expect`, with the call source and values taken
        one frame further up so the report points at the test, not here.

        Examples:
            The failure report always includes the source location and the
            values the call reads.  When *msg* is provided it appears *in
            addition to* the auto-captured source context, never replacing it:

            >>> from otto.suite.expect import ExpectCollector
            >>> collector = ExpectCollector()
            >>> x = 42
            >>> collector(x == 99, "math is broken")
            >>> report = collector.failures[0]
            >>> print(report.splitlines()[1])  # the caller's line, not __call__'s
              collector(x == 99, "math is broken")
            >>> "Message: math is broken" in report
            True
            >>> print(report.splitlines()[-1])
              Values: x = 42
        """
        self.expect(condition, msg, _stack_offset=2)

    def reset(self) -> None:
        """Clear all recorded failures."""
        self.failures.clear()

    def raise_if_failures(self) -> None:
        """Raise :class:`AssertionError` if any failures were recorded.

        The error message contains all failure reports joined together.
        This is the recommended way to surface failures outside of pytest.
        """
        if self.failures:
            summary = "\n\n".join(self.failures)
            raise AssertionError(f"{len(self.failures)} expectation(s) failed:\n\n{summary}")


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
