"""Pure command/argv builders for host-resident socat tunnels — no I/O.

Every value here is a string or list of strings destined for ``host.oneshot``;
running nothing keeps the whole module unit-testable (assert exact argv).
"""

import re
import shlex

# Old-stable socat address keywords only (compatible down to procps/socat on
# Linux 2.6.32). ``fork`` lets one listener serve repeated datagrams/connections;
# ``reuseaddr`` avoids TIME_WAIT bind failures on teardown+re-add.
_LISTEN = {"udp": "UDP4-LISTEN", "tcp": "TCP4-LISTEN"}
_DELIVER = {"udp": "UDP4", "tcp": "TCP4"}

DISCOVERY_PS_COMMAND: str = "ps -eo pid=,etime=,args= 2>/dev/null | grep -a ' otto-link:' || true"
"""Portable ``ps`` used by discovery (formatted ``etime`` — ``etimes`` is
procps>=3.3, too new for 2.6.32-era userland); ``|| true`` so a no-match grep
(exit 1) is not treated as a command failure."""

FREE_PORT_PROBE_COMMAND: str = "ss -Htln 2>/dev/null || netstat -tln 2>/dev/null || true"
"""Free-port probe run on the exit host — ``ss`` preferred, ``netstat``
fallback (both exist on CentOS 6). Parsed by :func:`parse_listening_ports`."""

_PORT_RE = re.compile(r":(\d{1,5})\b")
_MAX_PORT = 65535


def ingress_socat_args(
    protocol: str, service_port: int, exit_ip: str, carrier_port: int
) -> list[str]:
    """Accept client traffic on service port, ship over TCP carrier to exit host."""
    listen = _LISTEN[protocol]
    return [
        "socat",
        f"{listen}:{service_port},fork,reuseaddr",
        f"TCP4:{exit_ip}:{carrier_port}",
    ]


def egress_socat_args(
    protocol: str, service_port: int, dest_ip: str, carrier_port: int
) -> list[str]:
    """Accept TCP carrier, deliver to destination on service port.

    dest_ip = the exit host itself, or a relay target for ``--dest``.
    """
    deliver = _DELIVER[protocol]
    return [
        "socat",
        f"TCP4-LISTEN:{carrier_port},fork,reuseaddr",
        f"{deliver}:{dest_ip}:{service_port}",
    ]


def launch_command(sentinel: str, socat_args: list[str]) -> str:
    """Build the ``host.oneshot`` line for a detached, tagged, session-surviving tunnel.

    ``bash -c 'exec -a "$1" "${@:2}"' _ <sentinel> <socat argv…>`` sets the
    process's ``argv[0]`` to the sentinel (``exec -a`` — a bash builtin; bash is
    required on tunnel hosts). ``socat_args`` is the FULL program argv (it begins
    with ``"socat"``), so the template must NOT hardcode ``socat`` — hardcoding it
    runs ``socat socat <addr> <addr>`` and dies on the bogus third address.

    Surviving the ssh session is the subtle part (found via live-bed e2e). On a
    systemd host, a process left in the ssh session's scope is killed when that
    session ends, and ``setsid`` does NOT escape the session cgroup — so we
    launch it in the USER manager's scope via ``systemd-run --user`` (no sudo, no
    root; the transient unit is ``--collect``ed on exit). On a non-systemd host
    (older distros — the portability floor), ``systemd-run`` is absent and a
    plain ``setsid``-detached background process survives normally, so we fall
    back to that.
    """
    inner = shlex.quote('exec -a "$1" "${@:2}"')
    tagged = " ".join(shlex.quote(a) for a in (sentinel, *socat_args))
    systemd = f"systemd-run --user --collect --quiet -- bash -c {inner} _ {tagged}"
    setsid = f"setsid bash -c {inner} _ {tagged} </dev/null >/dev/null 2>&1 &"
    return f"if command -v systemd-run >/dev/null 2>&1; then {systemd}; else ( {setsid} ); fi"


def parse_listening_ports(output: str) -> set[int]:
    """Extract every port appearing as ``:<port>`` in ss/netstat output.

    Safe superset of used ports — we only need to avoid them.
    """
    return {int(m) for m in _PORT_RE.findall(output) if 0 < int(m) <= _MAX_PORT}


def pick_free_port(used: set[int], lo: int = 49152, hi: int = 65535) -> int:
    """First port in ``[lo, hi]`` not in ``used``. Raises when exhausted."""
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise RuntimeError(f"no free TCP port in [{lo}, {hi}]")
