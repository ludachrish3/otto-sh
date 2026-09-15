"""The dial tier: a TCP connect and a bounded banner read, from where the real path connects.

Direct hosts are dialed from the controller. A hopped host is dialed ON the
hop, over the hop's existing session, so the observation is made from the
same vantage the connect path uses. Banners decide the service; an open port
with no banner is open and unclassified, never folded into a timeout.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..connections import teardown_step

if TYPE_CHECKING:
    from ...result import CommandResult
    from ..userland import Userland

HOP_DIAL_BASH = (
    "timeout {t} bash -c 'exec 3<>/dev/tcp/{ip}/{port} && echo OTTO_STATE=open && "
    "timeout {r:g} cat <&3 | head -c {n} | od -An -v -tx1' 2>&1; echo OTTO_RC=$?"
)
"""Connect, then read the banner with ONLY the reader under the inner timeout.

The reader has to be the last thing killed and the only thing killed. The
first shape of this script was ``timeout {t} ... head -c 256 <&3 | od``, and
it could never report a banner from a real service: ``head -c`` writes
nothing until it has all 256 bytes or sees EOF, and ssh/ftp/telnet send 12-43
bytes and then WAIT for the client -- so ``head`` sat on the bytes, and
``timeout`` killed its whole process group (bash, head AND od) on expiry, so
no hex ever reached stdout. Every hopped open port read ``open, no banner
within Ns`` about a service that had answered in milliseconds, which killed
discovery and the swept-console promotion on every hopped host.

``timeout {r} cat`` fixes it because coreutils ``timeout`` runs its child in
a NEW process group: expiry kills ``cat`` alone. ``cat`` does not buffer, so
what arrived is already in the pipe; ``head`` then sees EOF, bounds the
bytes and exits; ``od`` -- outside the inner timeout's group, and still
inside the outer one, which has not fired -- runs to completion and prints
the partial banner.

The OUTER timeout stays and is what bounds the CONNECT: ``exec 3<>/dev/tcp``
is a bash builtin with no bound of its own, so a SYN-dropped port would
otherwise hang for the kernel's ~2 minutes. The reader's bound is therefore
strictly smaller than the outer one (:func:`_reader_timeout`) so that the
whole command still costs at most ``t`` -- the budget the sweep sizes itself
against -- and ``OTTO_RC=124`` keeps meaning exactly one thing: the connect
never completed.
"""
HOP_DIAL_NC = "{prefix}nc -z -w {t} {ip} {port} 2>&1; echo OTTO_RC=$?"
"""The applet fallback: ``nc -z`` is zero-I/O by construction, so no banner.

Nothing here reads bytes, so the buffering defect HOP_DIAL_BASH had cannot
exist in this arm, and its ``open (... no banner read)`` detail already says
what it did rather than claiming a banner failed to arrive. Reading one would
cost a SECOND connect to the port (a BusyBox ``nc`` that is not in ``-z``
mode gives no separable exit status once it is in a pipeline), and a second
client on a port whose first verdict is already in hand is precisely what
the embedded sweep was corrected NOT to do.
"""
_TIMEOUT_PREFIX = {"coreutils": "timeout {t} ", "dash-t": "timeout -t {t} "}
_BANNER_BYTES = 256
_READER_GRACE_S = 1.0
"""Seconds of the outer bound reserved for the connect, ``od`` and the fork costs."""
_READER_FLOOR_S = 0.5
"""The reader's smallest bound; a real banner arrives in single-digit milliseconds."""
_RC = re.compile(r"^OTTO_RC=(\d+)$")
_TIMEOUT_RCS = {"124", "143"}


@dataclass(frozen=True, slots=True)
class DialOutcome:
    """What one dial (direct or hop-side) found at one (ip, port).

    ``open``, ``closed``, ``timeout`` or ``not-checkable``, plus the banner, if any.
    """

    state: Literal["open", "closed", "timeout", "not-checkable"]
    service: str | None = None
    banner: str = ""
    detail: str = ""


def classify_banner(data: bytes) -> str | None:
    """``ssh`` / ``ftp`` / ``telnet`` from the first bytes a server sends, else ``None``."""
    if data.startswith(b"SSH-"):
        return "ssh"
    if data.startswith(b"220"):
        return "ftp"
    if data[:1] == b"\xff":
        return "telnet"
    return None


async def dial_direct(ip: str, port: int, *, timeout: float) -> DialOutcome:
    """Connect from the controller and read up to 256 banner bytes within *timeout*."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except ConnectionRefusedError:
        return DialOutcome(state="closed", detail="connection refused")
    except (TimeoutError, asyncio.TimeoutError):
        return DialOutcome(state="timeout", detail=f"no connect within {timeout:g}s")
    except OSError as exc:
        return DialOutcome(state="not-checkable", detail=f"{type(exc).__name__}: {exc}")
    reset = False
    eof = False
    try:
        data = await asyncio.wait_for(reader.read(_BANNER_BYTES), timeout)
        # An empty read is a clean EOF, not a silent service: the peer accepted
        # and then hung up (tcpwrappers, a single-client console refusing the
        # second client). Saying "no banner within Ns" about it reports a
        # timeout that did not happen.
        eof = not data
    except (TimeoutError, asyncio.TimeoutError):
        data = b""
    except OSError:
        # The port accepted the connection, so the dial itself succeeded — a
        # mid-read reset (sshd at MaxStartups, tcpwrappers, a busy embedded
        # stack) is honestly `open`, never an exception escaping the tier.
        data = b""
        reset = True
    finally:
        with teardown_step("survey", "dial close"):
            writer.close()
    with teardown_step("survey", "dial wait_closed"):
        await writer.wait_closed()
    if not data:
        if reset:
            detail = "reset after connect"
        elif eof:
            detail = "open, closed by peer before any banner"
        else:
            detail = f"open, no banner within {timeout:g}s"
        return DialOutcome(state="open", detail=detail)
    text = data.decode("utf-8", "replace")
    detail = text.strip().splitlines()[0][:80] if text.strip() else ""
    return DialOutcome(state="open", service=classify_banner(data), banner=text, detail=detail)


def _reader_timeout(t: int) -> float:
    """Return the banner read's own bound, always strictly inside the outer bound *t*.

    What is left over is the connect's budget: at least
    :data:`_READER_GRACE_S`, which over a hop's own link is three orders of
    magnitude more than a TCP handshake costs. Keeping the sum at *t* is what
    lets the embedded sweep keep sizing its budget from the per-dial timeout
    it was given.
    """
    return max(_READER_FLOOR_S, t - _READER_GRACE_S)


def hop_dial_script(
    ip: str, port: int, *, timeout: float, userland: "Userland | None"
) -> str | None:
    """Return the command the hop runs, or ``None`` when the hop offers no dial tool."""
    t = int(max(1, round(timeout)))
    if userland is None or userland.shell_dialect == "bash":
        return HOP_DIAL_BASH.format(t=t, r=_reader_timeout(t), n=_BANNER_BYTES, ip=ip, port=port)
    if userland.has_applet("nc") == "present":
        prefix = _TIMEOUT_PREFIX.get(userland.timeout_style, "").format(t=t)
        return HOP_DIAL_NC.format(prefix=prefix, t=t, ip=ip, port=port)
    return None


_HEX_BYTE_WIDTH = 2


def _is_hex_byte(word: str) -> bool:
    return len(word) == _HEX_BYTE_WIDTH and all(c in "0123456789abcdef" for c in word)


def _decode_hex(lines: list[str]) -> bytes:
    """Decode ``od -An -v -tx1`` lines: a banner line is ALL hex-pair words, or it is not one.

    Restricted per line (not scavenged word-by-word across the merged
    ``2>&1`` stream) so a stray two-character hex-looking token inside an
    unrelated stderr line — ``bash: ab cd: command not found`` — can never
    contribute banner bytes.
    """
    hexes: list[str] = []
    for line in lines:
        words = line.split()
        if words and all(_is_hex_byte(word) for word in words):
            hexes.extend(words)
    return bytes(int(h, 16) for h in hexes)


def _outcome_from_hop_output(text: str, hop_id: str, timeout: float) -> DialOutcome:
    """Classify the hop's raw stdout+rc-marker text into one :class:`DialOutcome`."""
    lines = [line.strip() for line in text.splitlines()]
    rc = ""
    for line in lines:
        rc_match = _RC.match(line)
        if rc_match:
            rc = rc_match.group(1)
            break
    body = [line for line in lines if not line.startswith("OTTO_")]
    if any(line == "OTTO_STATE=open" for line in lines):
        data = _decode_hex(body)
        banner = data.decode("utf-8", "replace")
        no_banner = f"open, no banner within {timeout:g}s"
        detail = banner.strip().splitlines()[0][:80] if banner.strip() else no_banner
        service = classify_banner(data)
        return DialOutcome(state="open", service=service, banner=banner, detail=detail)
    if rc in _TIMEOUT_RCS:
        detail = f"no connect within {timeout:g}s (from hop {hop_id})"
        return DialOutcome(state="timeout", detail=detail)
    if rc == "0":
        return DialOutcome(state="open", detail=f"open (nc -z from hop {hop_id}, no banner read)")
    if "refused" in text.lower():
        return DialOutcome(state="closed", detail=f"connection refused (from hop {hop_id})")
    first = body[0] if body else f"rc {rc or '?'}"
    return DialOutcome(state="not-checkable", detail=f"hop {hop_id}: {first[:80]}")


async def dial_via_hop(
    run: "Callable[[str], Awaitable[CommandResult]]",
    hop_id: str,
    ip: str,
    port: int,
    *,
    timeout: float,
    userland: "Userland | None",
) -> DialOutcome:
    """Dial *ip*:*port* from the hop and classify what came back."""
    script = hop_dial_script(ip, port, timeout=timeout, userland=userland)
    if script is None:
        return DialOutcome(state="not-checkable", detail=f"hop {hop_id} offers no dial tool")
    try:
        result = await run(script)
    # A hop that cannot even run the dial is not-checkable, with its reason.
    except Exception as exc:  # noqa: BLE001
        detail = f"hop {hop_id}: {type(exc).__name__}: {exc}"
        return DialOutcome(state="not-checkable", detail=detail)
    text = str(result.value)
    return _outcome_from_hop_output(text, hop_id, timeout)
