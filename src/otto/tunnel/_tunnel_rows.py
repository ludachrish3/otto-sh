"""The probe rows of ``otto tunnel check``: segments, payloads and their verdicts.

:mod:`otto.tunnel.check` runs the check: it validates, fingerprints, sweeps,
builds, lists and tears down. This module holds each probe row and the
verdict it earns; the tagged echo listeners the probes aim at live in
``otto.tunnel._tunnel_echoes``. Every
host command goes through :func:`~otto.check.fingerprint.check_read`. A
command that fails on a host that answered is a row's verdict, never an
exception; only a host that does not answer raises
:class:`~otto.check.CheckHostUnreachableError`, host-named.
"""

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..check import (
    FeatureResult,
    HostFingerprint,
    UnmeasuredReason,
    Verdict,
)
from ..check.fingerprint import TUNNEL_TOOLS, check_read
from ._tunnel_echoes import EchoStart, kill_matching, listen_command, start_echo
from .check_probes import (
    BULK_SIZE,
    SMALL_SIZES,
    TRIPS,
    EchoTag,
    bulk_script,
    handshake_script,
    parse_bulk,
    parse_trips,
    trips_script,
)
from .discovery import DISCOVERY_PS_COMMAND, parse_process_discovery
from .manage import ResolvedHop
from .model import Role
from .socat import parse_port_holders

LOOPBACK = "127.0.0.1"
"""Where the throwaway tunnel's egress delivers, and so where its two echoes listen."""

SERVICE_PORT_ROW = "service port"
BUILD_ROW = "build"
RTT_ROW = "rtt"
LIST_ROW = "list"
TEARDOWN_ROW = "teardown"

FWD_ECHO = "fwd-echo"
REV_ECHO = "rev-echo"
SEGMENT_ECHO = "segment"

_ADD_TOOLS = ["socat", "bash"]
"""The tools ``otto tunnel add`` itself checks every hop for; the rest are the probes' own."""
_PAYLOAD_VERIFIED = "payload-verified"
HANDSHAKE_ONLY = "handshake only — the payload is not verified (#440)"
NO_REPLY_ORACLE = "UDP to a device that runs nothing of otto's has no reply to check (#440)"


def _named_naturally(items: list[str]) -> str:
    """Join *items* as English prose: ``"a"``, ``"a and b"``, ``"a, b and c"``."""
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def swept_detail(tunnel_id: str, *, tunnel_gone: bool, echoes_gone: list[EchoTag]) -> str:
    """Name exactly what vanished before this run's teardown, and what most likely took it.

    Every check's start-of-run sweep takes any run's echoes once they are
    older than :data:`~otto.check.sweep.SWEEP_MIN_AGE_S`, so a run that outlives
    that bound can lose its tunnel, its delivery echoes, or both, mid-run, to
    a second check started on the same hosts. *tunnel_gone* says ``tunnel_id`` was
    already absent from what removing it reported removed; *echoes_gone* are
    this run's tagged delivery echoes that were seen running once they started,
    and that the clean-up scan no longer found.

    A sweep removes the tunnel first, so a vanished tunnel is blamed on one.
    Echoes that vanished while the tunnel was still there may as well have
    exited on their own (one that lost its bind to another run's echo can
    pass the one scan that confirms it before it goes), so that row names
    both causes. The row names precisely what vanished, never a blanket
    claim — and never an internal ``"; "``, which would collide with the
    ``"; "`` other teardown problems are joined with.
    """
    named = [f"tunnel {tunnel_id}"] if tunnel_gone else []
    named += [f"the {tag.role} on {tag.host_id}" for tag in echoes_gone]
    subject = _named_naturally(named)
    were = "were" if len(named) > 1 else "was"
    if tunnel_gone:
        cause = "removed mid-run, most likely by another check's sweep on these hosts"
    elif len(named) > 1:
        cause = "they exited, or another check's sweep took them"
    else:
        cause = "it exited, or another check's sweep took it"
    return (
        f"{subject} {were} gone before teardown — {cause}, and the payload rows above are not "
        "evidence"
    )


_SWEPT_SAID = "not proven — its tunnel or echoes were gone before teardown"
_HANDSHAKE_OK = "handshake ok"
_HANDSHAKE_TOOLS = ["socat", "bash"]
"""What :func:`~otto.tunnel.check_probes.handshake_script` runs on the last hop."""
_HANDSHAKE_HINT = (
    "the handshake goes from {last} straight to --port {port} on {dest}: is the device's own "
    "service listening there, and does {last} reach it?"
)
_HELD_HINT = (
    "the real tunnel on --port {port} would fail its post-add verify: free the port or pick another"
)


def bulk_label(protocol: str) -> str:
    """How a row names *protocol*'s bulk size: ``64 KiB`` for TCP, ``65000 B`` for UDP."""
    size = BULK_SIZE[protocol]
    return f"{size // 1024} KiB" if size % 1024 == 0 else f"{size} B"


def payload_rows(direction: str, protocol: str) -> list[str]:
    """Name one direction's payload rows, in order: the small sizes, then the bulk."""
    return [
        *(f"{direction} {size} B" for size in SMALL_SIZES),
        f"{direction} {bulk_label(protocol)}",
    ]


def segment_row_name(a: str, b: str) -> str:
    """Name the row proving one hop pair, e.g. ``segment test1 → test2``."""
    return f"segment {a} → {b}"


def last_segment_row_name(dest: str) -> str:
    """Name the split proof's row for the leg into a device that runs nothing of otto's."""
    return f"last segment → {dest}"


def _echo_failed(name: str, start: EchoStart) -> FeatureResult:
    return FeatureResult(
        name, Verdict.FAIL, detail=start.problem, commands=start.commands, output=start.output
    )


# --------------------------------------------------------------------------
# Rows before the build
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingTools:
    """Tools the check needs that some hop lacks: the row detail, and which tools they are."""

    detail: str
    """Each gap as ``<tools> not found on <host>``, joined with ``; ``."""
    tools: list[str]
    """Every missing tool, once each, in :data:`~otto.check.TUNNEL_TOOLS` order."""


def missing_tools(fingerprints: list[HostFingerprint]) -> MissingTools | None:
    """Name each hop's missing :data:`~otto.check.TUNNEL_TOOLS`, or ``None`` when none is."""
    gaps = []
    tools: list[str] = []
    for fp in fingerprints:
        absent = [tool for tool in TUNNEL_TOOLS if not fp.tools.get(tool)]
        if absent:
            gaps.append(f"{', '.join(absent)} not found on {fp.host_id}")
            tools += absent
    if not gaps:
        return None
    return MissingTools("; ".join(gaps), [t for t in TUNNEL_TOOLS if t in tools])


def missing_tool_hint(tools: list[str]) -> str:
    """Say what to do about *tools*, and whether ``otto tunnel add`` would refuse the path too.

    ``add`` itself checks every hop for socat and bash only, so only those
    carry the claim; the others are needed by the check's own probes.

    >>> "would refuse" in missing_tool_hint(["socat"]), "would refuse" in missing_tool_hint(["wc"])
    (True, False)
    """
    for_add = [t for t in tools if t in _ADD_TOOLS]
    for_probes = [t for t in tools if t not in _ADD_TOOLS]
    said = []
    if for_add:
        said.append(
            f"`otto tunnel add` needs {', '.join(for_add)} on every hop and would refuse "
            "this path too"
        )
    if for_probes:
        said.append(
            f"the check's probes need {', '.join(for_probes)} on every hop "
            "(`otto tunnel add` itself does not)"
        )
    said.append(f"install {'it' if len(tools) == 1 else 'them'}, then check again")
    return "; ".join(said)


def unmeasured_missing(names: list[str], missing: MissingTools) -> list[FeatureResult]:
    """Every row in *names* as ``unmeasured (missing-tool)``, naming the tools and hosts."""
    return [
        FeatureResult(
            name,
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.MISSING_TOOL,
            detail=missing.detail,
            hint=missing_tool_hint(missing.tools),
        )
        for name in names
    ]


async def _otto_tunnel_on(host: Any, protocol: str, port: int) -> str | None:
    """Find an otto tunnel whose ingress binds *port* on *host*; return its id, or ``None``."""
    scanned = await check_read(host, DISCOVERY_PS_COMMAND)
    for obs in parse_process_discovery(scanned.value or ""):
        tunnel = obs.parsed.tunnel
        if (
            obs.parsed.role is Role.INGRESS
            and tunnel.protocol == protocol
            and tunnel.service_port == port
        ):
            return tunnel.id
    return None


async def service_port_row(endpoints: list[Any], protocol: str, port: int) -> FeatureResult:
    """Whether *port* is free for *protocol* on both endpoints — looked at, never bound."""
    cmd = listen_command(protocol)
    said: list[str] = []
    held: list[str] = []
    holders: list[str] = []
    blind: list[str] = []
    ids: list[str] = []
    for host in endpoints:
        result = await check_read(host, cmd)
        said.append(f"{host.id}: {result.value or ''}".rstrip())
        if not result.is_ok:
            blind.append(host.id)
            continue
        lines = parse_port_holders(result.value or "", port)
        held += [f"{host.id}: {line}" for line in lines]
        if lines:
            holders.append(host.id)
            tunnel_id = await _otto_tunnel_on(host, protocol, port)
            if tunnel_id is not None:
                ids.append(f"{tunnel_id} on {host.id}")
    commands, output = [cmd], "\n".join(said)
    if held:
        detail = (
            f"an otto tunnel already binds it: {', '.join(ids)}"
            if ids
            else f"{protocol} port {port} is already bound on {', '.join(holders)}"
        )
        return FeatureResult(
            SERVICE_PORT_ROW,
            Verdict.FAIL,
            measured="; ".join(held),
            wanted="free",
            detail=detail,
            hint=_HELD_HINT.format(port=port),
            commands=commands,
            output=output,
        )
    if blind:
        return FeatureResult(
            SERVICE_PORT_ROW,
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.MISSING_TOOL,
            detail=f"neither ss nor netstat could list {protocol} sockets on {', '.join(blind)}",
            commands=commands,
            output=output,
        )
    return FeatureResult(
        SERVICE_PORT_ROW, Verdict.PASS, measured="free", commands=commands, output=output
    )


@dataclass(frozen=True)
class SegmentResult:
    """One hop pair's row, and its round-trip time when the clock gave one."""

    row: FeatureResult
    rtt_ms: float | None = None


async def segment_row(
    a: ResolvedHop, b: ResolvedHop, protocol: str, port: int, run: str
) -> SegmentResult:
    """Prove hop pair *a* → *b*: an echo on *b* at its own address, 1 B trips from *a*.

    All :data:`TRIPS` trips must come back, and the round trip reported is their median:
    the same trips, on one flow, as the tunnel's own ``fwd 1 B`` row, so the
    ``rtt`` row compares like with like. The echo is killed in a
    ``finally``, whatever the trips did.
    """
    name = segment_row_name(a.hop.host, b.hop.host)
    tag = EchoTag(run, "-", protocol, SEGMENT_ECHO, b.hop.host)
    try:
        start = await start_echo(b.host, tag, b.ip, port)
        if start.problem is not None:
            return SegmentResult(_echo_failed(name, start))
        script = trips_script(protocol, b.ip, port, 1, trips=TRIPS)
        result = await check_read(a.host, script)
        trips = parse_trips(result.value or "", TRIPS)
        commands, output = [script], result.value
        if trips.matched == TRIPS:
            rtt = statistics.median(trips.rtts_ms) if trips.rtts_ms else None
            measured = f"{TRIPS}/{TRIPS} echoed"
            if rtt is not None:
                measured += f", median {rtt:.1f} ms round trip"
            return SegmentResult(
                FeatureResult(
                    name, Verdict.PASS, measured=measured, commands=commands, output=output
                ),
                rtt,
            )
        where = f"{a.hop.host} → {b.hop.host}"
        said = (
            f"{where}: no echo from {b.ip}:{port}"
            if trips.matched == 0
            else f"{where}: {trips.matched} of {TRIPS} trips echoed back from {b.ip}:{port}"
        )
        return SegmentResult(
            FeatureResult(name, Verdict.FAIL, detail=said, commands=commands, output=output)
        )
    finally:
        await kill_matching(b.host, lambda t: t.run == run and t.role == SEGMENT_ECHO)


async def last_segment_row(
    last: ResolvedHop,
    last_fp: HostFingerprint,
    dest: ResolvedHop,
    protocol: str,
    port: int,
) -> FeatureResult:
    """Prove the split proof's last leg: *last* to a *dest* that runs nothing of otto's.

    TCP makes a data-less handshake from *last* to the dest's real *port*
    (``--port``); a completed one is all it shows. It closes with FIN, never
    RST: it half-closes at once and drains what the device says before it
    goes (see :func:`~otto.tunnel.check_probes.handshake_script`). It needs
    socat and bash on *last* (*last_fp* says whether they are there), and is
    ``unmeasured (missing-tool)`` without them. UDP has no reply to check, so it
    is ``unmeasured (no-reply-oracle)`` whatever the hops have. No command ever
    runs on the dest.
    """
    name = last_segment_row_name(dest.hop.host)
    if protocol != "tcp":
        return FeatureResult(
            name,
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.NO_REPLY_ORACLE,
            detail=NO_REPLY_ORACLE,
        )
    absent = [tool for tool in _HANDSHAKE_TOOLS if not last_fp.tools.get(tool)]
    if absent:
        gap = MissingTools(f"{', '.join(absent)} not found on {last.hop.host}", absent)
        return unmeasured_missing([name], gap)[0]
    script = handshake_script(dest.ip, port)
    result = await check_read(last.host, script)
    said = result.value or ""
    if _HANDSHAKE_OK in [line.strip() for line in said.splitlines()]:
        return FeatureResult(
            name,
            Verdict.PASS,
            measured="connected",
            detail=HANDSHAKE_ONLY,
            commands=[script],
            output=result.value,
        )
    return FeatureResult(
        name,
        Verdict.FAIL,
        detail=f"{last.hop.host} → {dest.hop.host}: no TCP handshake with {dest.ip}:{port}",
        hint=_HANDSHAKE_HINT.format(last=last.hop.host, port=port, dest=dest.hop.host),
        commands=[script],
        output=result.value,
    )


# --------------------------------------------------------------------------
# Rows through the built tunnel
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Payloads:
    """One direction's payload rows, and the round-trip times its 1-byte trips gave."""

    rows: list[FeatureResult]
    one_byte_rtts_ms: list[float] = field(default_factory=list)


async def payloads(hop: ResolvedHop, direction: str, protocol: str, port: int) -> Payloads:
    """Push every payload from *hop* into the tunnel at *hop*'s own address, one at a time."""
    names = payload_rows(direction, protocol)
    rows: list[FeatureResult] = []
    rtts: list[float] = []
    for name, size in zip(names[: len(SMALL_SIZES)], SMALL_SIZES, strict=True):
        script = trips_script(protocol, hop.ip, port, size)
        result = await check_read(hop.host, script)
        trips = parse_trips(result.value or "", TRIPS)
        verdict = Verdict.PASS if trips.matched == TRIPS else Verdict.FAIL
        rows.append(
            FeatureResult(
                name,
                verdict,
                measured=f"{trips.matched}/{TRIPS} echoed byte for byte",
                wanted=f"{TRIPS}/{TRIPS}",
                commands=[script],
                output=result.value,
            )
        )
        if size == 1:
            rtts = trips.rtts_ms
    script = bulk_script(protocol, hop.ip, port, BULK_SIZE[protocol])
    result = await check_read(hop.host, script)
    bulk = parse_bulk(result.value or "")
    ok = bulk.match and bulk.got == bulk.sent
    rows.append(
        FeatureResult(
            names[-1],
            Verdict.PASS if ok else Verdict.FAIL,
            measured=f"got {bulk.got} of {bulk.sent} B",
            wanted=f"{BULK_SIZE[protocol]} B, checksum-identical",
            commands=[script],
            output=result.value,
        )
    )
    return Payloads(rows, rtts)


def _has_clock(fp: HostFingerprint) -> bool:
    return fp.versions.get("epochrealtime") == "yes"


def rtt_row(
    tunnel_rtts_ms: list[float], segment_rtts_ms: list[float | None], hop0: HostFingerprint
) -> FeatureResult:
    """Report the median 1-byte FWD round trip through the tunnel, next to the segments' sum.

    Each segment's figure is the median of its own :data:`TRIPS` 1 B trips,
    so both sides are warm medians on one flow.

    Informational: any RTT at all passes. No clock on hop 0 (bash < 5) is
    ``unmeasured (no-clock)`` and never touches a payload verdict; a clock
    with no 1 B trip back to time is ``skipped``.
    """
    if not tunnel_rtts_ms:
        if not _has_clock(hop0):
            return FeatureResult(
                RTT_ROW,
                Verdict.UNMEASURED,
                reason=UnmeasuredReason.NO_CLOCK,
                detail=(
                    f"bash on {hop0.host_id} has no $EPOCHREALTIME (bash < 5); "
                    "the payload verdicts stand"
                ),
            )
        return FeatureResult(RTT_ROW, Verdict.SKIPPED, hint="fwd 1 B did not echo")
    through = statistics.median(tunnel_rtts_ms)
    if segment_rtts_ms and all(rtt is not None for rtt in segment_rtts_ms):
        total = sum(rtt for rtt in segment_rtts_ms if rtt is not None)
        segments = f"segments sum {total:.1f} ms"
    else:
        segments = "segments sum not measured (a hop has no clock)"
    return FeatureResult(
        RTT_ROW, Verdict.PASS, measured=f"through tunnel {through:.1f} ms; {segments}"
    )


# --------------------------------------------------------------------------
# The one line saying what was proven
# --------------------------------------------------------------------------


def _payload_said(columns: dict[str, list[FeatureResult]], *, swept: bool) -> str:
    """Say ``payload-verified``, or why the payload rows (by exact name) do not.

    A run whose tunnel or echoes were gone before its teardown (another
    check's sweep took them, or the echoes exited) proves nothing, whatever
    its payload rows say: that is said first, before any other reason.
    """
    if swept:
        return _SWEPT_SAID
    payload = []
    for protocol, results in columns.items():
        names = [*payload_rows("fwd", protocol), *payload_rows("rev", protocol)]
        payload += [r for r in results if r.feature in names]
    if payload and all(r.verdict is Verdict.PASS for r in payload):
        return _PAYLOAD_VERIFIED
    unmeasured = next((r for r in payload if r.verdict is Verdict.UNMEASURED), None)
    if unmeasured is not None:
        return f"not measured — {unmeasured.detail}"
    return "not proven — see the failing rows"


def _last_segment_said(columns: dict[str, list[FeatureResult]], dest: str) -> str:
    """Say what the split proof's last leg showed, per requested protocol."""
    name = last_segment_row_name(dest)
    said = []
    for protocol, results in columns.items():
        if protocol != "tcp":
            said.append(f"{protocol.upper()} not measured")
            continue
        verdict = next((r.verdict for r in results if r.feature == name), None)
        if verdict is Verdict.PASS:
            said.append("TCP handshake only")
        elif verdict is Verdict.FAIL:
            said.append("TCP handshake failed")
        else:
            said.append("TCP not measured")
    return "; ".join(said)


def proven_line(
    columns: dict[str, list[FeatureResult]],
    *,
    dest: str | None,
    full: bool,
    swept: bool = False,
) -> str:
    """Say what was proven: *columns* maps each requested protocol to its rows, in order.

    With no *dest*, it covers the hop chain. A *full* proof covers the whole
    path to *dest*. A split proof covers the hop chain, then says what the
    last leg to *dest* showed, and no more. *swept* says what some column
    built was gone before its teardown, so nothing it measured is proof.
    """
    said = _payload_said(columns, swept=swept)
    protocols = ", ".join(columns)
    if dest is None:
        if said == _PAYLOAD_VERIFIED:
            return f"hop chain: payload-verified ({protocols})"
        return f"hop chain: {said}"
    if full:
        if said == _PAYLOAD_VERIFIED:
            return f"whole path payload-verified to {dest} ({protocols})"
        return f"whole path to {dest}: {said}"
    return f"hop chain: {said}; last segment to {dest}: {_last_segment_said(columns, dest)}"
