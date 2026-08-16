"""Live tunnel discovery — the processes on the hosts ARE the record (spec §9).

``_scan_hosts`` gathers :data:`DISCOVERY_PS_COMMAND` across hosts (best-effort,
bounded, transparent about unreachables); ``parse_process_discovery`` decodes
each tagged process; and ``discover_tunnels`` groups observations by tunnel
id, comparing what was observed against what the sentinel-encoded path says
must exist.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..errors import OttoError
from ..host.daemon import parse_ps_output, ps_scan_command
from ..host.host import is_dry_run
from ..logger.mode import LogMode
from .model import ProcKey, Tunnel
from .sentinel import SENTINEL_PREFIX, ParsedSentinel, parse_sentinel

if TYPE_CHECKING:
    from ..config.lab import Lab

logger = logging.getLogger(__name__)

_TUNNEL_HOST_TIMEOUT = 30.0
"""Ceiling on any single-host ``exec`` on the discovery path (spec §6.4)."""

DISCOVERY_PS_COMMAND: str = ps_scan_command(SENTINEL_PREFIX)
"""The lab-wide daemon scan for tunnel processes. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story. The bytes are pinned by TestPsScanCommand in
tests/unit/host/test_daemon.py (STABILITY CONTRACT)."""


class TunnelNotMeasuredError(OttoError, RuntimeError):
    """A device was never asked — this run is a ``--dry-run``.

    NOT "unreachable" and NOT "the read failed": nothing was sent, so there is
    no host to blame and no tooling to go and fix. ``--dry-run`` is documented
    as "Preview without running commands"
    (``docs/guide/cli-reference.md``), so every answer this package gives
    under one has to come from lab data and the caller's own arguments.

    ``_device_read`` and ``_device_running`` raise this rather than
    letting the synthetic reply through, because ``BaseHost.exec`` ANSWERED a
    dry run with ``Status.Skipped`` — whose ``is_ok`` is ``True`` — carrying
    the literal value ``"[DRY RUN] Command not executed"``, and this package
    PARSES what it gets. That string has no bare ``ok`` line, so
    ``otto.tunnel.manage._require_tools`` accused a host of missing socat
    it never spoke to; it has no ``:<port>`` match, so
    :func:`~otto.tunnel.socat.parse_listening_ports` returned an empty set and
    the carrier ports were always 49152/49153; it parses to zero observations,
    so ``otto tunnel list`` reported a reachable, empty lab; and
    ``result.value.strip()`` made it a container's IP ADDRESS, which then flowed
    into a socat argv. A raise cannot be mistaken for a measurement; a clean
    read can.

    The primitive now returns a ``Status.NotRun`` decline whose ``value``
    raises (:exc:`~otto.result.CommandNotRunError`), so every one of those
    parses would break rather than lie. This backstop STAYS — it names the
    tunnel and the read, it fires before a device call is attempted at all,
    and belt-and-braces above the primitive is the design's stated position
    (dry-run contract spec, section 4).

    ``RuntimeError``, like :class:`~otto.tunnel.socat.NoFreePortError` and
    ``otto.tunnel.records.TunnelScanFailedError``, because the consumers
    bucket on that base: both CLI commands that can raise catch
    ``(ValueError, RuntimeError)``, and the monitor's collector treats any
    exception out of its tunnel source as "keep the last known set". A class
    outside that hierarchy would escape both.
    """


async def _device_read(host: Any, cmd: str) -> Any:
    """Run one bounded, quiet READ on *host* and hand back its ``CommandResult``.

    **THE DRY-RUN BACKSTOP LIVES HERE**, and this is the single funnel every
    parsed read in ``otto.tunnel`` goes through:
    :func:`~otto.tunnel.manage._container_ip`,
    :func:`~otto.tunnel.manage._require_tools`,
    :func:`~otto.tunnel.manage._probe_used_ports` and :func:`_scan_hosts` all
    arrive by it. A dry run reaching this line means some path above forgot to
    short-circuit, and the only safe answer is to fail loudly — see
    :class:`TunnelNotMeasuredError` for what each caller does with the banner
    otherwise. ``tests/unit/tunnel/test_discovery.py``'s
    ``TestEveryDeviceTouchInThePackageIsDeclared`` pins the roster of sites
    that may touch a device at all, across BOTH touch classes, so a direct
    literal read added later cannot be added SILENTLY. Not the same claim as
    "cannot be added": that scan is by name over this package's own source,
    and a touch reached through an alias or through a helper elsewhere is
    invisible to it.

    It is a backstop and NOT the answer. On its own it turns every command
    into an error, which is honest and is not a product: a dry run with no
    preview is useless rather than safe. It also cannot build one — a preview
    needs the caller's whole intent (the chain, the port, ``--dest``, the
    carrier), none of which is visible from a command string — so
    :func:`~otto.tunnel.manage.add_tunnel`, :func:`~otto.tunnel.manage.remove_tunnel`
    and :func:`~otto.tunnel.manage.remove_all_tunnels` short-circuit ABOVE
    this, where that intent lives, and never reach it.

    :func:`_scan_hosts` is the deliberate exception: it has no plan to build —
    reading IS its whole job — so it lets this fire and
    :func:`discover_tunnels` catches it, which is what gives ``otto tunnel
    list`` its "not measured" state and keeps this arm live rather than a
    tripwire nothing ever trips.

    The three MUTATING ``exec`` sites (the launch loop,
    :func:`~otto.tunnel.manage._kill_tunnel_on` and
    :func:`~otto.tunnel.manage._reap`'s kill) deliberately do not come through
    here: they are unreachable under a dry run because the three public entry
    points short-circuit above them, and routing them through a function whose
    name says READ would blur which of the two guarantees is holding. They do
    read their replies — ``is_ok``/``timed_out``, and ``result.value`` for the
    failure message — but never for DEVICE STATE, which is the parse a
    synthetic reply corrupts. Unreachability, not indifference, is what makes
    them safe.
    """
    if is_dry_run():
        raise TunnelNotMeasuredError(
            f"{cmd!r} was not run on host {getattr(host, 'id', host)!r}: a dry run makes no "
            f"device contact, so nothing about this host's state was measured"
        )
    return await host.exec(cmd, timeout=_TUNNEL_HOST_TIMEOUT, log=LogMode.QUIET)


async def _device_running(host: Any) -> bool:
    """Ask docker whether *host*'s container is up, bounded by the host timeout.

    THE SECOND DEVICE TOUCH, and the reason :func:`_device_read` is not the
    whole backstop: ``DockerContainerHost.is_running`` is a liveness probe, not
    a command-execution method, so it never passes through ``BaseHost.exec``
    and no ``CommandResult`` guard can see it. Under a dry run it is a REAL
    ``docker ps`` on the parent, and — worse — the synthetic reply satisfies
    ``_resolve_container_id``'s truthiness check, so the probe both answered
    "running" and CACHED ``"[DRY RUN] Command not executed"`` as the container
    id. Hence the same refusal here, from the same class.

    It has no ``timeout`` parameter of its own, which is why the bound is
    applied externally, here, once — the two call sites used to each wrap
    their own :func:`asyncio.wait_for`. ``asyncio.TimeoutError`` still
    propagates: both callers already answer it, and they answer it
    differently.
    """
    if is_dry_run():
        raise TunnelNotMeasuredError(
            f"host {getattr(host, 'id', host)!r} was not probed for liveness: a dry run "
            f"makes no device contact, so whether its container is running is unknown"
        )
    return await asyncio.wait_for(host.is_running(), _TUNNEL_HOST_TIMEOUT)


@dataclass(frozen=True, slots=True)
class Observation:
    """One tagged tunnel process seen on one host."""

    pid: int
    age_seconds: int
    parsed: ParsedSentinel


def parse_process_discovery(ps_output: str) -> list[Observation]:
    """Reconstruct observations from :data:`DISCOVERY_PS_COMMAND` output."""
    out: list[Observation] = []
    for proc in parse_ps_output(ps_output, SENTINEL_PREFIX):
        parsed = parse_sentinel(proc.token)
        if parsed is None:
            continue
        out.append(Observation(pid=proc.pid, age_seconds=proc.age_seconds, parsed=parsed))
    return out


async def _scan_hosts(hosts: list[Any]) -> tuple[list[tuple[str, Observation]], list[str]]:
    """Gather the discovery command over *hosts*; best-effort + transparent.

    Returns ``(observations_by_origin, unreachable_host_ids)``. Also the
    verify primitive for the manage layer, which scans just a chain's hosts.
    """

    # DEBT(no-tuple-return): observations plus an error string.
    # ast-grep-ignore: no-tuple-return
    async def scan(host: Any) -> tuple[list[tuple[str, Observation]], str | None]:
        try:
            # Hosts with a liveness probe (docker containers) are asked first:
            # a declared-but-down container definitively carries no processes
            # — a clean empty scan, not an unreachable host — and exec'ing it
            # would auto-start its whole compose stack (issue #139; docker is
            # a test aid, never a tunnel requirement).
            if getattr(host, "is_running", None) is not None and not await _device_running(host):
                return [], None
            result = await _device_read(host, DISCOVERY_PS_COMMAND)
            if result.timed_out:
                logger.warning(f"otto tunnel: timed out scanning host {host.id!r}")
                return [], host.id
            observed = parse_process_discovery(result.value)
        except TunnelNotMeasuredError:
            # BEFORE both arms below, and this is the whole reason the arm is
            # written out rather than left to the wide one. A backstop raise
            # filed as "could not scan host X" is a DIFFERENT WRONG STORY from
            # the one it replaced: `list` would print `partial scan — could
            # not reach: …` naming every host in the lab, and
            # `discover_tunnel_records` would raise TunnelScanFailedError at
            # the monitor, both accusing a bed nobody spoke to. Re-raised bare
            # so the mapping to `TunnelDiscovery.not_measured` keeps one home,
            # in `discover_tunnels`.
            raise
        except asyncio.TimeoutError:
            # Still reachable via the is_running() probe above, which remains
            # externally wrapped.
            logger.warning(f"otto tunnel: timed out scanning host {host.id!r}")
            return [], host.id
        except Exception as e:  # noqa: BLE001 — best-effort scan; name + skip
            logger.warning(f"otto tunnel: could not scan host {host.id!r}: {e}")
            return [], host.id
        return [(host.id, obs) for obs in observed], None

    gathered = await asyncio.gather(*(scan(h) for h in hosts))
    observations = [pair for host_pairs, _u in gathered for pair in host_pairs]
    unreachable = [host_id for _pairs, host_id in gathered if host_id is not None]
    return observations, unreachable


# DEBT(no-tuple-return): observations plus error list; converts with scan().
# ast-grep-ignore: no-tuple-return
async def discover_observations(lab: "Lab") -> tuple[list[tuple[str, Observation]], list[str]]:
    """Every tagged tunnel process across the lab's ``has_bash`` hosts."""
    hosts = [h for h in lab.hosts.values() if getattr(h, "has_bash", False)]
    return await _scan_hosts(hosts)


@dataclass(frozen=True, slots=True)
class DiscoveredTunnel:
    """One live tunnel: intended shape + what was actually observed."""

    tunnel: Tunnel
    present: set[ProcKey]
    missing: set[ProcKey]
    """Expected-but-absent processes on hosts that WERE scanned. Absence on
    an unreachable host is unknown, not missing (spec §9)."""
    age_seconds: int
    """Oldest observed process age (max etime) — the tunnel's creation age."""
    uncertain: bool
    """True when >=1 chain host was unreachable during the scan."""

    @property
    def status(self) -> str:
        """``ok`` / ``degraded (<present>/<expected>)``, ``?``-suffixed if uncertain."""
        expected = len(self.tunnel.expected_processes())
        base = "ok" if not self.missing else f"degraded ({len(self.present)}/{expected})"
        return f"{base}?" if self.uncertain else base

    @property
    def health(self) -> Literal["ok", "degraded", "uncertain"]:
        """Single-word health class — the monitor wire enum's exact values.

        ``uncertain`` dominates ``degraded`` dominates ``ok``. The human
        :attr:`status` string stays deliberately richer (it shows
        degradation AND uncertainty at once, e.g. ``degraded (4/6)?``);
        any consumer needing ONE word — the GUI, a future CLI filter —
        must read this, never re-derive from ``missing``/``uncertain``.
        """
        if self.uncertain:
            return "uncertain"
        if self.missing:
            return "degraded"
        return "ok"


@dataclass(frozen=True, slots=True)
class TunnelDiscovery:
    """A full scan: the tunnels seen plus the hosts that couldn't be scanned."""

    tunnels: list[DiscoveredTunnel]
    unreachable: list[str]

    not_measured: bool = False
    """``True`` when a scan was ATTEMPTED and refused: at least one host was
    going to be asked, and a ``--dry-run`` declined to ask it.

    A THIRD state, and neither of the two above. :attr:`tunnels` being empty
    with :attr:`unreachable` empty means "every host answered and none of them
    is running an otto tunnel" — a real, complete, useful answer — and that is
    exactly what a ``--dry-run`` used to report, because the synthetic reply
    parses to zero observations. It was the only wrong answer indistinguishable
    from a right one.

    Reporting it as :attr:`unreachable` instead would be the other wrong story:
    that cell means a host was asked and did not answer, and it sends an
    operator to look at the network. Nothing was asked.

    A ``bool`` rather than a message: there is exactly one reason to be here
    and the sentence belongs to the renderer. APPENDED, because this is a
    public dataclass and inserting a field mid-order silently rebinds
    positional construction instead of failing.

    When it is ``True``, :attr:`tunnels` and :attr:`unreachable` are both empty
    BY CONSTRUCTION rather than by measurement.

    THE ONE DRY RUN THAT IS ``False``, and it is not an oversight: a lab that
    declares no ``has_bash`` host has nothing to scan, so the refusal never
    fires and this stays ``False`` with an empty, COMPLETE answer. That is the
    honest verdict — :func:`discover_observations` only ever visits ``has_bash``
    hosts and ``otto.tunnel.manage._validate_chain_shape`` refuses a
    non-bash chain member precisely so nothing otto builds can live outside
    that set, which makes "no otto tunnels" a conclusion from LAB DATA. A real
    pass over the same lab returns the identical value. Forcing ``True`` here
    would decline a question otto can answer, the mirror image of the bug this
    field exists for."""


def group_observations(
    observations: list[tuple[str, Observation]], unreachable: list[str]
) -> list[DiscoveredTunnel]:
    """Group per-host observations by tunnel id and compute per-tunnel status."""
    unreachable_set = set(unreachable)
    by_id: dict[str, list[tuple[str, Observation]]] = {}
    for origin, obs in observations:
        by_id.setdefault(obs.parsed.tunnel.id, []).append((origin, obs))
    out: list[DiscoveredTunnel] = []
    for _tid, group in sorted(by_id.items()):
        tunnel = group[0][1].parsed.tunnel
        present: set[ProcKey] = {(origin, o.parsed.direction, o.parsed.role) for origin, o in group}
        expected = tunnel.expected_processes()
        missing = {k for k in expected - present if k[0] not in unreachable_set}
        chain_hosts = {hop.host for hop in tunnel.path}
        out.append(
            DiscoveredTunnel(
                tunnel=tunnel,
                present=present,
                missing=missing,
                age_seconds=max(o.age_seconds for _origin, o in group),
                uncertain=bool(chain_hosts & unreachable_set),
            )
        )
    return out


async def discover_tunnels(lab: "Lab") -> TunnelDiscovery:
    """Discover live otto tunnels across the lab (the monitor-facing surface).

    UNDER ``--dry-run`` THIS HAS NO SHORT-CIRCUIT OF ITS OWN, on purpose.
    :func:`~otto.tunnel.manage.add_tunnel` and the two remove entry points need
    one because they have a plan to build; discovery has none — reading IS its
    job — so the honest answer is whatever the read attempt produces, and the
    backstop in ``_device_read`` produces exactly it. Routing it this way
    rather than adding a second ``is_dry_run()`` here is what keeps the arm
    below LIVE: a hand-written short-circuit would make it a branch nothing
    ever takes.

    The result is a :attr:`TunnelDiscovery.not_measured` report, never a raise,
    because every caller that reaches this wants to SAY something: ``otto
    tunnel list`` renders the state, and
    ``otto.tunnel.records.discover_tunnel_records`` re-raises for the
    monitor, which needs an exception to keep its last known set.
    """
    try:
        observations, unreachable = await discover_observations(lab)
    except TunnelNotMeasuredError:
        return TunnelDiscovery(tunnels=[], unreachable=[], not_measured=True)
    return TunnelDiscovery(
        tunnels=group_observations(observations, unreachable), unreachable=unreachable
    )
