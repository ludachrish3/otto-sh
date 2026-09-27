"""``otto tunnel check``: prove a tunnel path end to end before building the real one.

The check takes the same arguments as ``otto tunnel add`` (spec 2026-09-24
§5) and never touches the user's service port beyond looking at it. It runs
in this order:

1. **Validate** the path with the same pure refusals ``add_tunnel`` makes; a
   refusal is the result, with no host contacted.
2. **Fingerprint** every hop, and label it against the proven range.
3. **Sweep** each hop for leftovers of an earlier, killed run: its tagged
   echoes and the throwaway tunnels they name. Only a leftover older than
   :data:`~otto.check.sweep.SWEEP_MIN_AGE_S` is removed; a younger one may be a
   check running right now, and is left with a line saying so. Nothing else
   is touched.
4. **Per protocol, one after the other:** check that the service port is free
   on both endpoints; prove each hop pair with a segment echo; start the two
   delivery echoes, then build a throwaway tunnel on a scratch port with the
   real :func:`~otto.tunnel.manage.add_tunnel`; push payloads through it,
   forward then reverse; confirm ``tunnel list`` sees it ``ok``. The tunnel
   and every echo are then removed in a ``finally``, and every hop is
   re-scanned to prove nothing tagged survived.

A ``--dest`` is proven as far as it lets itself be. One that runs socat and
bash is treated like one more hop: fingerprinted, swept, given the fwd echo at
its own address and torn down, so the whole path is payload-verified. One that
runs nothing of otto's (the lab says ``has_bash=False``, or its fingerprint
shows no socat or bash) gets the split proof of spec §5.3: the hop chain is
payload-verified to an echo on the last hop, and the last leg is only a
data-less TCP handshake from the last hop to ``--port`` on the dest. Closing
that gap is issue #440.

Nothing overlaps within one run. socat's UDP ``fork`` listeners can
cross-deliver the first datagrams of two new flows that arrive together
(issue #471), so no two probes of the same run ever go at once. The per-row
probes live in ``otto.tunnel._tunnel_rows``, and the tagged echoes they
aim at in ``otto.tunnel._tunnel_echoes``.

Two *separate* runs sharing hosts are safe too (issue #442): each sweeps
only leftovers old enough that a running check could not still own them, and
each draws its own scratch port at random from the free-port budget
(:func:`~otto.tunnel.socat.pick_random_free_port`) rather than always the
lowest one, so their echoes land on different ports. A collision is still
possible, just rare — see :data:`TunnelCheckReport.scratch_port`.
"""

import random
import secrets
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..check import (
    CheckHostUnreachableError,
    CheckSection,
    FeatureResult,
    HostFingerprint,
    ReportVerdicts,
    Verdict,
    range_labels,
)
from ..check.fingerprint import TUNNEL_TOOLS, TUNNEL_VERSIONS, probe_fingerprint
from ..check.sweep import left_line, run_age, sweepable, swept_note
from ..host.errors import HostUnreachableError
from ..host.host import is_dry_run
from . import _tunnel_echoes as tagged
from . import _tunnel_plan as plan
from . import _tunnel_render as render
from . import _tunnel_rows as rows
from ._tunnel_rows import (
    BUILD_ROW,
    FWD_ECHO,
    LIST_ROW,
    LOOPBACK,
    REV_ECHO,
    RTT_ROW,
    SERVICE_PORT_ROW,
    TEARDOWN_ROW,
)
from .carrier import DEFAULT_CARRIER
from .check_probes import EchoTag
from .discovery import discover_tunnels
from .manage import (
    EndpointSpec,
    RemovedReport,
    ResolvedHop,
    add_tunnel,
    probe_port_budget,
    remove_tunnel,
    resolve_chain,
    resolve_endpoint,
)
from .model import make_tunnel_id
from .socat import pick_random_free_port

if TYPE_CHECKING:
    from ..config.lab import Lab

PROTOCOLS = ["tcp", "udp"]
"""Every protocol the check proves, in the order ``--protocol both`` runs them."""

_scratch_rng = random.SystemRandom()
"""Draws this run's scratch port (see :func:`pick_random_free_port`).

``SystemRandom`` draws from the OS's own entropy on every call, so two
runs racing in the same event loop (``asyncio.gather``) or two separate
otto processes never share state that could correlate their draws, the way
two ``random.Random()`` instances seeded at nearly the same instant might.

A module-level seam, not a parameter of :func:`check_tunnel`: nothing else
here needs to know a random generator exists, and a test pins the draw by
monkeypatching this attribute directly."""

REFUSAL_HINT = (
    "the check refuses exactly what `otto tunnel add` would; see `otto tunnel add --help` "
    "and docs/cli/tunnel/add"
)

FULL_PROOF = "full"
""":attr:`TunnelCheckReport.dest_proof` for a dest that runs socat and bash."""
SPLIT_PROOF = "split"
""":attr:`TunnelCheckReport.dest_proof` for a dest that runs nothing of otto's (spec §5.3)."""


@dataclass(frozen=True)
class TunnelCheckHop:
    """One hop of the checked path."""

    host_id: str
    address: str
    """The resolved address the tunnel uses on this hop."""
    fingerprint: HostFingerprint | None
    range_labels: dict[str, str]
    """Component (kernel, isa, userland, socat, bash, launcher) -> its
    :class:`~otto.check.RangeLabel` value."""
    swept: list[str] = field(default_factory=list)
    """What the start-of-run sweep did on this hop, one display line each.

    It names what it removed, and what it left because it was too young to be
    a dead run's."""


@dataclass(frozen=True)
class TunnelCheckColumn:
    """One protocol's results."""

    protocol: str
    results: list[FeatureResult]
    """One per row, in :func:`row_order`."""
    concurrently_swept: bool = False
    """True when this column's tunnel or echoes were gone before its teardown
    (another check's sweep took them, or the echoes exited), so none of its
    rows is evidence."""


@dataclass
class TunnelCheckReport(ReportVerdicts):
    """Everything one ``otto tunnel check`` found — what the CLI renders and ``--report`` writes."""

    path: list[str]
    """The path as given, one ``host`` or ``host@if`` per hop."""
    port: int
    """The service port the user intends to tunnel; looked at, never bound."""
    dest: str | None
    carrier: str
    protocols: list[str]
    scratch_port: int | None
    """The service port the throwaway tunnel was built on; ``None`` when none was picked.

    Drawn at random from the free ports above the budget's floor
    (:func:`~otto.tunnel.socat.pick_random_free_port`), not the lowest one,
    so two checks racing on the same hosts are unlikely to pick the same
    one. When they do, the later run's echo on that port exits at once, and
    the row that started it (a ``segment`` row or ``build``) fails with
    ``scratch port <port> on <host> is held by something else (another
    check?)``, and the teardown does not blame a sweep for an echo that
    never ran. The scan that tells is one sample: an echo caught in the
    moment before it exits passes it, the probes reach the other run's
    echo, and the teardown then finds this run's echo gone and says so."""
    hops: list[TunnelCheckHop]
    columns: list[TunnelCheckColumn]
    destination: TunnelCheckHop | None = None
    """The ``--dest`` device as resolved: its fingerprint when it was taken, and
    what the sweep removed from it (only a full proof sweeps it)."""
    dest_proof: str | None = None
    """:data:`FULL_PROOF` or :data:`SPLIT_PROOF` once a run decided it; ``None``
    with no ``--dest``, and before any device was contacted."""
    proven: str = ""
    """One line saying what was proven."""
    refusal: str | None = None
    refusal_hint: str | None = None
    dry_run_plan: list[str] = field(default_factory=list)

    @override
    def results(self) -> list[FeatureResult]:
        """Every result, column by column."""
        return [r for column in self.columns for r in column.results]


def requested_protocols(protocol: str) -> list[str]:
    """Expand ``--protocol`` (``tcp``, ``udp`` or ``both``) to the protocols to check.

    Public so a caller (the CLI) can reject a bad value as a usage error
    before anything that would count as this check's result.

    Raises:
        ValueError: *protocol* is none of ``tcp``, ``udp`` or ``both``.
    """
    wanted = protocol.lower()
    if wanted == "both":
        return list(PROTOCOLS)
    if wanted in PROTOCOLS:
        return [wanted]
    raise ValueError(f"unknown protocol {protocol!r}; valid: tcp, udp, both")


def row_order(protocol: str, host_ids: list[str], *, split_dest: str | None = None) -> list[str]:
    """Every row of *protocol*'s column over *host_ids*, in report order.

    *host_ids* are the hosts the payloads are proven across: the path, plus a
    ``--dest`` that gets the full proof (its leg is one more segment). A dest
    that gets the split proof is *split_dest* instead, and its
    ``last segment`` row comes last.
    """
    segments = [rows.segment_row_name(a, b) for a, b in pairwise(host_ids)]
    order = [
        SERVICE_PORT_ROW,
        *segments,
        BUILD_ROW,
        *rows.payload_rows("fwd", protocol),
        *rows.payload_rows("rev", protocol),
        RTT_ROW,
        LIST_ROW,
        TEARDOWN_ROW,
    ]
    if split_dest is not None:
        order.append(rows.last_segment_row_name(split_dest))
    return order


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Dest:
    """The ``--dest`` as resolved, and which proof it gets."""

    spec: EndpointSpec
    hop: ResolvedHop
    fingerprint: HostFingerprint | None
    """``None`` when the lab says it has no shell: it is never contacted."""
    full: bool
    """It runs socat and bash, so the whole path to it is payload-verified."""

    @property
    def split_name(self) -> str | None:
        """The host id a split proof's ``last segment`` row names; ``None`` for a full proof."""
        return None if self.full else self.hop.hop.host


async def _decide_dest(spec: EndpointSpec, hop: ResolvedHop) -> _Dest:
    """Fingerprint a dest that may have a shell, and decide which proof it gets.

    A dest the lab says has no shell (``has_bash=False``, an embedded target)
    is never contacted at all.
    """
    if plan.runs_no_shell(hop.host):
        return _Dest(spec, hop, None, full=False)
    fp = await probe_fingerprint(hop.host, tools=TUNNEL_TOOLS, versions=TUNNEL_VERSIONS)
    return _Dest(spec, hop, fp, full=bool(fp.tools.get("socat") and fp.tools.get("bash")))


def _echo_hops(resolved: list[ResolvedHop], dest: _Dest | None) -> list[ResolvedHop]:
    """Every host the check runs echoes on, in path order: the path, plus a full-proof dest."""
    return [*resolved, dest.hop] if dest is not None and dest.full else list(resolved)


@dataclass
class _Run:
    """One check's shared state: the resolved path and this run's token."""

    lab: Any
    specs: list[EndpointSpec]
    resolved: list[ResolvedHop]
    fingerprints: list[HostFingerprint]
    token: str
    port: int
    scratch: int
    carrier: str
    dest: _Dest | None = None

    @property
    def first(self) -> ResolvedHop:
        return self.resolved[0]

    @property
    def last(self) -> ResolvedHop:
        return self.resolved[-1]

    @property
    def echo_hops(self) -> list[ResolvedHop]:
        """The hosts proven by segment and payload, swept and torn down."""
        return _echo_hops(self.resolved, self.dest)

    @property
    def full_dest(self) -> _Dest | None:
        """The dest when it gets the full proof, else ``None``."""
        return self.dest if self.dest is not None and self.dest.full else None


def _removal_note(tunnel_id: str, removed: RemovedReport, lost: list[str]) -> str:
    """Say how removing one throwaway tunnel went, from ``remove_tunnel``'s own report.

    *lost* are the hosts on the checked path the removal could not reach; a
    lab host off the path that did not answer is not this path's concern.
    """
    notes = [
        f"removed tunnel {tunnel_id}"
        if tunnel_id in removed.removed_ids
        else f"its tunnel {tunnel_id} was already gone"
    ]
    if removed.survivors:
        pids = ", ".join(f"pid {pid} on {host}" for host, pid in removed.survivors)
        notes.append(f"tunnel processes survived: {pids}")
    if lost:
        notes.append(f"could not reach {', '.join(lost)}")
    return "; ".join(notes)


@dataclass(frozen=True)
class _LeftoverTunnels:
    """What the sweep did with the throwaway tunnels its echoes name."""

    notes: dict[str, str] = field(default_factory=dict)
    """Tunnel id -> how its removal went, or why it was left."""
    kept: set[str] = field(default_factory=set)
    """Tunnels whose removal did not verify: their echoes stay running, so a later
    sweep finds them again."""


async def _remove_leftover_tunnels(
    lab: Any, echoes: list[tagged.TaggedEcho], path: set[str]
) -> _LeftoverTunnels:
    """Remove the throwaway tunnels the sweep may take; say how each went, and which to keep.

    A tunnel is removed only when every echo naming it is old enough to sweep
    (:func:`~otto.check.sweep.sweepable`): one younger echo naming it means
    its run may still be using it.
    """
    young = {e.tag.tunnel_id for e in echoes if not sweepable(e.age_s)}
    named = [e.tag.tunnel_id for e in echoes if sweepable(e.age_s) and e.tag.tunnel_id != "-"]
    done = _LeftoverTunnels()
    for tunnel_id in dict.fromkeys(named):
        if tunnel_id in young:
            done.notes[tunnel_id] = f"its tunnel {tunnel_id} was left: a younger echo names it"
            continue
        removed = await remove_tunnel(lab, tunnel_id)
        lost = [host for host in removed.unreachable if host in path]
        done.notes[tunnel_id] = _removal_note(tunnel_id, removed, lost)
        if removed.survivors or lost:
            done.kept.add(tunnel_id)
    return done


def _sweep_line(
    host: str,
    echo: tagged.TaggedEcho,
    tunnels: _LeftoverTunnels,
    *,
    survived: set[int] | None,
    killed: bool,
) -> str:
    """Say what the sweep did with one echo it found on *host*.

    *survived* are the killed pids the scan after the ``kill`` still found,
    or ``None`` when that scan could not list processes. A *killed* echo
    still running is most likely another user's, which this login may not
    signal: it is never called swept, and neither is one no scan could show
    gone.
    """
    what = f"otto-check echo {echo.tag.role}"
    if echo.age_s is not None and not sweepable(echo.age_s):
        return left_line(what, host, echo.age_s)
    if killed and survived is None:
        line = f"could not confirm {what} pid {echo.pid} on {host} went: ps on {host} lists nothing"
    elif killed and survived is not None and echo.pid in survived:
        line = (
            f"could not remove {what} pid {echo.pid} on {host} "
            "(kill refused — started by another user?)"
        )
    else:
        verb = "kept" if echo.tag.tunnel_id in tunnels.kept else "swept"
        line = f"{verb} {what} on {host} ({swept_note(echo.age_s)})"
    if echo.tag.tunnel_id in tunnels.notes:
        line += f"; {tunnels.notes[echo.tag.tunnel_id]}"
    return line


def _aged_by_run(found: dict[str, list[tagged.TaggedEcho]]) -> dict[str, list[tagged.TaggedEcho]]:
    """Give every echo in *found* its run's age: the oldest of that run's echoes on any host.

    A run token is one check run, whichever hosts its echoes are on, so the
    sweep takes or leaves a run whole (:func:`~otto.check.sweep.run_age`).
    """
    every = [e for echoes in found.values() for e in echoes]
    runs = {e.tag.run for e in every}
    ages = {run: run_age([e.age_s for e in every if e.tag.run == run]) for run in runs}
    return {
        host: [replace(e, age_s=ages[e.tag.run]) for e in echoes] for host, echoes in found.items()
    }


async def _sweep(lab: Any, resolved: list[ResolvedHop]) -> dict[str, list[str]]:
    """Remove an earlier run's leftovers from every hop; say what went and what was left, per hop.

    Only ``otto-check:v1`` echoes are found, and only the throwaway tunnels
    those echoes name are removed, so a user's own tunnel is never touched.
    Echoes are judged by run: a run is as old as its oldest echo on any hop,
    and its echoes are taken only when that is older than
    :data:`~otto.check.sweep.SWEEP_MIN_AGE_S` (or ``ps`` gave no age for one of
    them). A hop whose ``ps`` cannot list processes at all is not swept, and
    says so. A younger run may be a check running on these hosts right now, so
    its echoes and the tunnels they name are left, each with a line saying so.

    Tunnels go first and echoes second, the order the teardown uses: a sweep
    killed halfway still leaves an echo naming any tunnel it had not removed.
    For the same reason, the echoes of a tunnel whose removal did not verify
    clean are left running, so a later sweep tries it again. A hop where
    something was killed is scanned again, and an echo still running there
    (one this login may not signal) is named as such, never called swept.

    Removing a named tunnel scans the whole lab, the way ``otto tunnel remove``
    does. As in the teardown, only a swept host that did not answer leaves a
    removal unverified: an unrelated lab host that is down would otherwise
    pin the leftover, and re-try it, on every run for as long as it stays down.
    """
    scans = {r.hop.host: await tagged.scan(r.host) for r in resolved}
    found = _aged_by_run({host: scanned.echoes for host, scanned in scans.items()})
    every = [e for echoes in found.values() for e in echoes]
    tunnels = await _remove_leftover_tunnels(lab, every, set(found))
    said: dict[str, list[str]] = {}
    for r in resolved:
        if not scans[r.hop.host].listed:
            said[r.hop.host] = [tagged.could_not_list(r.hop.host)]
            continue
        echoes = found[r.hop.host]
        old = [e for e in echoes if sweepable(e.age_s) and e.tag.tunnel_id not in tunnels.kept]
        survived = await tagged.kill_confirmed(r.host, [e.pid for e in old])
        said[r.hop.host] = [
            _sweep_line(r.hop.host, e, tunnels, survived=survived, killed=e in old) for e in echoes
        ]
    return said


@dataclass
class _ColumnState:
    """What one column's probing got as far as, for its teardown."""

    add_attempted: bool = False
    added: bool = False
    """``add_tunnel`` returned: the throwaway tunnel exists until something removes it."""
    echoes: list[EchoTag] = field(default_factory=list)
    """The delivery echoes this column started and saw running at their address.

    Only these can be said to have vanished at teardown: an echo no scan ever
    saw running was never known to be there."""


async def _build(run: _Run, protocol: str, tunnel_id: str, state: _ColumnState) -> FeatureResult:
    """Start both delivery echoes, then build the throwaway tunnel with the real ``add_tunnel``.

    The echoes start first and carry the tunnel's id, so a run killed at any
    point after this leaves an echo the next run's sweep finds, naming the
    tunnel to remove.

    ``add_tunnel`` refusing the build (a missing tool, a port taken, a
    process that did not come up) is the row's ``fail``. A hop that stops
    answering is not a verdict: it raises, host-named, once the teardown ran.
    """
    full = run.full_dest
    # The egress delivers to 127.0.0.1 on the last hop, or to a full-proof dest's own address.
    fwd = (full.hop, full.hop.ip) if full is not None else (run.last, LOOPBACK)
    for role, hop, bind_ip in [(FWD_ECHO, *fwd), (REV_ECHO, run.first, LOOPBACK)]:
        tag = EchoTag(run.token, tunnel_id, protocol, role, hop.hop.host)
        start = await tagged.start_echo(hop.host, tag, bind_ip, run.scratch)
        if start.problem is not None:
            return FeatureResult(
                BUILD_ROW,
                Verdict.FAIL,
                detail=start.problem,
                commands=start.commands,
                output=start.output,
            )
        if start.confirmed:
            state.echoes.append(tag)
    state.add_attempted = True
    try:
        added = await add_tunnel(
            run.lab,
            run.specs,
            port=run.scratch,
            protocol=protocol,
            dest=full.spec if full is not None else None,
            carrier=run.carrier,
        )
    except (OSError, HostUnreachableError) as e:
        path = " → ".join(r.hop.host for r in run.resolved)
        raise CheckHostUnreachableError(
            f"a hop of {path} stopped answering while tunnel {tunnel_id} was built: {e}"
        ) from e
    except (ValueError, RuntimeError) as e:
        return FeatureResult(BUILD_ROW, Verdict.FAIL, detail=str(e), output=str(e))
    state.added = True
    return FeatureResult(
        BUILD_ROW,
        Verdict.PASS,
        detail=f"carriers {added.carrier_fwd}/{added.carrier_rev} · {added.tunnel.id}",
    )


async def _list_row(lab: Any, tunnel_id: str) -> FeatureResult:
    """``tunnel list`` must rebuild the throwaway tunnel from its tags, and call it ``ok``."""
    discovery = await discover_tunnels(lab)
    seen = [t for t in discovery.tunnels if t.tunnel.id == tunnel_id]
    status = seen[0].status if seen else "not listed"
    if status == "ok":
        return FeatureResult(LIST_ROW, Verdict.PASS, measured=status)
    return FeatureResult(
        LIST_ROW,
        Verdict.FAIL,
        measured=status,
        wanted="ok",
        detail=f"`tunnel list` shows {tunnel_id} as {status}",
        hint=(
            "a tunnel `tunnel list` cannot rebuild from its process tags is one "
            "`tunnel remove` cannot find either"
        ),
    )


@dataclass(frozen=True)
class _Teardown:
    row: FeatureResult
    unreachable: CheckHostUnreachableError | None = None
    swept: bool = False
    """What this column built was already gone before its teardown: another check's sweep
    took it, or its echoes exited."""


async def _teardown(run: _Run, tunnel_id: str, state: _ColumnState) -> _Teardown:
    """Remove the tunnel, kill this run's echoes, and re-scan every hop for survivors.

    Best effort on every hop: a hop that does not answer is named, the others
    are still cleaned, and the first such error is handed back for the caller
    to raise once nothing else is in flight.

    When the tunnel's removal does not verify clean, the echoes carrying its
    id are left running on purpose: they are how the next check's sweep finds
    the leaked tunnel and removes it.

    A hop whose ``ps`` cannot list processes fails the row, saying so: this
    run's echoes there can be neither confirmed gone nor said to have
    vanished.

    A tunnel that was built but is no longer there to remove was removed by
    something else mid-run: most likely another check's sweep on these hosts,
    which takes any run's echoes once they are older than
    :data:`~otto.check.sweep.SWEEP_MIN_AGE_S`, so a run that outlives that
    bound can lose them. A delivery echo that was seen running but is gone,
    while its tunnel was still there, either exited or was taken by such a
    sweep. Either way the row says so first, naming exactly what vanished
    (:func:`~otto.tunnel._tunnel_rows.swept_detail`), since the payload rows
    measured a tunnel that may not have been this run's.
    """
    problems: list[str] = []
    unreachable: CheckHostUnreachableError | None = None
    leaked = False
    tunnel_gone = False
    echoes_gone: list[EchoTag] = []
    if state.add_attempted:
        path = {r.hop.host for r in run.resolved}
        try:
            removed = await remove_tunnel(run.lab, tunnel_id)
        except (OSError, ValueError, RuntimeError) as e:
            problems.append(f"removing tunnel {tunnel_id} failed: {e}")
            leaked = True
        else:
            lost = [host for host in removed.unreachable if host in path]
            problems += [
                f"tunnel process pid {pid} survived on {host}" for host, pid in removed.survivors
            ]
            problems += [f"could not reach {host} to remove tunnel {tunnel_id}" for host in lost]
            leaked = bool(removed.survivors or lost)
            tunnel_gone = state.added and tunnel_id not in removed.removed_ids
        if leaked:
            problems.append(
                f"tunnel {tunnel_id} was left for the next check's sweep: its echoes still "
                "run and name it"
            )
    keep = tunnel_id if leaked else None
    for r in run.echo_hops:
        cleaned = await tagged.clean_hop(r, run.token, keep_tunnel=keep)
        problems += cleaned.problems
        unreachable = unreachable or cleaned.unreachable
        # A hop that did not answer, or whose ps lists nothing, shows nothing gone.
        if cleaned.unreachable is None and cleaned.listed:
            started = [t for t in state.echoes if t.host_id == r.hop.host]
            echoes_gone += [tag for tag in started if tag not in cleaned.seen]
    swept = tunnel_gone or bool(echoes_gone)
    if swept:
        problems.insert(
            0, rows.swept_detail(tunnel_id, tunnel_gone=tunnel_gone, echoes_gone=echoes_gone)
        )
    if problems:
        row = FeatureResult(TEARDOWN_ROW, Verdict.FAIL, detail="; ".join(problems))
    else:
        row = FeatureResult(TEARDOWN_ROW, Verdict.PASS, measured="nothing tagged survived")
    return _Teardown(row, unreachable, swept)


async def _probe(
    run: _Run, protocol: str, tunnel_id: str, got: dict[str, FeatureResult], state: _ColumnState
) -> str | None:
    """Run the segment, build and payload rows into *got*; return why the rest was skipped."""
    segment_rtts: list[float | None] = []
    for a, b in pairwise(run.echo_hops):
        segment = await rows.segment_row(a, b, protocol, run.scratch, run.token)
        got[segment.row.feature] = segment.row
        if segment.row.verdict is not Verdict.PASS:
            return f"{segment.row.feature} failed, so no tunnel was built across it"
        segment_rtts.append(segment.rtt_ms)
    got[BUILD_ROW] = await _build(run, protocol, tunnel_id, state)
    if got[BUILD_ROW].verdict is not Verdict.PASS:
        return "the throwaway tunnel could not be built"
    # FWD enters at hop 0 and REV at the last hop — strictly one after the other.
    fwd = await rows.payloads(run.first, "fwd", protocol, run.scratch)
    rev = await rows.payloads(run.last, "rev", protocol, run.scratch)
    for result in [*fwd.rows, *rev.rows]:
        got[result.feature] = result
    got[RTT_ROW] = rows.rtt_row(fwd.one_byte_rtts_ms, segment_rtts, run.fingerprints[0])
    got[LIST_ROW] = await _list_row(run.lab, tunnel_id)
    return None


async def _column(run: _Run, protocol: str) -> TunnelCheckColumn:
    """One protocol's whole column; the teardown runs whatever happened before it."""
    # Imported here, not at module scope, as otto.tunnel.manage does: otto.lifecycle
    # is only needed once a teardown runs, and the tunnel CLI imports this module.
    from ..lifecycle import compensate

    split_dest = run.dest.split_name if run.dest is not None else None
    names = row_order(protocol, [r.hop.host for r in run.echo_hops], split_dest=split_dest)
    # The real tunnel on --port listens only where add_tunnel's two ingresses bind it:
    # the first and the last hop. A --dest is only connected to, so a listener on its
    # --port is the user's service, never a conflict.
    got = {
        SERVICE_PORT_ROW: await rows.service_port_row(
            [run.first.host, run.last.host], protocol, run.port
        )
    }
    tunnel_id = make_tunnel_id(tuple(r.hop for r in run.resolved), protocol, run.scratch)
    skipped: str | None = None
    state = _ColumnState()
    try:
        skipped = await _probe(run, protocol, tunnel_id, got, state)
    finally:
        teardown = await compensate(
            _teardown(run, tunnel_id, state),
            what=f"tunnel check {protocol} teardown",
        )
    if teardown.unreachable is not None:
        raise teardown.unreachable
    got[TEARDOWN_ROW] = teardown.row
    if run.dest is not None and split_dest is not None:
        # Independent of the tunnel, so it runs whatever the hop chain showed.
        got[names[-1]] = await rows.last_segment_row(
            run.last, run.fingerprints[-1], run.dest.hop, protocol, run.port
        )
    results = [
        got.get(name) or FeatureResult(name, Verdict.SKIPPED, hint=skipped) for name in names
    ]
    return TunnelCheckColumn(protocol, results, concurrently_swept=teardown.swept)


def _proven(columns: list[TunnelCheckColumn], dest: _Dest | None) -> str:
    """Say what was proven, across every requested protocol, as far as the dest lets it go."""
    return rows.proven_line(
        {c.protocol: c.results for c in columns},
        dest=dest.hop.hop.host if dest is not None else None,
        full=dest is not None and dest.full,
        swept=any(c.concurrently_swept for c in columns),
    )


async def check_tunnel(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "both",
    dest: EndpointSpec | None = None,
    carrier: str = DEFAULT_CARRIER,
) -> TunnelCheckReport:
    """Check that a tunnel over *hosts* would work, by building a throwaway one and using it.

    The arguments mirror :func:`~otto.tunnel.manage.add_tunnel`, so a path
    that checks clean can be built for real with the same ones. *port* is
    the service port the user intends: the check only looks at whether it is
    free on both endpoints, and builds on a scratch port instead. A path
    ``add_tunnel`` would refuse is reported as the result, and a dry run
    returns the plan without contacting any host.

    A command that fails on a host that answered is a row's verdict, never an
    exception.

    Raises:
        ValueError: *protocol* is none of ``tcp``, ``udp`` or ``both``.
        ~otto.check.CheckHostUnreachableError: a hop the check needed did not
            answer. A down host is never reported as a skip.
        ~otto.tunnel.socat.NoFreePortError: no scratch port is free above
            the hops' budget floor. It is a ``RuntimeError``, and ``otto
            tunnel check`` exits 1 with its message.
    """
    protocols = requested_protocols(protocol)
    given = [plan.spec_text(spec) for spec in hosts]

    def report(
        *,
        scratch: int | None = None,
        hops: list[TunnelCheckHop] | None = None,
        columns: list[TunnelCheckColumn] | None = None,
        **kw: Any,
    ) -> TunnelCheckReport:
        return TunnelCheckReport(
            given,
            port,
            plan.spec_text(dest) if dest is not None else None,
            carrier,
            protocols,
            scratch,
            hops or [],
            columns or [],
            **kw,
        )

    try:
        planned = plan.validate(lab, hosts, protocols, dest, carrier)
    except ValueError as e:
        return report(refusal=str(e), refusal_hint=REFUSAL_HINT)
    if is_dry_run():
        lines = plan.dry_run_plan(planned, protocols, port=port, dest=dest, carrier=carrier)
        return report(dry_run_plan=lines)
    try:
        resolved = await resolve_chain(lab, hosts)
        dest_hop = await resolve_endpoint(lab, dest) if dest is not None else None
    except ValueError as e:
        return report(refusal=str(e), refusal_hint=REFUSAL_HINT)
    except (OSError, HostUnreachableError) as e:
        raise CheckHostUnreachableError(f"resolving {' → '.join(given)}: {e}") from e
    fingerprints = [
        await probe_fingerprint(r.host, tools=TUNNEL_TOOLS, versions=TUNNEL_VERSIONS)
        for r in resolved
    ]
    the_dest = (
        await _decide_dest(dest, dest_hop) if dest is not None and dest_hop is not None else None
    )
    echo_hops = _echo_hops(resolved, the_dest)
    swept = await _sweep(lab, echo_hops)

    def hop_report(r: ResolvedHop, fp: HostFingerprint | None, lines: list[str]) -> TunnelCheckHop:
        labels = range_labels(fp, render.RANGE_COMPONENTS) if fp is not None else {}
        return TunnelCheckHop(r.hop.host, r.ip, fp, labels, lines)

    hops = [
        hop_report(r, fp, swept[r.hop.host]) for r, fp in zip(resolved, fingerprints, strict=True)
    ]
    dest_kw: dict[str, Any] = {}
    if the_dest is not None:
        # Only a full-proof dest is swept; a split one has no sweep lines.
        dest_swept = swept.get(the_dest.hop.hop.host, [])
        dest_kw = {
            "destination": hop_report(the_dest.hop, the_dest.fingerprint, dest_swept),
            "dest_proof": FULL_PROOF if the_dest.full else SPLIT_PROOF,
        }
    missing = rows.missing_tools(fingerprints)
    if missing is not None:
        ends = [resolved[0].host, resolved[-1].host]
        split_dest = the_dest.split_name if the_dest is not None else None
        columns = []
        for p in protocols:
            names = row_order(p, [r.hop.host for r in echo_hops], split_dest=split_dest)
            results = [await rows.service_port_row(ends, p, port)]
            if the_dest is not None and split_dest is not None:
                # The last leg does not use the tunnel: it is decided on its own.
                results += rows.unmeasured_missing(names[1:-1], missing)
                results.append(
                    await rows.last_segment_row(
                        resolved[-1], fingerprints[-1], the_dest.hop, p, port
                    )
                )
            else:
                results += rows.unmeasured_missing(names[1:], missing)
            columns.append(TunnelCheckColumn(p, results))
        proven = _proven(columns, the_dest)
        return report(hops=hops, columns=columns, proven=proven, **dest_kw)
    try:
        budget = await probe_port_budget(echo_hops)
    except (OSError, HostUnreachableError) as e:
        raise CheckHostUnreachableError(f"a hop did not answer the free-port probe: {e}") from e
    run = _Run(
        lab,
        hosts,
        resolved,
        fingerprints,
        token=secrets.token_hex(3),
        port=port,
        scratch=pick_random_free_port(budget.used | {port}, _scratch_rng, lo=budget.floor),
        carrier=carrier,
        dest=the_dest,
    )
    columns = [await _column(run, p) for p in protocols]
    proven = _proven(columns, the_dest)
    return report(scratch=run.scratch, hops=hops, columns=columns, proven=proven, **dest_kw)


def tunnel_sections(report: TunnelCheckReport) -> list[CheckSection]:
    """Build a completed *report*'s one :class:`~otto.check.CheckSection`.

    Mirrors :func:`otto.link.check.link_sections`: ready for
    :func:`~otto.check.render_sections`, and for ``--report`` alongside
    :func:`~otto.check.report_to_json`. A refusal or a dry-run plan is never
    passed here — the CLI layer renders those, the same way ``otto link
    check``'s does.
    """
    return render.tunnel_sections(report)
