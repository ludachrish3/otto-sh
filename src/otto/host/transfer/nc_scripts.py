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
