"""Pure probe builders and parsers for ``otto tunnel check`` (spec §5).

The check builds a throwaway tunnel and pushes payloads through it: round
trips on one flow, a checksum-compared bulk transfer, and a no-data TCP
handshake. It also launches tagged socat echo listeners, built and
discovered the same way a real tunnel process is (:mod:`otto.host.daemon`).

Every function here is a pure string builder or a pure parser — nothing in
this module touches a device. The scripts are handed to a host's shell
through ``sh -c`` (see :func:`otto.check.fingerprint.one_command`), so each
one is wrapped in :func:`otto.check.clock.bash_script` to opt into bash's
``$EPOCHREALTIME`` clock regardless of the host's default shell.
"""

import re
from dataclasses import dataclass

from ..check.clock import PROBE_TIMEOUT_S, SOCAT_CONNECT_S, bash_script, parse_elapsed_ms
from ..host.daemon import encode_token, launch_command, split_token

CHECK_PREFIX = "otto-check"
CHECK_VERSION = "v1"
PS_FAILED = "@ps-failed"
"""The line :data:`SWEEP_COMMAND` prints when ``ps`` itself failed, so found-nothing and
could-not-look read differently (the marker ``otto link check``'s sweep prints too)."""
SWEEP_COMMAND = (
    f"{{ ps -eo pid= -eo etime= -eo args= 2>/dev/null || echo {PS_FAILED}; }} | "
    f"\\grep -a -e ' {CHECK_PREFIX}:' -e '^{PS_FAILED}$' || true"
)
"""Portable ``ps`` scan for this check's own tagged echoes, with their pid and age.

The same scan as :func:`~otto.host.daemon.ps_scan_command` (see it for the
separate ``-eo`` flags, ``etime`` and ``\\grep``), except that a ``ps`` that
rejects those fields (a busybox one built without its desktop options) prints
:data:`PS_FAILED` instead of nothing: the check then says it could not look,
never that no echo runs there. ``tunnel list``'s discovery keeps the shared
scan.

Its ``' otto-check:'`` needle never matches the link check's
``otto-check-<6hex>`` tags — those have a hyphen where this has a colon."""

SMALL_SIZES = [1, 1400]
"""Payload sizes round-tripped on one flow, timed per trip."""

BULK_SIZE = {"tcp": 65536, "udp": 65000}
"""One bulk transfer size per protocol, checksum-compared on return."""

TRIPS = 5
"""Round trips per small size on one flow."""

_BULK_UDP_EOF_WAIT_S = 3
"""socat's ``-t`` (post-EOF wait) for the UDP bulk client only.

UDP has no end-of-stream signal, so once the payload's single write hits EOF
the client can only wait out its own ``-t`` timer for a reply — at the full
:data:`~otto.check.clock.PROBE_TIMEOUT_S` that would stall the whole bulk
probe for many seconds per direction. TCP keeps the full timeout: its FIN
lets the client return as soon as the peer is done replying."""

HANDSHAKE_DRAIN_S = 2
"""How long the ``--dest`` handshake waits for a quiet device to close.

The handshake sends FIN first, then reads and discards what the device
says, because closing with unread data would reset the device instead
(see :func:`handshake_script`). A device that answers FIN by closing ends
the wait at once. One that stays open but goes quiet ends it after this
many seconds of quiet, bounding the probe with nothing left unread. Two
seconds covers a console's greeting burst without slowing the check."""

_PAYLOAD_SEGMENTS = 5


@dataclass(frozen=True, slots=True)
class EchoTag:
    """One tagged socat echo listener launched for the check."""

    run: str
    """6-hex run token, one per check invocation."""

    tunnel_id: str
    """The throwaway tunnel this echo serves — ``"-"`` for a segment echo."""

    protocol: str
    role: str
    """``"fwd-echo"``, ``"rev-echo"`` or ``"segment"``."""

    host_id: str


def echo_sentinel(tag: EchoTag) -> str:
    """Wire token for *tag*, launched as the echo daemon's ``argv[0]``."""
    # Every segment is a slug otto already controls (make_host_id, a
    # tun-<12hex>-<port> tunnel id or "-", a 6-hex run token, tcp/udp, the
    # fixed role names) — none can contain ':', so unlike
    # otto.tunnel.sentinel's compound path segment, nothing here needs
    # percent-encoding before it's handed to encode_token.
    return encode_token(
        CHECK_PREFIX,
        CHECK_VERSION,
        [tag.run, tag.tunnel_id, tag.protocol, tag.role, tag.host_id],
    )


def parse_echo_sentinel(token: str) -> EchoTag | None:
    """Parse one echo sentinel; ``None`` for non-otto-check / other-version / malformed."""
    payload = split_token(token, CHECK_PREFIX, CHECK_VERSION, _PAYLOAD_SEGMENTS)
    if payload is None:
        return None
    run, tunnel_id, protocol, role, host_id = payload
    return EchoTag(run=run, tunnel_id=tunnel_id, protocol=protocol, role=role, host_id=host_id)


def echo_argv(protocol: str, bind_ip: str, port: int) -> list[str]:
    """Build the socat argv for a tagged echo listener bound to *bind_ip*:*port*.

    ``PIPE`` echoes every byte straight back. UDP additionally carries
    ``-T 30``, a backstop for a forked child whose reply stalls: a UDP
    socket never sees a peer disconnect. It bounds children only; neither
    listening parent ever times out, so the check's sweep is what ends them.
    """
    if protocol == "tcp":
        return [
            "socat",
            "-b",
            "65535",
            f"TCP4-LISTEN:{port},bind={bind_ip},fork,reuseaddr",
            "PIPE",
        ]
    if protocol == "udp":
        return [
            "socat",
            "-b",
            "65535",
            "-T",
            "30",
            f"UDP4-RECVFROM:{port},bind={bind_ip},fork",
            "PIPE",
        ]
    raise ValueError(f"unknown protocol {protocol!r}")


def echo_launch_command(tag: EchoTag, protocol: str, bind_ip: str, port: int) -> str:
    """Launch an echo listener tagged *tag*, the same way a tunnel process launches."""
    return launch_command(echo_sentinel(tag), echo_argv(protocol, bind_ip, port))


def _client(protocol: str, ip: str, port: int, *, eof_wait: int = PROBE_TIMEOUT_S) -> str:
    """Socat client bounded by its own timers: connect, inactivity, and the wait after EOF.

    ``-T`` (inactivity) always stays at the full :data:`PROBE_TIMEOUT_S`.
    *eof_wait* sets only ``-t``, socat's wait after the client's stdin hits
    EOF for a last reply. :func:`bulk_script` passes
    :data:`_BULK_UDP_EOF_WAIT_S` for UDP instead of the full
    :data:`PROBE_TIMEOUT_S` — see that constant's docstring for why.
    """
    addr = (
        f"TCP4:{ip}:{port},connect-timeout={SOCAT_CONNECT_S}"
        if protocol == "tcp"
        else f"UDP4:{ip}:{port}"
    )
    return f"socat -b 65535 -T {PROBE_TIMEOUT_S} -t {eof_wait} - {addr}"


def trips_script(protocol: str, ip: str, port: int, size: int, trips: int = TRIPS) -> str:
    """Round trips of a fresh printable nonce on ONE flow, stopping at the first non-ok trip.

    One coprocess socat is one TCP connection or one UDP flow (one source
    port), so the trips share a flow — ``coproc S { exec ...; }``, not a bare
    compound command, so ``$S_PID`` names the socat process itself rather
    than an intermediate subshell; without ``exec``, the teardown's
    ``kill "$S_PID"`` would only kill that subshell and orphan socat. Its
    stderr is merged into the coprocess's own stdout (``2>&1``) so a
    connection refusal or similar failure text is readable evidence instead
    of vanishing into an unmonitored fd.

    Every trip is timed against :data:`~otto.check.clock.PROBE_TIMEOUT_S`
    (``-t`` on ``read``), so *trips* consecutive full-timeout trips could
    otherwise run *trips* x ``PROBE_TIMEOUT_S`` — comfortably past
    ``otto.check.CHECK_HOST_TIMEOUT`` at the default ``TRIPS``, and a late
    reply to an abandoned trip would poison the next one's read. The loop
    therefore STOPS at the first trip that is not a clean match; the summary
    line is always printed regardless of how the loop ends, so the caller
    always learns how many trips matched before the flow gave up.

    A dead or already-closed client can make ``printf`` write to a broken
    pipe; ``trap '' PIPE`` keeps that from killing the whole script (bash
    would otherwise die of SIGPIPE — exit 141, no output at all). The
    following ``read``'s own exit status then tells a genuine timeout
    (> 128, from its own ``-t``) apart from the flow having already closed
    (any other non-zero status: EOF with fewer than *size* bytes read),
    reporting the latter as ``closed`` — with what was actually read, for
    evidence — rather than a misleading ``timeout``.

    That ignored SIGPIPE is inherited by the nonce generator's ``tr`` too (an
    ignored signal stays ignored across ``exec``): when ``head -c`` has its
    bytes and closes the pipe, ``tr`` gets EPIPE instead of dying silently and
    prints ``tr: write error: Broken pipe`` on every trip — noise a host
    merges into the transcript (found on the live bed, bash 5.2 and 4.2
    alike). ``tr``'s stderr is therefore discarded, which would also hide a
    generator that fails outright; the length check right after it keeps that
    visible as its own ``nonce`` verdict, reported at once, instead of an
    empty payload waiting out a full ``timeout`` that would blame the tunnel.

    Payloads are printable and at most 1400 B, under the pipe's atomic write
    size (PIPE_BUF), so socat reads each one in a single read and sends it as
    a single datagram. ``read -N`` takes exactly *size* bytes back
    (``LC_ALL=C`` makes that bytes, not characters). Without
    ``$EPOCHREALTIME`` (bash < 5) the timestamps are empty and only the
    match verdicts count.
    """
    client = _client(protocol, ip, port)
    body = (
        "export LC_ALL=C; trap '' PIPE; "
        f"coproc S {{ exec {client} 2>&1; }}; m=0; "
        f"for ((i=1; i<={trips}; i++)); do "
        f"p=$(tr -dc a-z0-9 </dev/urandom 2>/dev/null | head -c {size}); "
        f'if [ ${{#p}} -ne {size} ]; then echo "trip $i nonce got=${{#p}}"; break; fi; '
        's=$EPOCHREALTIME; printf %s "$p" >&"${S[1]}"; '
        f'IFS= read -r -N {size} -t {PROBE_TIMEOUT_S} got <&"${{S[0]}}"; rc=$?; '
        "if [ $rc -eq 0 ]; then e=$EPOCHREALTIME; "
        'if [ "$got" = "$p" ]; then m=$((m+1)); echo "trip $i ok $s $e"; '
        'else echo "trip $i mismatch got=${#got}"; break; fi; '
        'elif [ $rc -gt 128 ]; then echo "trip $i timeout"; break; '
        'else echo "trip $i closed got=${#got} ${got}"; break; fi; '
        "done; "
        f'echo "trips {trips} matched $m"; kill "$S_PID" 2>/dev/null; wait 2>/dev/null; true'
    )
    return bash_script(body)


def bulk_script(protocol: str, ip: str, port: int, size: int) -> str:
    """One *size*-byte random payload from a REGULAR FILE, checksum-compared on return.

    A regular file, not a pipe: socat then reads the whole payload in one
    read, so a UDP payload leaves as ONE datagram (a pipe could hand it over
    in pieces). No timing claim for UDP (the client waits its EOF timer for a
    reply that has no end-of-stream), so only TCP's elapsed time is
    meaningful; the parser reports it and the caller ignores it for UDP.

    The verdict requires ``$a`` to be NON-EMPTY before comparing it to
    ``$b``: a host missing ``cksum`` makes both checksums the empty string,
    and a bare ``[ "$a" = "$b" ]`` would call that a false ``match``.
    :func:`parse_bulk` layers a second guard on top (``sent == got > 0``) for
    the same reason.
    """
    eof_wait = _BULK_UDP_EOF_WAIT_S if protocol == "udp" else PROBE_TIMEOUT_S
    client = _client(protocol, ip, port, eof_wait=eof_wait)
    body = (
        'f=$(mktemp) && r=$(mktemp) || exit 1; trap \'rm -f "$f" "$r"\' EXIT; '
        f'head -c {size} /dev/urandom >"$f"; s=$EPOCHREALTIME; '
        f'{client} <"$f" >"$r" 2>/dev/null; e=$EPOCHREALTIME; '
        'a=$(cksum <"$f" 2>/dev/null); b=$(cksum <"$r" 2>/dev/null); '
        'if [ -n "$a" ] && [ "$a" = "$b" ]; then v=match; else v=mismatch; fi; '
        'echo "sent=$(wc -c <"$f" | tr -d " ") got=$(wc -c <"$r" | tr -d " ") $v"; echo "$s $e"'
    )
    return bash_script(body)


def handshake_script(ip: str, port: int) -> str:
    """TCP handshake to a device that runs nothing of otto's, closed with FIN (spec §5.3).

    The target is the user's real ``--port``, often a console that talks
    first: telnet negotiation, a banner, a prompt. The script never sends a
    byte, but it must still read what the device says. ``close()`` on a TCP
    socket with unread received data sends RST instead of FIN, and a
    device reset in the middle of its own send can wedge it. A Zephyr
    ``shell_telnet`` console, for one, tears the client down, sees a stale
    ``POLLERR``, and fails to rebind (-112) or goes dead.

    So socat runs bidirectionally with stdin from ``/dev/null``. It reads
    EOF at once and half-closes the socket, so the device sees our FIN
    before anything else. The device's replies go to ``/dev/null`` until it
    closes, or until it has been quiet for :data:`HANDSHAKE_DRAIN_S`
    (socat's ``-t``). Either way socat exits 0 with nothing left unread,
    and the kernel's close sends nothing but FIN. socat's stderr is kept as
    the evidence for a failed connect.

    ``-t`` is re-armed by every read, so a device that never stops talking
    would keep socat alive forever. A watchdog built from bash builtins
    (``read -t`` on a pipe that never delivers, so no ``sleep`` or
    ``timeout`` is needed) kills socat after
    :data:`~otto.check.clock.SOCAT_CONNECT_S` plus :data:`HANDSHAKE_DRAIN_S`.
    A socat still running then has connected, since ``connect-timeout``
    would have failed it by now, so a cut-off drain is still
    ``handshake ok``. No client can avoid a reset from a device that keeps
    sending after the client has gone.

    The verdict is ``handshake ok`` when socat exits 0 or the watchdog cut
    it off, and ``handshake failed`` otherwise, after socat's own error
    text. The watchdog kills only on a genuine ``read`` timeout (status
    above 128). A shell that cannot build its pipe never kills, and socat
    is left to its own timers.
    """
    cutoff = SOCAT_CONNECT_S + HANDSHAKE_DRAIN_S
    body = (
        f"socat -t {HANDSHAKE_DRAIN_S} - TCP4:{ip}:{port},connect-timeout={SOCAT_CONNECT_S} "
        "</dev/null 2>&1 >/dev/null & p=$!; "
        f'{{ read -t {cutoff} <> <(:); [ $? -gt 128 ] && kill "$p" && '
        f'echo "still sending after {cutoff}s: drain cut off"; }} 2>/dev/null & w=$!; '
        'wait "$p" 2>/dev/null; rc=$?; kill "$w" 2>/dev/null; wait "$w" 2>/dev/null; cut=$?; '
        "if [ $rc -eq 0 ] || [ $cut -eq 0 ]; then echo handshake ok; else echo handshake failed; fi"
    )
    return bash_script(body)


@dataclass(frozen=True, slots=True)
class TripsResult:
    """Parsed output of :func:`trips_script`."""

    matched: int
    """Replies byte-identical to what was sent; capped at *trips*."""

    trips: int
    rtts_ms: list[float]
    """One per matched trip with a clock reading; empty without one."""

    said: str
    """The raw output, for evidence."""


_TRIP_OK_FIELDS = 3
_TRIP_TIMED_FIELDS = 5


def parse_trips(output: str, trips: int) -> TripsResult:
    """Parse :func:`trips_script` output. Total: garbage or empty output matches nothing.

    A ``nonce``, ``mismatch``, ``timeout``, ``closed`` or unrecognised line is
    simply not ``ok`` and is never counted; ``matched`` is additionally capped at
    *trips* so a corrupted transcript can never overcount.
    """
    matched = 0
    rtts_ms: list[float] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < _TRIP_OK_FIELDS or fields[0] != "trip" or fields[2] != "ok":
            continue
        if matched >= trips:
            continue
        matched += 1
        if len(fields) >= _TRIP_TIMED_FIELDS:
            try:
                start, end = float(fields[3]), float(fields[4])
            except ValueError:
                continue
            rtts_ms.append((end - start) * 1000)
    return TripsResult(matched=matched, trips=trips, rtts_ms=rtts_ms, said=output)


@dataclass(frozen=True, slots=True)
class BulkResult:
    """Parsed output of :func:`bulk_script`."""

    sent: int
    got: int
    match: bool
    elapsed_ms: float | None
    said: str
    """The raw output, for evidence."""


_BULK_RE = re.compile(r"sent=(\d+)\s+got=(\d+)\s+(match|mismatch)")


def parse_bulk(output: str) -> BulkResult:
    """Parse :func:`bulk_script` output. Total: garbage or empty output is a non-match.

    ``match`` additionally requires ``sent == got > 0`` on top of the
    script's own verdict word — the same defence, on the parser side, as
    :func:`bulk_script`'s own ``[ -n "$a" ]`` guard against a host missing
    ``cksum``.
    """
    m = _BULK_RE.search(output)
    if m is None:
        return BulkResult(sent=0, got=0, match=False, elapsed_ms=None, said=output)
    sent, got, verdict = int(m.group(1)), int(m.group(2)), m.group(3)
    match = verdict == "match" and sent == got and got > 0
    return BulkResult(
        sent=sent,
        got=got,
        match=match,
        elapsed_ms=parse_elapsed_ms(output),
        said=output,
    )
