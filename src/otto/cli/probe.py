"""``--dry-run --probe``: open a connection to each named host, and never a command.

Spec: ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` §3.

**The flag permits a CONNECTION, never a COMMAND.** That single sentence is why
this module is allowed to exist inside a contract whose whole point is that a
dry run contacts no device: a connection attempt produces no command result, so
it feeds no ``if result.is_ok:`` and no parser — the fabrication hazard the rest
of the workstream closed is untouched. Reachability is *information*, printed
and never acted on: an unreachable host does not fail the dry run.

The dialing primitive is :meth:`~otto.host.host.BaseHost.is_reachable`, and that
choice is load-bearing rather than convenient. Re-verified for this task across
every family (with the transport primitives faked and every command entrypoint
spied):

- ``UnixHost`` ``term=ssh`` — one ``ssh_connect``, zero commands.
- ``UnixHost`` ``term=telnet`` — one telnet ``connect()`` (which writes the
  login credentials), zero commands.
- ``UnixHost`` ``transfer=ftp`` — the term channel **plus** the FTP control
  channel (``connect`` + ``login``): three opens for one probe, still zero
  commands.
- ``EmbeddedHost``/``ZephyrHost`` — one telnet console open, zero commands.
- ``LocalHost`` — answers ``True`` with no transport at all.
- ``DockerContainerHost`` — has no override, so ``BaseHost.is_reachable``
  raises ``NotImplementedError``. That is reported as ``not probed``, NOT as
  ``unreachable``: "we could not ask" and "we asked and it said no" are
  different facts and only one of them is about the host.

So "one host probed" is not "one socket opened" — it is one *reachability
question* asked over the transports that host's configuration names.

**Opening includes authenticating, and that is the right line.** Telnet and FTP
put credentials on the wire during the open. Narrowing the probe to a bare TCP
connect (or to the term channel alone) would look purer and would manufacture
this contract's own defect in miniature: a host that accepts TCP and refuses the
login is not usable, and reporting it ``reachable`` is a fact about a question
the operator did not ask. The probe verifies the real run's whole connect phase.
"""

import dataclasses
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .invoke import LabReference

PROBE_TIMEOUT_SECONDS = 10.0
"""Per-host bound on one reachability probe, in seconds.

Matches :meth:`~otto.host.host.BaseHost.is_reachable`'s own default so a probe
under ``--probe`` waits exactly as long as otto's live reachability path does
— a host that a real run would call up is not called down here by a tighter
budget than the real run uses.
"""

REACHABLE = "reachable"
"""A connection opened."""

UNREACHABLE = "unreachable"
"""A connection was attempted and did not open (refused, timed out, refused
authentication). The reason is not carried here: ``is_reachable`` collapses
every failure to a bool, and ``verify_connection`` has already logged the
underlying error, so inventing a reason at this layer would be the same
fabrication in a smaller font."""

NOT_PROBED = "not probed"
"""No reachability question could be asked at all (the family has no probe, or
the id no longer resolves). Deliberately not ``unreachable``."""

PROBE_HEADLINE = "probe: a connection only -- no command was run"
"""Printed above the table. Says what the flag did AND what it did not do,
because the second half is the whole promise."""

PROBE_NO_HOSTS = "probe: this command names no host to dial"
"""SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: a probe that found nothing to
dial says so. Printing nothing would be indistinguishable from a broken flag."""


NO_TRANSPORT = "no transport to open"
"""Why a :data:`REACHABLE` host was never dialed — ``LocalHost`` is the machine
otto is running on, so its reachability is answered without a socket."""


@dataclasses.dataclass(frozen=True)
class ProbeTarget:
    """One host to dial, with the protocol overrides the invocation chose.

    The overrides matter because a probe that ignores them answers a question
    nobody asked: ``otto host dut1 --term telnet … -n --probe`` would open SSH
    and report the host reachable over a transport the real run is not going to
    use. They ride on the reference (and so on the leaf's own resolver) rather
    than being re-derived here.
    """

    host_id: str
    term: "str | None" = None
    transfer: "str | None" = None


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """One host's answer to the reachability question, as the table prints it."""

    host_id: str
    """The lab id dialed."""

    state: str
    """:data:`REACHABLE`, :data:`UNREACHABLE` or :data:`NOT_PROBED`."""

    connect_ms: "float | None" = None
    """Milliseconds to open the connection, or ``None`` when there is nothing
    honest to report. This is connect-and-authenticate time, not a network
    round trip, which is why the table labels it ``connect`` — and it is
    recorded only for a host that was actually dialed AND answered: the time an
    unreachable host takes to fail is a property of the timeout, not of the
    host, and a host reached without a socket took no connect time at all."""

    detail: str = ""
    """Why no probe happened, for :data:`NOT_PROBED`; :data:`NO_TRANSPORT` for a
    host answered without one. Empty otherwise."""

    dialed: bool = False
    """Whether this probe actually attempted to OPEN a transport.

    The ONLY input to the block's device-contact claim (see
    :func:`probe_contacted`), and false in two distinct cases that both used to
    read as contact: a :data:`NOT_PROBED` host (nothing was attempted) and a
    family that answers reachability without a socket (``LocalHost``). Derived
    from the host object, not from the state, because "we said reachable" and
    "we opened something" are different facts.
    """


def probe_targets(references: "list[LabReference]") -> "list[ProbeTarget]":
    """Flatten the resolved references into the ordered, deduplicated dial list.

    Kind-agnostic on purpose: a host argument contributes its own id, a link
    contributes both endpoints, a tunnel contributes its chain — each leaf's
    ``__otto_dry_run_refs__`` resolver decides what its reference *names*, and
    this function never re-derives it. One authority, the leaf's own.

    Deduplicated by host id, first reference winning. Two references naming one
    host with conflicting protocol overrides cannot arise today (only
    ``otto host`` supplies overrides, and it names exactly one host); if a
    future leaf makes it possible, the first mention is what gets dialed.
    """
    targets: list[ProbeTarget] = []
    seen: set[str] = set()
    for ref in references:
        for host_id in ref.host_ids:
            if host_id in seen:
                continue
            seen.add(host_id)
            targets.append(ProbeTarget(host_id, term=ref.term, transfer=ref.transfer))
    return targets


async def _probe_one(target: ProbeTarget) -> ProbeResult:
    """Ask one host whether it is reachable, and never ask it anything else."""
    from ..config import get_host
    from ..host.remote_host import RemoteHost

    try:
        host: Any = get_host(target.host_id, term=target.term, transfer=target.transfer)
    except (KeyError, ValueError) as e:
        # KeyError: the id no longer resolves. ValueError: an override outside
        # the host's own menu. Neither can arise on today's `otto host` path
        # (the leaf's resolver validated both before the seam got here), but a
        # traceback out of a REPORTING layer would be a worse answer than a row
        # saying which question could not be asked.
        return ProbeResult(target.host_id, NOT_PROBED, detail=str(e))

    # Asked of the object, before anything is attempted: only a RemoteHost has a
    # transport to open (``_probe_connection`` is defined there). LocalHost and
    # DockerContainerHost are BaseHosts, and answering "reachable" for the
    # machine otto is running on is not device contact.
    dials = isinstance(host, RemoteHost)

    start = time.monotonic()
    try:
        reachable = await host.is_reachable(timeout=PROBE_TIMEOUT_SECONDS)
    except NotImplementedError as e:
        # The family has no connection probe (a docker container is reached
        # through its parent's shell, so there is no transport of its own to
        # open). Say that, rather than reporting the host down.
        return ProbeResult(target.host_id, NOT_PROBED, detail=str(e))
    if not reachable:
        return ProbeResult(target.host_id, UNREACHABLE, dialed=dials)
    if not dials:
        return ProbeResult(target.host_id, REACHABLE, detail=NO_TRANSPORT, dialed=False)
    return ProbeResult(
        target.host_id,
        REACHABLE,
        connect_ms=(time.monotonic() - start) * 1000.0,
        dialed=True,
    )


async def _probe_all(targets: "list[ProbeTarget]") -> "list[ProbeResult]":
    """Probe every host concurrently, in the order the references named them."""
    import asyncio

    return list(await asyncio.gather(*(_probe_one(target) for target in targets)))


def run_probe(references: "list[LabReference]") -> "list[ProbeResult]":
    """Dial the hosts *references* name; return one result per host.

    Runs under :func:`~otto.lifecycle.run_command` rather than a bare
    ``asyncio.run``, for cleanup rather than for policy: ``get_host`` registers
    every host it hands out with the active context's host scope, and that
    scope's exit sweep is what CLOSES the transports this function opened. A
    probe that left them open would strand them on a loop that is about to be
    torn down — and, on the preview path, hand the command body connections
    bound to a dead loop. An override copy (``--term``/``--transfer``) is
    registered by the same call, so it is swept too.
    """
    targets = probe_targets(references)
    if not targets:
        return []
    from ..lifecycle import run_command

    return run_command(_probe_all(targets))


def probe_contacted(results: "list[ProbeResult]") -> bool:
    """Whether the probe actually opened (or tried to open) a transport.

    The block's headline asserts something about device contact either way, so
    this is the one predicate allowed to decide it — and it counts DIALS, not
    results. A set that is entirely ``not probed``, or entirely local, means no
    socket was attempted and the default "no device was contacted" headline is
    the true one.
    """
    return any(result.dialed for result in results)


def probe_report_lines(results: "list[ProbeResult]") -> "list[str]":
    """Render the per-host rows. Pure, so the table is testable without a clock."""
    lines: list[str] = []
    for result in results:
        line = f"{result.host_id}: {result.state}"
        if result.state == REACHABLE and result.connect_ms is not None:
            line = f"{line} (connect {result.connect_ms:.0f} ms)"
        elif result.detail:
            line = f"{line} -- {result.detail}"
        lines.append(line)
    return lines


def print_probe_report(results: "list[ProbeResult]") -> None:
    """Print the probe's product: what was dialed, and what answered.

    Straight to the console like the dry-run block itself, never through a
    logger — the announcement is the deliverable and must not be foldable by a
    log level or a capture filter.
    """
    from rich import print as rprint
    from rich.markup import escape

    if not results:
        rprint(f"[magenta]{escape(PROBE_NO_HOSTS)}[/magenta]")
        return
    rprint(f"[magenta]{escape(PROBE_HEADLINE)}[/magenta]")
    for line in probe_report_lines(results):
        rprint(f"  {escape(line)}")
