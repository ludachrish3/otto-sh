"""Probe command builders and output parsers for ``otto link check``.

Pure functions only: command strings the check runs on lab hosts, and
parsers for what those commands print back. No host I/O happens here — the
check orchestrator owns the ssh/exec layer and hands this module raw text.

Two areas:

- Ping: :func:`ping_command` builds the command; :func:`parse_ping` reads
  either GNU (iputils) or BusyBox ``ping`` output into :class:`PingStats`.
- The probe kit: a detached TCP echo listener (:func:`listener_command`) and
  two timed clients (:func:`timed_connect_command`,
  :func:`timed_transfer_command`) built for whichever backend
  (:data:`ProbeBackend`) the target host actually has, plus
  :func:`parse_elapsed_ms` for the elapsed-time text they print. Every
  client ends on its own within :data:`PROBE_TIMEOUT_S`.
"""

import re
import shlex
import statistics
from dataclasses import dataclass, field
from typing import Literal

_REPLY_RE = re.compile(r"(?:icmp_)?seq=(\d+)\b.*?\btime[=<]([\d.]+)\s*ms(.*)$")
_TX_RE = re.compile(r"(\d+) packets transmitted")


@dataclass(frozen=True)
class PingStats:
    """Per-reply statistics parsed from one ``ping`` run.

    ``rtts`` and ``seqs`` hold one entry per unique reply, in arrival order —
    duplicate replies (``(DUP!)``) are excluded from both lists and counted
    in ``duplicates`` instead.
    """

    transmitted: int
    """Probes sent, from the summary's ``N packets transmitted``."""

    rtts: list[float] = field(default_factory=list)
    """Round-trip times in ms, one per unique reply, arrival order."""

    seqs: list[int] = field(default_factory=list)
    """ICMP sequence numbers, one per unique reply, arrival order."""

    duplicates: int = 0
    """Count of replies whose sequence number had already been seen."""

    @property
    def received(self) -> int:
        """Unique replies received (duplicates excluded)."""
        return len(self.rtts)

    @property
    def loss_pct(self) -> float:
        """Percent of transmitted probes with no unique reply.

        ``0.0`` when nothing was transmitted (rather than dividing by zero).
        """
        if self.transmitted == 0:
            return 0.0
        return 100.0 * (self.transmitted - self.received) / self.transmitted

    @property
    def avg(self) -> float | None:
        """Mean RTT in ms, or ``None`` when no replies arrived."""
        if not self.rtts:
            return None
        return statistics.fmean(self.rtts)

    @property
    def sd(self) -> float | None:
        """Population standard deviation of RTTs in ms.

        ``0.0`` for exactly one reply, ``None`` when no replies arrived.
        """
        if not self.rtts:
            return None
        if len(self.rtts) == 1:
            return 0.0
        return statistics.pstdev(self.rtts)

    @property
    def out_of_order(self) -> int:
        """Count of replies whose sequence number is lower than an earlier arrival's.

        I.e. ``i`` where ``seqs[i] < max(seqs[:i])``.
        """
        count = 0
        running_max: int | None = None
        for seq in self.seqs:
            if running_max is not None and seq < running_max:
                count += 1
            else:
                running_max = seq if running_max is None else max(running_max, seq)
        return count


def parse_ping(output: str) -> PingStats | None:
    """Per-reply statistics from GNU (iputils) or BusyBox ``ping`` output.

    Statistics come from the reply lines, never the summary's rtt line:
    BusyBox prints no ``mdev``, and duplicates must be excluded from the
    timing sample but counted. Returns ``None`` when no
    ``N packets transmitted`` summary line is found (e.g. ``ping`` never ran
    a probe at all).
    """
    tx = _TX_RE.search(output)
    if tx is None:
        return None
    rtts: list[float] = []
    seqs: list[int] = []
    dups = 0
    for line in output.splitlines():
        m = _REPLY_RE.search(line)
        if m is None:
            continue
        if "DUP!" in m.group(3):
            dups += 1
            continue
        seqs.append(int(m.group(1)))
        rtts.append(float(m.group(2)))
    return PingStats(transmitted=int(tx.group(1)), rtts=rtts, seqs=seqs, duplicates=dups)


def ping_command(target: str, *, count: int, interval: float) -> str:
    """Build a ``ping`` invocation."""
    return f"ping -c {count} -i {interval} -W 2 {target}"


ProbeBackend = Literal["python3", "socat"]
"""Which tool builds the echo listener and timed clients on a lab host."""


def pick_backend(tools: dict[str, bool]) -> ProbeBackend | None:
    """Pick the probe kit backend a host supports, python3 first, then socat.

    *tools* maps a tool name to whether the host has it (e.g. from a
    fingerprint probe). ``None`` when neither is available.
    """
    if tools.get("python3"):
        return "python3"
    if tools.get("socat"):
        return "socat"
    return None


PROBE_TIMEOUT_S = 15
"""How long a timed probe client may take before it gives up on its own.

Well under the check's per-command host timeout
(``otto.check.CHECK_HOST_TIMEOUT``), so a probe that stalls —
a path that drops full-size packets, a listener that accepts and never
answers — ends as the probe's own failure, with its output, instead of as a
host that stopped answering."""

_SOCAT_CONNECT_S = 5
"""socat's ``connect-timeout``, capped at the probe's bound: a link-local connect is instant."""

_PY_ECHO = (
    "import socket\n"
    "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(({bind!r}, {port})); s.listen(16)\n"
    "while True:\n"
    "    c, _ = s.accept()\n"
    "    while True:\n"
    "        d = c.recv(65536)\n"
    "        if not d: break\n"
    "        c.sendall(d)\n"
    "    c.close()\n"
)


def listener_command(
    backend: ProbeBackend,
    port: int,
    *,
    tag: str,
    netns: str | None = None,
    bind: str | None = None,
) -> str:
    """Detached echo listener on *port*, tagged so teardown can find it via ``pkill -f``.

    *bind* is the one address the listener accepts on; ``None`` takes every
    address. The sandbox's listeners live in a private namespace and take
    every address, while a live check's bind only the far endpoint's address
    on the link, so they never answer on (or collide with) the host's other
    interfaces.

    socat is tagged via ``exec -a`` (it's a normal binary; renaming its
    argv[0] is harmless). python3 keeps its real argv[0] and carries the tag
    as a trailing script argument instead — an ``exec -a`` rename breaks a
    relocatable CPython build (uv-managed, pyenv, Nix) by defeating the
    argv[0]-based fallback it uses to locate its own stdlib, which fails
    silently here since the listener's output is redirected to
    ``/dev/null``.
    """
    prefix = f"ip netns exec {netns} " if netns else ""
    if backend == "python3":
        # Python binds every address to the empty host string.
        program = _PY_ECHO.format(bind=bind or "", port=port)
        body = f"python3 -c {shlex.quote(program)} {shlex.quote(tag)}"
    else:
        where = f",bind={bind}" if bind else ""
        body = f"exec -a {tag} socat TCP-LISTEN:{port}{where},fork,reuseaddr PIPE"
    return f"{prefix}setsid bash -c {shlex.quote(body)} >/dev/null 2>&1 < /dev/null &"


def _bash(script: str) -> str:
    """Run *script* under bash: the socat clients' clock is bash's ``$EPOCHREALTIME``.

    The check hands every command to ``sh -c`` (dash or BusyBox ash on many
    hosts), where ``$EPOCHREALTIME`` expands to nothing and the elapsed time
    would not parse.
    """
    return f"bash -c {shlex.quote(script)}"


_PY_CLOCK = (
    "import socket, sys, time\n"
    "t0 = time.monotonic()\n"
    "end = t0 + {within}\n"
    "def left():\n"
    "    r = end - time.monotonic()\n"
    "    if r <= 0: raise socket.timeout('timed out')\n"
    "    return r\n"
)
"""The python3 clients' shared head: one wall-clock deadline every socket call counts down to."""


def _socat(ip: str, port: int, within: float) -> str:
    """Build the socat client every timed probe pipes through, bounded by socat's own timers.

    socat has no wall-clock limit, so its three timers bound a stall instead:
    ``connect-timeout`` the connect, ``-T`` a transfer that stops moving, and
    ``-t`` the wait for the echo once the input is sent. A stall anywhere
    ends within *within* seconds of the last byte that moved.
    """
    connect = min(_SOCAT_CONNECT_S, within)
    return f"socat -T {within:g} -t {within:g} - TCP:{ip}:{port},connect-timeout={connect:g}"


def timed_connect_command(
    backend: ProbeBackend, ip: str, port: int, *, within: float = PROBE_TIMEOUT_S
) -> str:
    """One fresh connect + 1-byte echo, timed on the host; gives up after *within* seconds.

    Exits non-zero unless the byte came back: a service that is not otto's
    echo answers something else (or nothing), and must never read as a
    listener that is up.
    """
    if backend == "python3":
        return "python3 -c " + shlex.quote(
            _PY_CLOCK.format(within=within) + "got = None\n"
            "try:\n"
            f"    s = socket.create_connection(('{ip}', {port}), timeout=left())\n"
            "    s.sendall(b'x'); s.settimeout(left()); got = s.recv(1); s.close()\n"
            "except OSError as e:\n"
            "    print(repr(e))\n"
            "if got not in (None, b'x'): print('echo answered %r, not x' % got)\n"
            "print((time.monotonic() - t0) * 1000)\n"
            "sys.exit(0 if got == b'x' else 1)\n"
        )
    return _bash(
        "s=$EPOCHREALTIME; "
        f"echo x | {_socat(ip, port, within)} | grep -qx x; r=$?; "
        'e=$EPOCHREALTIME; echo "$s $e"; exit $r'
    )


def timed_transfer_command(
    backend: ProbeBackend, ip: str, port: int, nbytes: int, *, within: float = PROBE_TIMEOUT_S
) -> str:
    """Send *nbytes* through the echo and read them back, timed on the host.

    Exits non-zero unless every byte came back, and gives up after *within*
    seconds (python3: wall clock; socat: its own timers, see ``_socat``).
    """
    if backend == "python3":
        return "python3 -c " + shlex.quote(
            _PY_CLOCK.format(within=within) + "n = 0\n"
            "try:\n"
            f"    s = socket.create_connection(('{ip}', {port}), timeout=left())\n"
            f"    s.sendall(b'\\0' * {nbytes}); s.shutdown(socket.SHUT_WR)\n"
            "    while True:\n"
            "        s.settimeout(left())\n"
            "        d = s.recv(65536)\n"
            "        if not d: break\n"
            "        n += len(d)\n"
            "except OSError as e:\n"
            "    print(repr(e))\n"
            f"if n != {nbytes}: print('echoed %d of {nbytes} bytes' % n)\n"
            "print((time.monotonic() - t0) * 1000)\n"
            f"sys.exit(0 if n == {nbytes} else 1)\n"
        )
    return _bash(
        "s=$EPOCHREALTIME; "
        f"n=$(head -c {nbytes} /dev/zero | {_socat(ip, port, within)} | wc -c); "
        "e=$EPOCHREALTIME; n=$((n+0)); "
        f'[ $n -eq {nbytes} ] || echo "echoed $n of {nbytes} bytes"; '
        f'echo "$s $e"; [ $n -eq {nbytes} ]'
    )


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
