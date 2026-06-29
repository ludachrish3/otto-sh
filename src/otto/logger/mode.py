"""Per-command / per-host logging disposition.

``LogMode`` decides *where* a host's command I/O is recorded — independent of the
log *level* (INFO vs DEBUG), which stays native to the ``logger.info``/
``logger.debug`` call. See ``docs/superpowers/specs/2026-06-28-three-sink-logging-design.md``.
"""

from enum import Enum


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
