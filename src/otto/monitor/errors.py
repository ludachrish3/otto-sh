"""Refusals raised by the monitor library, importable without the monitor runtime.

``otto monitor`` catches these at its one translation site, and
``otto test --monitor`` turns two of them into a stopped run. This module
imports only :mod:`otto.errors`, so catching them never pulls the collector,
aiosqlite or uvicorn onto a CLI path (import budget).
"""

from ..errors import FieldError, OttoError

_MAX_NAMED_HOSTS = 5
"""How many ids the no-monitorable-hosts message spells out before summarizing.

The ids are what make the message actionable (the fix is per-host lab
config, so "which host" IS the question), but it fires only when EVERY
selected host is unmonitorable, which on a large fleet is a whole-lab
misconfiguration and a wall of ids nobody reads.
"""


class MonitorInputError(FieldError):
    """A monitor input refusal; ``field`` is the parameter at fault.

    One of ``"hosts"``, ``"targets"`` or ``"interval"``. A CLI spells it in
    its own flags at :func:`otto.cli.invoke.usage_error_from`.
    """


class ReviewSourceError(FieldError):
    """A review source that is not a loadable monitor export; ``field`` is ``"source"``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, field="source")


class NoMonitorableHostsError(OttoError):
    """A selection that yielded no host otto can sample.

    Raised only after the selection itself worked: a pattern that matched
    nothing raises :class:`~otto.config.scope.EmptySelectionError` first. So
    the message never offers to widen a pattern. An empty *walked* is the one
    case with nothing selected at all: the lab held no hosts, and the message
    says so without mentioning a pattern.

    Args:
        walked: The ids the selection yielded, monitorable or not.
    """

    def __init__(self, walked: list[str]) -> None:
        self.walked = sorted(walked)
        super().__init__(_message(self.walked))


class MonitorTlsError(OttoError):
    """The repos' declared ``[monitor]`` TLS cannot be served.

    Not a :class:`~otto.errors.FieldError`: no flag spells it, it is a
    settings refusal. ``setting`` is ``"tls_cert"`` or ``"tls_key"`` for a
    file that is missing or unloadable, ``None`` when repos disagree;
    ``repos`` names the declaring repos.
    """

    def __init__(self, message: str, *, setting: "str | None", repos: list[str]) -> None:
        super().__init__(message)
        self.setting = setting
        self.repos = repos


def _message(ids: list[str]) -> str:
    if not ids:
        return "No hosts available in the active lab."
    shown = ", ".join(ids[:_MAX_NAMED_HOSTS])
    if len(ids) > _MAX_NAMED_HOSTS:
        shown += f" (+{len(ids) - _MAX_NAMED_HOSTS} more)"
    return (
        f"{len(ids)} host(s) selected, but none of them can be monitored: {shown}. "
        "otto samples metrics over a shell (Unix hosts) or over SNMP (any host "
        "declaring an `snmp` block in its lab entry), and these declare neither. "
        "The selection is not the problem, so widening it will not help: give the "
        "host(s) you want monitored an `snmp` block, or select a Unix host."
    )
