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
from dataclasses import dataclass
from typing import Any, overload

from typing_extensions import override

from otto.utils import Status


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
