"""Unified result family for host verbs.

Every ``@cli_exposed`` host verb returns a member of this family (except
``login()``, which returns ``None``): scalar verbs return :class:`Result`
or :class:`CommandResult`; ``run()`` returns :class:`Results`. The CLI derives
its exit code from :attr:`Result.exit_code`.

>>> from otto.utils import Status
>>> r = Result(Status.Success, value=["mod_a"], msg="")
>>> r.is_ok, r.exit_code
(True, 0)
>>> cr = CommandResult(Status.Failed, value="", command="false", retcode=1)
>>> cr.exit_code
1
>>> res = Results.collect([cr])
>>> res.only.command, res.exit_code, bool(res)
('false', 1, False)
"""

from collections.abc import Iterator, Sequence
from dataclasses import FrozenInstanceError, dataclass
from typing import Any, overload

from typing_extensions import Never, override

from otto.errors import OttoError
from otto.utils import Status


class CommandNotRunError(OttoError, RuntimeError):
    """A dry-run synthetic result's value was read as if it were data.

    Defined here rather than in ``otto.host.errors``, where its two peers
    (``HostUnreachableError``/``HostCommandError``) live, because the only
    thing that raises it is ``NotRunResult.value`` in this module -- and
    ``otto.host.errors`` imports ``CommandResult`` from here, so reaching the
    other way is a circular import at runtime AND a layering inversion tach
    rejects (``otto.host`` depends on ``otto.result``, never the reverse).
    Import it from ``otto.result``.
    """

    def __init__(self, command: str, host: str, detail: str = "") -> None:
        """Name the call, the host, and optionally why the preview stopped here.

        *detail* is appended to the standard sentence rather than replacing it,
        so every decline reads the same at the front however it was raised.
        It exists for the one caller whose decline is not a mistake --
        ``expect``, where a session preview legitimately runs out of things it
        can know -- and defaults to empty everywhere else.
        """
        super().__init__(
            f"{command!r} was not run on host {host!r}: this is a dry run, "
            f"which contacts no device. Build previews from configuration; "
            f"if you need the device's answer, drop --dry-run." + (f" {detail}" if detail else "")
        )


@dataclass(frozen=True)
class Result:
    """Outcome of a host verb: status + optional payload + human diagnostic."""

    status: Status
    """Aggregate outcome; see :class:`~otto.utils.Status`."""

    value: Any = None
    """Verb-specific payload (see the per-verb table in the host guide)."""

    msg: str = ""
    """Human diagnostic; empty on success."""

    @property
    def is_ok(self) -> bool:
        """True when :attr:`status` counts as passing (Success or Skipped)."""
        return self.status.is_ok

    def __bool__(self) -> bool:
        """Truthiness follows :attr:`is_ok`, never the payload.

        An empty-but-successful result is truthy; a failed result carrying a
        payload is falsy.
        """
        return self.is_ok

    @property
    def exit_code(self) -> int:
        """The CLI exit code -- 0 when ok, otherwise ``status.value``."""
        return 0 if self.is_ok else self.status.value


@dataclass(frozen=True)
class CommandResult(Result):
    """Result of one shell command; :attr:`~otto.result.Result.value` holds the command's output."""

    command: str = ""
    """The command that was issued."""

    retcode: int = -1
    """Shell return code; -1 means the command never ran."""

    timed_out: bool = False
    """True when the command was killed by its timeout rather than exiting.

    Distinguishes a timeout from an ordinary failure without string-matching
    :attr:`~otto.result.Result.value`; ``retcode`` cannot, since ``-1`` also
    means "never ran" and "skipped: cumulative budget exhausted".
    """

    @override
    @property
    def exit_code(self) -> int:
        """The ssh-like CLI exit code -- the command's own retcode.

        0 when ok; 255 when the command never ran (retcode -1, matching ssh's
        connection-error convention); ``status.value`` when the command exited
        0 but otto marked it failed (e.g. an expect mismatch).
        """
        if self.is_ok:
            return 0
        if self.retcode == -1:
            return 255
        if self.retcode != 0:
            return self.retcode
        return self.status.value


class NotRunResult(CommandResult):
    """`CommandResult` for a command a dry run declined to issue.

    `value` RAISES on read: the one thing a synthetic result must never be is
    parseable. Everything else (`status`, `retcode`, `timed_out`, `command`)
    stays readable so fire-and-forget callers keep working.

    Deliberately NOT re-decorated with ``@dataclass``: the inherited generated
    ``__init__`` keeps every other field working, while ``value`` becomes a
    class-level data descriptor that no instance attribute can shadow.
    Re-decorating would turn ``value`` back into a plain field and silently
    restore the poison pill.

    ONE LINE DECIDES EVERY DUNDER BELOW: raising is correct where the caller
    is READING THE MEASUREMENT, and wrong where the caller is merely HANDLING
    THE OBJECT. Handling must never raise -- ``repr``, ``str``, f-strings,
    ``==``, ``hash`` and containment are what logging, assertions and
    collections do incidentally, and an error thrown there reports the mistake
    at a line that made none. That is the same defect this class exists to
    kill, pointed the other way: a log line that explodes tells the operator a
    different wrong story just as effectively as a fabricated payload does. So
    the generated dunders that reach through ``self.value`` are all replaced
    below with ones that do not.
    """

    host_name: str = ""

    def __init__(self, *args: Any, host_name: str = "", **kwargs: Any) -> None:
        object.__setattr__(self, "host_name", host_name)
        super().__init__(*args, **kwargs)

    def _identity(self) -> tuple[Any, ...]:
        """Every field that says WHICH declined command this is, minus ``value``.

        ``host_name`` is in here and is not a dataclass field, which is the
        whole reason equality and hashing are hand-written rather than
        delegated: two declines of the same command on different hosts are
        different objects, and the generated dunders cannot see the attribute
        that says so.
        """
        return (self.status, self.msg, self.command, self.retcode, self.timed_out, self.host_name)

    @property
    @override
    def value(self) -> Any:
        """Always raises -- a declined command measured nothing to return.

        No colon in that summary line, deliberately: napoleon reads a property
        docstring's ``prefix: rest`` as ``type: description`` and ``-W -n``
        then fails on the unresolvable ``prefix`` class.
        """
        raise CommandNotRunError(self.command, self.host_name)

    @value.setter
    @override
    def value(self, _payload: Any) -> None:
        """Absorb and DISCARD writes; there is no payload to keep.

        Required, not decorative. A frozen dataclass's generated ``__init__``
        assigns through ``object.__setattr__`` to bypass the frozen
        ``__setattr__`` -- but ``object.__setattr__`` still honours data
        descriptors on the type, so a property with no setter makes
        ``NotRunResult(...)`` itself raise ``AttributeError: can't set
        attribute 'value'`` before any caller can read anything. Swallowing
        the write is also the correct semantics: dropping the payload is the
        whole point, and it is what keeps a smuggled ``value=`` argument out
        of the instance dict rather than merely outranked.

        Ordinary attribute assignment is unaffected -- ``r.value = x`` still
        hits :meth:`__setattr__` below and raises ``FrozenInstanceError``.
        """

    # ── Handling the object: none of these may raise ──────────────────────

    @override
    def __repr__(self) -> str:
        """Render the absence as ``value=<not run>`` rather than omitting it.

        Naming the field and saying why beats dropping it. A log line missing
        ``value`` reads as an empty result, which is the fiction; ``<not run>``
        tells the reader no measurement was ever taken.
        """
        return (
            f"{type(self).__name__}(status={self.status!r}, value=<not run>, "
            f"msg={self.msg!r}, command={self.command!r}, retcode={self.retcode!r}, "
            f"timed_out={self.timed_out!r}, host_name={self.host_name!r})"
        )

    @override
    def __eq__(self, other: object) -> bool:
        """Equal when the same command was declined on the same host.

        The ``type`` check is what keeps a non-measurement from ever comparing
        equal to a real :class:`CommandResult` -- including one whose fields
        otherwise match -- so ``result == expected`` cannot quietly accept a
        decline in place of an answer.
        """
        if type(other) is not type(self):
            return NotImplemented
        return self._identity() == other._identity()

    @override
    def __hash__(self) -> int:
        """Hash the identity, so a decline can live in a set or a dict key.

        Defined explicitly because declaring ``__eq__`` would otherwise set
        ``__hash__`` to None and make every instance unhashable -- turning a
        ``result in seen`` into a TypeError at another innocent line.
        """
        return hash((type(self), self._identity()))

    @override
    def __setattr__(self, name: str, value: Any) -> Never:
        """Reject every assignment, which the inherited version does not.

        The generated frozen ``__setattr__`` refuses a name only when it is a
        declared dataclass field OR when ``type(self)`` is the decorated class
        itself. This class is neither decorated nor does it declare
        ``host_name`` as a field, so ``nr.host_name = 'other'`` silently
        succeeded and made every later error message name the WRONG host --
        and ``nr.typo = 1`` stuck too. Every other member of the Result family
        rejects all assignment; so does this one now.

        Construction is unaffected: ``__init__`` assigns through
        ``object.__setattr__``, which does not consult this method.
        """
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    @override
    def __delattr__(self, name: str) -> Never:
        """Reject every deletion, for the same reason as :meth:`__setattr__`."""
        raise FrozenInstanceError(f"cannot delete field {name!r}")


@dataclass(frozen=True)
class ShellResult(Result):
    """Result of one :class:`~otto.host.app_shell.AppShell` command.

    :attr:`~otto.result.Result.value` holds the parsed object (or the raw
    output when no parser was given); :attr:`output` always keeps the raw,
    prompt-stripped text for debugging.
    """

    command: str = ""
    """The line sent to the application shell."""

    output: str = ""
    """Raw output between the echoed command and the next prompt."""


@dataclass(frozen=True)
class Results(Result, Sequence[CommandResult]):
    """Aggregate over per-command results; itself a :class:`Result`.

    Returned by ``run()`` only. :attr:`~otto.result.Result.value` is
    ``list[CommandResult]`` in execution order. Build with :meth:`collect`,
    which computes the aggregate status: ``Success`` when every entry is ok,
    otherwise the first non-ok entry's status. Truthiness follows
    :attr:`~otto.result.Result.is_ok`, not emptiness.
    """

    @classmethod
    def collect(cls, items: Sequence[CommandResult], msg: str = "") -> "Results":
        """Build a Results from per-command entries, computing the aggregate."""
        entries = list(items)
        status = next((e.status for e in entries if not e.is_ok), Status.Success)
        return cls(status=status, value=entries, msg=msg)

    @override
    def __len__(self) -> int:
        return len(self.value)

    @overload
    def __getitem__(self, index: int) -> CommandResult: ...
    @overload
    def __getitem__(self, index: slice) -> list[CommandResult]: ...
    @override
    def __getitem__(self, index: int | slice) -> "CommandResult | list[CommandResult]":
        return self.value[index]

    @override
    def __iter__(self) -> Iterator[CommandResult]:
        return iter(self.value)

    @property
    def only(self) -> CommandResult:
        """The sole entry when exactly one command ran; ValueError otherwise."""
        if len(self.value) != 1:
            raise ValueError(
                f"Results.only requires exactly 1 command result, got {len(self.value)}"
            )
        return self.value[0]

    @property
    def first_failure(self) -> CommandResult | None:
        """The first non-ok entry, or None when everything passed."""
        return next((e for e in self.value if not e.is_ok), None)

    @override
    @property
    def exit_code(self) -> int:
        """0 when ok, else the first failing command's :attr:`exit_code`."""
        failure = self.first_failure
        return 0 if failure is None else failure.exit_code
