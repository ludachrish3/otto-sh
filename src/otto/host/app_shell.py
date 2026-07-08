"""AppShell parsing engine — ``Parsed`` models and the ``parse=`` dispatch.

This module holds both halves of otto's AppShell feature: the *parsing half* —
regex-backed pydantic models (:class:`Parsed`) and the functions that turn REPL
output into typed objects — and the interactive :class:`AppShell` REPL that
wraps an application (mysql, ``python3``) living inside an already-open shell
session, driving it with :meth:`AppShell.cmd` and locking out the session's
sentinel-framed ``run`` while attached.

A :class:`Parsed` subclass pairs a pydantic model with the compiled regex that
produces it. Named groups feed same-named fields; pydantic converts the
captured strings to the field types. A field typed as another ``Parsed``
subclass — or ``list[Sub]`` / ``Sub | None`` — is parsed *recursively* from the
region its group captured, so composite REPL output (mysql's bordered table
*and* its trailing stats line, say) maps to a nested object graph.

The public entry point is :func:`apply_parse`, which dispatches on the shape of
the ``parse=`` spec:

* a ``Parsed`` subclass  -> single :func:`parse_one` (``pattern.search``);
* ``list[Sub]``          -> :func:`parse_all` (``pattern.finditer``), where the
  empty list is a valid "zero rows" answer;
* any other callable     -> called as an escape hatch, its return value used
  verbatim and any exception surfaced as :class:`ParseMismatch`.
"""

import asyncio
import re
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Union, get_args, get_origin

from pydantic import ValidationError
from typing_extensions import Self, override

from otto.models.base import OttoModel
from otto.result import ShellResult
from otto.utils import Status

from .command_frame import _ANSI_RE

if TYPE_CHECKING:
    from .session import HostSession


class ParseMismatch(ValueError):  # noqa: N818 — spec-mandated public name; an `Error` suffix would break the documented AppShell API
    """Output did not match the model's pattern (or the callable raised)."""


class Parsed(OttoModel):
    """A pydantic model plus the regex that produces it.

    Named groups feed same-named fields; a field typed as another ``Parsed``
    subclass (or ``list[Sub]`` / ``Sub | None``) is recursively parsed from the
    region its group captured. Subclasses must define ``pattern`` as a compiled
    :class:`re.Pattern`; a class-definition-time check enforces that the
    pattern's named groups are a subset of the field names (typo guard) and a
    superset of the required fields (so pattern/model drift is impossible).
    Because every subclass self-checks at its own definition, nested models are
    validated automatically at each level.
    """

    pattern: ClassVar[re.Pattern[str]]

    @classmethod
    @override
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        pattern = getattr(cls, "pattern", None)
        if not isinstance(pattern, re.Pattern):
            raise TypeError(f"{cls.__name__} must define a compiled ClassVar 'pattern'")
        groups = set(pattern.groupindex)
        fields = set(cls.model_fields)
        required = {name for name, field in cls.model_fields.items() if field.is_required()}
        if groups - fields:
            raise TypeError(
                f"{cls.__name__}: pattern named groups {sorted(groups - fields)} "
                f"have no matching field"
            )
        if required - groups:
            raise TypeError(
                f"{cls.__name__}: required fields {sorted(required - groups)} "
                f"have no pattern named group"
            )
        # A ``list[X]`` field means "finditer the sub-model's pattern over the
        # region", so ``X`` must itself be a ``Parsed`` subclass; a scalar
        # element (``list[int]``) has no sub-pattern and is meaningless in the
        # region engine. Reject it LOUDLY here — otherwise it would fall through
        # ``_parse_region`` to a raw region string and, post the ValidationError
        # -> ParseMismatch wrap, silently downgrade an authoring error into a
        # quiet failed result.
        for field, info in cls.model_fields.items():
            inner = _unwrap_optional(info.annotation)
            if get_origin(inner) is not list:
                continue
            (element,) = get_args(inner)
            if not (isinstance(element, type) and issubclass(element, Parsed)):
                raise TypeError(
                    f"{cls.__name__}.{field}: list fields must contain a Parsed "
                    f"subclass, got {element}"
                )


def _unwrap_optional(annotation: Any) -> Any:
    """Return the inner type of an ``X | None`` annotation, else the annotation.

    Handles both the ``types.UnionType`` (``X | None``) and ``typing.Union``
    (``Optional[X]``) spellings. A union of several non-``None`` members is
    returned unchanged — only the ``Sub | None`` shape is unwrapped.
    """
    if get_origin(annotation) in (types.UnionType, Union):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _parse_region(annotation: Any, region: str | None) -> Any:
    """Interpret one group's captured ``region`` per its field ``annotation``.

    A ``None`` region (an optional group that did not participate) is passed
    through as ``None``. A ``Parsed``-typed field recurses via
    :func:`parse_one`; a ``list[Sub]`` field via :func:`parse_all`; any scalar
    field keeps the raw string for pydantic to convert.
    """
    if region is None:
        return None
    inner = _unwrap_optional(annotation)
    if get_origin(inner) is list:
        (element,) = get_args(inner)
        if isinstance(element, type) and issubclass(element, Parsed):
            return parse_all(element, region)
    elif isinstance(inner, type) and issubclass(inner, Parsed):
        return parse_one(inner, region)
    return region


def _from_match(model: type[Parsed], match: re.Match[str]) -> Parsed:
    """Build ``model`` from a match, recursing into nested ``Parsed`` fields.

    Only named groups (guaranteed a subset of the field names by the
    class-definition check) contribute to the data dict; fields without a
    matching group keep their defaults.
    """
    data = {
        name: _parse_region(model.model_fields[name].annotation, region)
        for name, region in match.groupdict().items()
    }
    try:
        return model(**data)
    except ValidationError as exc:
        # The regex matched but a captured string failed type conversion — a
        # DATA problem per spec §10, not a state problem. Surface it as the
        # uniform in-band failure signal so ``cmd`` returns a failed
        # ShellResult instead of leaking a raw pydantic ValidationError. This
        # routes parse_one, parse_all, and nested sub-model construction alike.
        raise ParseMismatch(f"{model.__name__}: matched but validation failed: {exc}") from exc


def parse_one(model: type[Parsed], text: str) -> Parsed:
    """Search ``text`` with ``model.pattern`` and build a single instance.

    Raises :class:`ParseMismatch` (naming the pattern) if nothing matches.
    """
    match = model.pattern.search(text)
    if match is None:
        raise ParseMismatch(f"{model.__name__}: no match for pattern {model.pattern.pattern!r}")
    return _from_match(model, match)


def parse_all(model: type[Parsed], text: str) -> list[Parsed]:
    """Return one ``model`` per ``finditer`` match; the empty list is valid."""
    return [_from_match(model, match) for match in model.pattern.finditer(text)]


def apply_parse(spec: Any, text: str) -> Any:
    """Apply a ``parse=`` spec to ``text`` and return the parsed value.

    ``spec`` is a :class:`Parsed` subclass (single :func:`parse_one`), a
    ``list[Sub]`` of one (:func:`parse_all`), or any other callable used as an
    escape hatch — its exceptions are wrapped in :class:`ParseMismatch`.
    """
    if isinstance(spec, type) and issubclass(spec, Parsed):
        return parse_one(spec, text)
    if get_origin(spec) is list:
        (element,) = get_args(spec)
        if isinstance(element, type) and issubclass(element, Parsed):
            return parse_all(element, text)
        raise TypeError(f"list parse spec element must be a Parsed subclass, got {element!r}")
    if callable(spec):
        try:
            return spec(text)
        except Exception as exc:
            # Name the exception type so a bare ``KeyError('x')`` reports
            # "KeyError: 'x'" rather than the opaque "'x'" (M12-2).
            raise ParseMismatch(f"{type(exc).__name__}: {exc}") from exc
    raise TypeError(f"unsupported parse spec: {spec!r}")


class AppShellActiveError(RuntimeError):
    """A shell session already has an :class:`AppShell` attached.

    Raised by :meth:`~otto.host.session.ShellSession.run_cmd` while a shell is
    attached — the sentinel command frame must never be typed into the app, so
    ``run()`` is locked out — and by :meth:`AppShell.attach` when the session
    already owns an active shell (AppShells do not nest).
    """


class AppShellTimeoutError(TimeoutError):
    """The application REPL's prompt did not return within the timeout.

    A *state* failure (the REPL is left in an unknown state), distinct from a
    *data* mismatch: :meth:`AppShell.cmd` marks the shell broken and the
    attaching context manager, on unwind, skips ``quit_cmd`` and goes straight
    to the command-frame recovery handshake.

    On the caller-owned :meth:`AppShell.attach` path that recovery cannot
    always confirm the POSIX shell is back; discard the session after this
    error rather than reusing it (see :meth:`AppShell.attach`).
    """


# Give-up ceiling for the app-shell teardown confirm in ``_exit`` — NOT an
# expected wait. Once the graceful path stopped SIGINT-racing the app's own exit
# (the wedge bug), a responsive shell confirms on the first probe and pays
# nothing here; this is only the point at which a genuinely-stuck shell is
# declared dead. A little larger than ``session._RECOVERY_TIMEOUT`` (5s) so a
# REPL that is merely CPU-starved (not wedged) still returns the POSIX shell
# under realistic ``-n auto`` contention, without the fast-fail post-timeout
# path inheriting the looser budget. See the app-shell recovery e2e.
_EXIT_RECOVERY_TIMEOUT = 10.0


class AppShell:
    r"""Base class for an application REPL living inside a shell session.

    A subclass declares how to start the app (:attr:`launch`), how to recognise
    its prompt (:attr:`prompt`), and how to quit (:attr:`quit_cmd`); it may add
    methods that build on :meth:`cmd`. Attach it to an already-open
    :class:`~otto.host.session.HostSession` with the :meth:`attach` async
    context manager, then drive the REPL with :meth:`cmd`::

        class MySql(AppShell):
            launch = "mysql --pager=cat"
            prompt = re.compile(r"mysql> \Z")
            quit_cmd = "quit"


        async with MySql.attach(session) as sql:
            rows = (await sql.cmd("SELECT id FROM users;", parse=list[Row])).value

    While attached, the session's sentinel-framed
    :meth:`~otto.host.session.HostSession.run` is locked out (raises
    :class:`AppShellActiveError`) so the command frame is never typed into the
    app; raw ``send`` / ``expect`` stay available. :class:`AppShell` is a plain
    class, not a pydantic model.
    """

    launch: ClassVar[str]
    """Command that starts the application REPL (sent with a trailing newline)."""

    prompt: ClassVar[re.Pattern[str]]
    r"""Pattern matching the app's prompt. A ``str`` is compiled at
    class-definition time; an end-anchored pattern (``r"...\Z"``) is recommended
    so a prompt-looking substring in the output cannot match early."""

    quit_cmd: ClassVar[str] = "exit"
    """Line sent on a clean context exit to leave the REPL."""

    user: ClassVar[str | None] = None
    """Cred login to become before launching (Part-1 proxy machinery); ``None``
    keeps the session's current user."""

    cmd_timeout: ClassVar[float] = 30.0
    """Class-level default seconds to wait for the prompt on launch and
    :meth:`cmd`. Overridable per-session via ``timeout=`` on :meth:`attach` /
    :meth:`~otto.host.host.BaseHost.app_shell` (governs the launch wait and
    becomes the default for every :meth:`cmd` in that session), and per-command
    via ``cmd(timeout=)`` (wins over both)."""

    def __init__(self, session: "HostSession", timeout: float | None = None) -> None:
        self._session = session
        # This session's effective default prompt-wait: the per-session
        # override if one was given at attach() time, else the class default.
        self._timeout = timeout if timeout is not None else self.cmd_timeout
        # Set when a prompt wait times out: the REPL state is unknown, so exit
        # skips quit_cmd and goes straight to POSIX-shell recovery.
        self._broken = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a subclass missing ``launch``/``prompt``; compile a ``str`` prompt.

        Runs at class-definition time (the plain-class analogue of
        :meth:`Parsed.__pydantic_init_subclass__`) so a misdeclared shell fails
        on import, never at attach time.
        """
        super().__init_subclass__(**kwargs)
        missing = [name for name in ("launch", "prompt") if not hasattr(cls, name)]
        if missing:
            raise TypeError(
                f"{cls.__name__} must define ClassVar(s) {missing} — an AppShell "
                f"subclass needs both a launch command and a prompt pattern"
            )
        if isinstance(cls.prompt, str):
            cls.prompt = re.compile(cls.prompt)
        if not isinstance(cls.prompt, re.Pattern):
            raise TypeError(
                f"{cls.__name__}.prompt must be a str or compiled re.Pattern, "
                f"got {type(cls.prompt).__name__}"
            )

    @classmethod
    @asynccontextmanager
    async def attach(
        cls, session: "HostSession", *, timeout: float | None = None
    ) -> "AsyncIterator[Self]":
        r"""Attach the shell to an already-open session (async context manager).

        Takes the session's app-shell lock, sends :attr:`launch`, waits for
        :attr:`prompt`, and yields the shell instance. While the block runs, the
        session's sentinel-framed ``run`` is locked out. On exit it sends
        :attr:`quit_cmd` (unless the shell is broken), confirms the POSIX shell
        via the command-frame recovery handshake, and releases the lock; the
        session itself is left open. Raises :class:`AppShellActiveError` if the
        session already has a shell attached, or :class:`AppShellTimeoutError`
        if the launch prompt never arrives.

        ``timeout``, if given, becomes this session's default prompt-wait: it
        governs the launch wait below and is used by :meth:`cmd` for every call
        in the session that doesn't pass its own ``timeout=``. Falls back to
        :attr:`cmd_timeout` when omitted.

        .. note::
           After an :class:`AppShellTimeoutError`, treat this session as spent:
           discard it and open a fresh one rather than reusing it. On this
           caller-owned ``attach`` path the shell may be left parked inside the
           application REPL, and the recovery handshake cannot always confirm
           the POSIX shell is back — a REPL that ignores Ctrl+C and echoes the
           recovery marker back can spoof a successful recovery.
           :meth:`~otto.host.host.BaseHost.app_shell` is unaffected: it owns and
           closes the session for you.
        """
        shell = cls(session, timeout=timeout)
        await shell._enter()
        try:
            yield shell
        finally:
            await shell._exit()

    async def _enter(self) -> None:
        """Take the lock, launch the app, and wait for its first prompt.

        The lock is released here on *any* launch failure — because ``_enter``
        raising means the :meth:`attach` context manager never runs its exit
        path, so nothing else would release it. A prompt timeout is translated
        to :class:`AppShellTimeoutError`; a dead transport or cancellation
        surfaces unchanged. Either way a shell that never started leaves the
        session unlocked (and its ``run`` usable again).
        """
        inner = self._session._session  # noqa: SLF001 — intra-package access to the HostSession's ShellSession to take the app-shell lock
        active = inner._app_shell  # noqa: SLF001 — intra-package read of the ShellSession app-shell lock
        if active is not None:
            raise AppShellActiveError(
                f"{type(active).__name__} is already attached to this session; "
                f"AppShells do not nest"
            )
        inner._app_shell = self  # noqa: SLF001 — intra-package write taking the ShellSession app-shell lock
        try:
            await self._session.send(self.launch + "\n")
            await self._session.expect(self.prompt, timeout=self._timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            inner._app_shell = None  # noqa: SLF001 — release the lock: the app never reached its prompt
            # The launch line was typed into a REPL that never returned its
            # prompt, so the caller's session may be parked inside a live app
            # (the #1 authoring error, a wrong `prompt` regex, hits exactly
            # this). Flag it for recovery: _ensure_ready re-confirms the POSIX
            # shell on the session's next use instead of typing a frame into
            # the app. Harmless on the host.app_shell owned-session path
            # (close() never calls _ensure_ready).
            inner._needs_recovery = True  # noqa: SLF001 — mark the ShellSession for recovery on next use
            raise AppShellTimeoutError(
                f"{type(self).__name__}: prompt {self.prompt.pattern!r} not seen "
                f"within {self._timeout}s of launch {self.launch!r}"
            ) from exc
        except BaseException:
            # Non-timeout launch failure (dead transport, cancellation): the
            # shell never attached, so release the lock and let the real error
            # surface instead of a misleading AppShellActiveError on next run().
            inner._app_shell = None  # noqa: SLF001 — release the lock: launch failed before the shell attached
            raise

    async def cmd(
        self,
        text: str,
        *,
        parse: Any = None,
        timeout: float | None = None,
    ) -> ShellResult:
        """Run one line in the REPL and return its :class:`~otto.result.ShellResult`.

        Sends ``text``, waits for the next :attr:`prompt`, and strips the echoed
        command line (if the app echoed it), ANSI sequences, and the matched
        prompt — what remains is :attr:`~otto.result.ShellResult.output`. With no
        ``parse`` the output is also the ``value``; with a ``parse`` spec the
        output is fed to :func:`apply_parse`. A parse mismatch is a *data*
        problem — it returns a failed :class:`~otto.result.ShellResult` with the
        output preserved, not an exception. A prompt timeout is a *state*
        problem — the shell is marked broken and :class:`AppShellTimeoutError`
        is raised.

        ``timeout``, if given, overrides this session's default prompt-wait for
        this call only; otherwise the session default set at :meth:`attach`
        time is used (which itself falls back to :attr:`cmd_timeout`).
        """
        await self._session.send(text + "\n")
        wait = timeout if timeout is not None else self._timeout
        try:
            out = await self._session.expect(self.prompt, wait)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self._broken = True
            raise AppShellTimeoutError(
                f"{type(self).__name__}: prompt {self.prompt.pattern!r} not seen "
                f"within {wait}s of {text!r}"
            ) from exc
        body = self._strip_output(text, out)
        if parse is None:
            return ShellResult(Status.Success, value=body, command=text, output=body)
        try:
            value = apply_parse(parse, body)
        except ParseMismatch as exc:
            return ShellResult(Status.Failed, value=None, msg=str(exc), command=text, output=body)
        return ShellResult(Status.Success, value=value, command=text, output=body)

    def _strip_output(self, text: str, out: str) -> str:
        """Reduce a raw ``expect`` capture to the command's output.

        Removes ANSI control sequences, the trailing matched prompt, and a
        leading echoed copy of ``text`` (apps differ on whether they echo the
        input line). A multi-line ``text`` is only echo-matched on its first
        line, which is acceptable for the single-line commands AppShell targets.
        """
        body = _ANSI_RE.sub("", out)
        # `expect` returns data up to and including the prompt match, so the
        # prompt is at the tail — take the last match's start as the cut point.
        last: re.Match[str] | None = None
        for match in self.prompt.finditer(body):
            last = match
        if last is not None:
            body = body[: last.start()]
        # Drop a leading echoed command line, if the app echoed the input.
        first, sep, rest = body.partition("\n")
        if sep and first.rstrip("\r") == text:
            body = rest
        return body

    async def _exit(self) -> None:
        """Leave the REPL and confirm the POSIX shell, always releasing the lock.

        Sends :attr:`quit_cmd` unless the shell is broken (best-effort), then
        runs the command-frame recovery handshake to prove the underlying POSIX
        shell is responsive again. The lock is cleared in a ``finally`` so the
        session is always unlocked, even if quit or recovery raises.
        """
        inner = self._session._session  # noqa: SLF001 — intra-package access to the HostSession's ShellSession for recovery + lock release
        try:
            if self._broken:
                # The REPL is in an unknown/hung state (a prompt wait timed out),
                # so no graceful quit was sent — interrupt it (Ctrl+C / SIGINT)
                # to break out, then confirm the POSIX shell is back.
                await inner._recover_session(deadline=_EXIT_RECOVERY_TIMEOUT)  # noqa: SLF001 — intra-package interrupt+confirm for a hung app
            else:
                # Graceful path: quit_cmd was accepted, so the app is exiting on
                # its own. Do NOT send an interrupt here — a SIGINT racing the
                # app's own exit can interrupt the REPL's stdin read and discard
                # the quit line, wedging it at its prompt forever (it then treats
                # every recovery probe as REPL input and never hands the POSIX
                # shell back — the app-shell recovery e2e flake under CPU load).
                # Just confirm the shell returned, with a load-tolerant budget so
                # a slow-to-quit REPL under starvation still confirms.
                await self._session.send(self.quit_cmd + "\n")
                await inner._confirm_recovered(deadline=_EXIT_RECOVERY_TIMEOUT)  # noqa: SLF001 — intra-package confirm-only after a graceful app exit
        finally:
            inner._app_shell = None  # noqa: SLF001 — always release the app-shell lock
