"""The shared probe clock for the setup checks.

``otto link check`` and ``otto tunnel check`` both time TCP
probes on hosts whose default shell is not bash: the check hands every
command to ``sh -c`` (dash or BusyBox ash on many hosts), where
``$EPOCHREALTIME`` expands to nothing and the elapsed time would not parse.
:func:`bash_script` wraps a probe script so it runs under bash regardless,
and :func:`parse_elapsed_ms` reads back what that clock printed.
"""

import shlex

PROBE_TIMEOUT_S = 15
"""How long a timed probe client may take before it gives up on its own.

Well under the check's per-command host timeout
(``otto.check.CHECK_HOST_TIMEOUT``), so a probe that stalls —
a path that drops full-size packets, a listener that accepts and never
answers — ends as the probe's own failure, with its output, instead of as a
host that stopped answering."""

SOCAT_CONNECT_S = 5
"""socat's ``connect-timeout``, capped at the probe's bound: a link-local connect is instant."""


def bash_script(script: str) -> str:
    """Run *script* under bash: the socat clients' clock is bash's ``$EPOCHREALTIME``.

    The check hands every command to ``sh -c`` (dash or BusyBox ash on many
    hosts), where ``$EPOCHREALTIME`` expands to nothing and the elapsed time
    would not parse.
    """
    return f"bash -c {shlex.quote(script)}"


def parse_elapsed_ms(output: str) -> float | None:
    """Elapsed time in ms from a timed client's last output line.

    Accepts either one float (already ms, the python3 backend) or two floats
    ``START END`` in seconds (bash ``$EPOCHREALTIME``, the socat backend) on
    the last non-blank line. ``None`` when that line is empty or doesn't
    parse — e.g. an old bash where ``$EPOCHREALTIME`` expands to nothing.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    fields = lines[-1].split()
    try:
        match fields:
            case [ms]:
                return float(ms)
            case [start, end]:
                return (float(end) - float(start)) * 1000
    except ValueError:
        return None
    return None
