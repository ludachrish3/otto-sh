"""The netcat ladders' rung scripts, as pure builders.

Each builder returns one shell command string and runs nothing, so a rung can
be run without a transfer backend.

THE CONTRACT EVERY BUILT-IN LISTING SCRIPT KEEPS (the ``ss``, ``netstat`` and
``/proc`` rungs). Stdout is exactly one line: base-10 digits (a free port, or a
listener count) or :data:`TOOL_FAILED`, and a script that prints the sentinel
exits 3. The python port rung prints no sentinel: its exit status is its answer
(see :func:`python_port_command`). The whole script runs in a ``( ... )``
subshell with its stderr sent to ``/dev/null``, the shell's own "not found"
included: a session merges stderr into the value, and a BusyBox ``netstat``
on a kernel without IPv6 complains about ``/proc/net/tcp6`` on stderr, exits
0 and still answers. The subshell also keeps a scan's ``exit`` from ending a
telnet control session. The tool's exit status is captured before its output
is read, so an empty listing means "nothing in use" only when the tool
succeeded. The listing is read by the shell itself (``read``, ``set --``,
``case``) and never by ``grep``: BusyBox 1.16.1's ``grep`` has no ``-x``, and
``grep -c`` exits 1 on a count of 0.

WHY THE LISTENER SCRIPTS COUNT. Measured 2026-09-11 on the bed (nc.openbsd
1.226): the listener binds with ``SO_REUSEPORT``, so a second ``nc -l -p PORT``
from another process coexists on the same port and the kernel hands each
connection to whichever it hashes to. A presence check was satisfied by the
other process's listener and two streams crossed. The caller reads the count
and refuses a port more than one process holds.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..shell_quote import path_operand

TOOL_FAILED = "OTTO_NC_TOOL_FAILED"
"""The line a built-in script prints when its tool is missing or failed."""


def _quiet(body: str) -> str:
    """Run *body* in a subshell whose whole stderr goes to ``/dev/null``."""
    return f"( {body} ) 2>/dev/null"


def _capture(command: str) -> str:
    """Store *command*'s stdout in ``$out``, or print the sentinel and exit 3."""
    return f"out=$({command}) || {{ echo {TOOL_FAILED}; exit 3; }}; "


def _each_line(start: str, step: str, end: str) -> str:
    r"""Feed ``$out`` line by line, split into ``$1``, ``$2``, ..., to *step*.

    ``set -f`` keeps a field such as ``0.0.0.0:*`` from globbing, and
    ``printf '%s\n'`` (not ``echo``) prints the listing unchanged: dash's
    ``echo`` reads backslashes, and a ``\c`` would cut the listing short.
    """
    return (
        f"printf '%s\\n' \"$out\" | "
        f"{{ set -f; {start}while read line; do set -- $line; {step} done; {end} }}"
    )


def _scan(base_port: int, reserved: str) -> str:
    """Print the first port from *base_port* in neither ``$used`` nor *reserved*."""
    return (
        f'reserved=" {reserved} "; '
        f"p={base_port}; "
        "while [ $p -le 65535 ]; do "
        '  case "$reserved" in *" $p "*) p=$((p+1)); continue;; esac; '
        '  case " $used " in *" $p "*) ;; *) echo $p; exit 0;; esac; '
        "  p=$((p+1)); "
        "done; "
        "exit 1;"
    )


# The local address is the fourth field of both `ss -tln` (State Recv-Q
# Send-Q Local Peer) and `netstat -tln` (Proto Recv-Q Send-Q Local Foreign
# State), and its port follows the last colon (`0.0.0.0:22`, `[::]:22`,
# `:::22`). Header words have no colon and match no port.
_LOCAL_PORT = "${4##*:}"


def _listing_port_script(command: str, base_port: int, reserved: str) -> str:
    return _quiet(
        _capture(command)
        + _each_line('used=""; ', f'used="$used {_LOCAL_PORT}";', _scan(base_port, reserved))
    )


def _listing_listener_script(command: str, port: int) -> str:
    return _quiet(
        _capture(command)
        + _each_line("n=0; ", f"case {_LOCAL_PORT} in {port}) n=$((n+1));; esac;", "echo $n;")
    )


def ss_port_script(*, base_port: int, reserved: str) -> str:
    """Build the ``ss`` port scan: the first free port from *base_port*."""
    return _listing_port_script("ss -tln", base_port, reserved)


def netstat_port_script(*, base_port: int, reserved: str) -> str:
    """Build the ``netstat`` port scan: the first free port from *base_port*."""
    return _listing_port_script("netstat -tln", base_port, reserved)


def ss_listener_script(port: int) -> str:
    """Build the count of ``ss`` listeners that hold *port*."""
    return _listing_listener_script(f"ss -tln sport = :{port}", port)


def netstat_listener_script(port: int) -> str:
    """Build the count of ``netstat`` listeners that hold *port*."""
    return _listing_listener_script("netstat -tln", port)


def _read_proc(proc_net: str) -> str:
    """Capture ``tcp``, plus ``tcp6`` when the kernel has one.

    A missing ``tcp6`` is a kernel without IPv6, not a failure; a missing or
    unreadable ``tcp`` is.
    """
    tcp = path_operand(f"{proc_net}/tcp")
    tcp6 = path_operand(f"{proc_net}/tcp6")
    return _capture(f"cat {tcp} && {{ [ ! -e {tcp6} ] || cat {tcp6}; }}")


def proc_port_script(*, base_port: int, reserved: str, proc_net: str = "/proc/net") -> str:
    """Build the ``/proc`` port scan over ``<proc_net>/tcp`` and ``tcp6``.

    The local address is the second field, its port in hex after the colon.
    """
    return _quiet(
        _read_proc(proc_net)
        + _each_line(
            'used=""; ',
            'case $2 in *:*) used="$used $(printf "%d" "0x${2##*:}")";; esac;',
            _scan(base_port, reserved),
        )
    )


def proc_listener_script(port: int, *, proc_net: str = "/proc/net") -> str:
    """Build the count of ``LISTEN`` (``0A``) rows of ``tcp`` and ``tcp6`` on *port*."""
    return _quiet(
        _read_proc(proc_net)
        + _each_line(
            "n=0; ",
            f"case $2 in *:{port:04X}) case $4 in 0A) n=$((n+1));; esac;; esac;",
            "echo $n;",
        )
    )


_PYTHON_PORT = (
    "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()"
)


def python_port_command(exe: str) -> str:
    """Bind port 0 with *exe* and print the port the kernel chose.

    No sentinel: it prints one port and exits 0, or exits non-zero with
    nothing on stdout (a missing interpreter is the shell's 127, its
    complaint dropped).
    """
    return f'{exe} -c "{_PYTHON_PORT}" 2>/dev/null'


_PYTHONS = ("python", "python3")
"""The interpreters the python port rung tries, in order."""

_PROC_NET = "/proc/net"


def _tool_present(tool: str) -> str:
    return f"command -v {tool} >/dev/null 2>&1"


def _proc_present(proc_net: str) -> str:
    return f"test -r {path_operand(f'{proc_net}/tcp')}"


@dataclass(frozen=True)
class PortRung:
    """One rung of the netcat port finder's ladder.

    Attributes:
        name: The rung's name, as ``nc_options.port_strategy`` spells it.
        present: A shell test that exits 0 when the rung can run on the host.
        scripts: ``(base_port, reserved) -> scripts``, tried in order; the
            next runs only after a non-zero exit or a timeout.
        failed: The text that opens this rung's failure message.
        sentinel: Whether the rung's scripts keep the listing contract and
            print :data:`TOOL_FAILED`; ``False`` for python, whose exit status
            is its answer.
        scans: Whether the scripts read the scan's base port and the reserved
            ports; ``False`` for python, where the kernel picks the port, so a
            caller need not work either out.
    """

    name: str
    present: str
    scripts: Callable[[int, str], tuple[str, ...]]
    failed: str
    sentinel: bool
    scans: bool = True


@dataclass(frozen=True)
class ListenerRung:
    """One rung of the netcat listener check's ladder.

    Attributes:
        name: The rung's name, as ``nc_options.listener_check`` spells it.
        present: A shell test that exits 0 when the rung can run on the host.
        script: ``port -> script`` that prints how many listeners hold it.
    """

    name: str
    present: str
    script: Callable[[int], str]


def _ss_port_scripts(base_port: int, reserved: str) -> tuple[str, ...]:
    return (ss_port_script(base_port=base_port, reserved=reserved),)


def _netstat_port_scripts(base_port: int, reserved: str) -> tuple[str, ...]:
    return (netstat_port_script(base_port=base_port, reserved=reserved),)


def _python_port_scripts(base_port: int, reserved: str) -> tuple[str, ...]:
    del base_port, reserved  # the kernel picks the port
    return tuple(python_port_command(exe) for exe in _PYTHONS)


def _proc_port_scripts(base_port: int, reserved: str) -> tuple[str, ...]:
    return (proc_port_script(base_port=base_port, reserved=reserved),)


PORT_LADDER: tuple[PortRung, ...] = (
    PortRung("ss", _tool_present("ss"), _ss_port_scripts, "ss port scan failed", True),
    PortRung(
        "netstat",
        _tool_present("netstat"),
        _netstat_port_scripts,
        "netstat port scan failed",
        True,
    ),
    PortRung(
        "python",
        " || ".join(_tool_present(exe) for exe in _PYTHONS),
        _python_port_scripts,
        "python port discovery failed",
        False,
        scans=False,
    ),
    PortRung(
        "proc",
        _proc_present(_PROC_NET),
        _proc_port_scripts,
        "/proc/net/tcp port scan failed",
        True,
    ),
)
"""The port finder's rungs, in the order ``auto`` tries them: the one place it is spelt."""

LISTENER_LADDER: tuple[ListenerRung, ...] = (
    ListenerRung("ss", _tool_present("ss"), ss_listener_script),
    ListenerRung("netstat", _tool_present("netstat"), netstat_listener_script),
    ListenerRung("proc", _proc_present(_PROC_NET), proc_listener_script),
)
"""The listener check's rungs, in the order ``auto`` tries them: the one place it is spelt."""

CONTROL_AUTO: Final = "auto"
"""The strategy value that walks a ladder in its order."""

CONTROL_CUSTOM: Final = "custom"
"""The strategy value that runs the user's own command."""

CONTROL_VALUES: tuple[str, ...] = (CONTROL_AUTO, CONTROL_CUSTOM)
"""The strategy values that are not rungs: ``auto`` walks a ladder, ``custom`` runs the user's."""

PORT_OPTION: Final = "port_strategy"
"""The ``nc_options`` field that pins a rung of :data:`PORT_LADDER`."""

LISTENER_OPTION: Final = "listener_check"
"""The ``nc_options`` field that pins a rung of :data:`LISTENER_LADDER`."""

LADDER_OPTION: dict[str, str] = {"port": PORT_OPTION, "listener": LISTENER_OPTION}
"""Each ladder's label (``port``, ``listener``) -> the ``nc_options`` field that pins it."""


def _first_present(var: str, ladder: "tuple[PortRung, ...] | tuple[ListenerRung, ...]") -> str:
    """Set ``$<var>`` to the first rung of *ladder* whose ``present`` test passes.

    The last rung is the fallback and is not tested.
    """
    *tested, fallback = ladder
    branches = "".join(
        f"{'if' if at == 0 else 'elif'} {rung.present}; then {var}={rung.name}; "
        for at, rung in enumerate(tested)
    )
    return f"{var}={fallback.name}; " + (f"{branches}fi; " if tested else "")


def presence_probe(
    *,
    port_ladder: "tuple[PortRung, ...]" = PORT_LADDER,
    listener_ladder: "tuple[ListenerRung, ...]" = LISTENER_LADDER,
) -> str:
    """Build the one line ``prepare`` runs; it prints ``"<port rung> <listener rung>"``.

    Each ladder picks its own rung, in its own order: the first whose
    ``present`` test passes. The last rung of each ladder is the fallback and
    is not tested here (see :func:`availability_probe`). Both picks are one
    remote command.

    Args:
        port_ladder: The port finder's rungs, in the order ``auto`` tries them.
        listener_ladder: The listener check's rungs, in the order ``auto``
            tries them.
    """
    return (
        _first_present("port", port_ladder)
        + _first_present("listener", listener_ladder)
        + 'echo "$port $listener"'
    )


def availability_probe(*, proc_net: str = _PROC_NET) -> str:
    """Build a probe that prints every rung whose ``present`` test passes, space separated.

    Each name is printed once, in ladder order, the port ladder's first. It
    is the one place the ``proc`` rungs' readability is asked.

    Args:
        proc_net: The directory the ``proc`` rungs read, for a stand-in.
    """
    tests: dict[str, str] = {}
    for rung in (*PORT_LADDER, *LISTENER_LADDER):
        present = rung.present.replace(_proc_present(_PROC_NET), _proc_present(proc_net))
        tests.setdefault(rung.name, present)
    checks = "".join(f'if {test}; then out="$out {name}"; fi; ' for name, test in tests.items())
    return f'out=""; {checks}echo $out'
