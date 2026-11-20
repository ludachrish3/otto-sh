"""Per-command / per-host logging disposition.

``LogMode`` decides *where* a host's command I/O is recorded — independent of the
log *level* (INFO vs DEBUG), which stays native to the ``logger.info``/
``logger.debug`` call (the three-sink logging spec).
"""

# Spec: docs/superpowers/specs/2026-06-28-three-sink-logging-design.md

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class LogMode(Enum):
    """Disposition of a host's command echo/output across the log sinks.

    - ``NORMAL`` — logged at the call's level, shown everywhere.
    - ``QUIET`` — suppressed from the console + ``console.log``, kept in ``verbose.log``.
    - ``NEVER`` — redacted from every sink at every level, including session diagnostics.

    ``LogMode`` governs command I/O only; ``logger.warning``/``logger.error`` and
    other non-command records are never suppressed by it.
    """

    NORMAL = "normal"
    QUIET = "quiet"
    NEVER = "never"

    @property
    def rank(self) -> int:
        """Restrictiveness rank, ascending ``NORMAL`` (0), ``QUIET`` (1), ``NEVER`` (2)."""
        return _RANK[self]


_RANK = {LogMode.NORMAL: 0, LogMode.QUIET: 1, LogMode.NEVER: 2}


def effective_mode(*modes: LogMode) -> LogMode:
    """Return the most restrictive of *modes* (``NORMAL`` when called with none)."""
    return max(modes, key=lambda m: m.rank, default=LogMode.NORMAL)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecLogContext:
    """Whose log one delegated ``exec`` call's lines go to, and under which mode.

    Built once by the host that delegates a command to another host's
    ``exec`` (a container to its parent) and carried on the call's
    :class:`~otto.host.command_frame.ExecInput`. The route announces
    *command* (the text the caller issued) on *log_command* and relays every
    output line to *log_output* (the delegating host's own sinks) under
    *mode*, which is already resolved: the route folds nothing over it.
    """

    command: str
    mode: LogMode
    log_command: "Callable[[str, LogMode], None]"
    log_output: "Callable[[str, LogMode], None]"
