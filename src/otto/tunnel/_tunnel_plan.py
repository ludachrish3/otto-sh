"""``otto tunnel check`` before any device: its refusals and its dry-run plan.

Everything here reads lab data and the given arguments only; nothing touches
a host. :func:`validate` makes every refusal ``add_tunnel`` would make, from
the same pure resolvers ``add_tunnel``'s own dry run uses, and
:func:`dry_run_plan` says what a real run would do, including which proof a
``--dest`` would get. :mod:`otto.tunnel.check` runs the check itself.
"""

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from ..check.clock import PROBE_TIMEOUT_S, SOCAT_CONNECT_S
from ..check.sweep import (
    COMMAND_ALLOWANCE_S,
    SWEEP_MIN_AGE_S,
    age_text,
    probe_worst_s,
    run_end_worst_s,
)
from . import _tunnel_rows as rows
from ._tunnel_echoes import _READY_INTERVAL_S, KILL_SETTLE_S, READY_DEADLINE_S
from ._tunnel_rows import LOOPBACK
from .carrier import build_carrier
from .check_probes import _BULK_UDP_EOF_WAIT_S, HANDSHAKE_DRAIN_S, SMALL_SIZES, TRIPS
from .manage import (
    _DIAGNOSIS_TIMEOUT,
    _VERIFY_RETRY_DELAY,
    EndpointSpec,
    PlannedHop,
    ensure_dest_outside_chain,
    planned_chain,
    planned_hop,
)

NOT_CONTACTED = "no device was contacted — nothing was measured"


def spec_text(spec: EndpointSpec) -> str:
    """Show one endpoint as given: ``host`` or ``host@if``."""
    host_id, iface = spec
    return f"{host_id}@{iface}" if iface else host_id


def runs_no_shell(host: Any) -> bool:
    """Whether the lab says *host* has no shell, so otto may never run a command on it."""
    return getattr(host, "has_bash", True) is False


@dataclass(frozen=True)
class PlannedPath:
    """The path, and the ``--dest``, resolved as far as lab data goes."""

    chain: list[PlannedHop]
    dest: PlannedHop | None


def validate(
    lab: Any,
    hosts: list[EndpointSpec],
    protocols: list[str],
    dest: EndpointSpec | None,
    carrier: str,
) -> PlannedPath:
    """Make every refusal ``add_tunnel`` makes from lab data alone; resolve what that can.

    Raises:
        ValueError: the refusal, worded as ``add_tunnel`` words it.
    """
    planned = planned_chain(lab, hosts)
    planned_dest = None
    if dest is not None:
        planned_dest = planned_hop(lab, dest)
        ensure_dest_outside_chain(dest[0], {host_id for host_id, _ in hosts})
    carrier_obj = build_carrier(carrier)()
    for protocol in protocols:
        if protocol not in carrier_obj.supported_protocols:
            supported = ", ".join(sorted(carrier_obj.supported_protocols))
            raise ValueError(
                f"carrier {carrier!r} does not support protocol {protocol!r} (use {supported})"
            )
    return PlannedPath(planned, planned_dest)


def _where(p: PlannedHop) -> str:
    return p.ip or "<its address, resolved at run time>"


def _dest_plan(
    last: str, dest: PlannedHop, protocols: list[str], *, port: int, spec: str
) -> list[str]:
    """Say which proof a ``--dest`` would get, and what each would do."""
    name = dest.hop.host
    split = [
        (
            f"the throwaway tunnel would deliver to the fwd echo on {last}, payload-verifying "
            "the hop chain"
        )
    ]
    if "tcp" in protocols:
        split.append(
            f"{last} would make a TCP handshake to {name} at {_where(dest)}:{port} "
            "(--port itself; no payload is sent)"
        )
    if "udp" in protocols:
        split.append(f"UDP to {name} would be unmeasured (no-reply-oracle)")
    split_text = "; ".join(split) + " — closing that gap is #440"
    if runs_no_shell(dest.host):
        return [
            (
                f"--dest {spec}: the lab says has_bash=False, so it runs nothing of otto's and "
                f"is never contacted — split proof: {split_text}"
            )
        ]
    return [
        (
            f"--dest {spec}: would fingerprint {name} (one probe command); which proof "
            "applies is decided by that fingerprint"
        ),
        (
            f"if {name} runs socat and bash, full proof: {name} is swept and torn down like a "
            f"hop, a segment echo on {name} at {_where(dest)}:<scratch> takes {TRIPS} 1 B "
            f"trips from {last}, the fwd echo on {name} at {_where(dest)}:<scratch> replaces "
            f"the one on {last}, and the tunnel is built with --dest {spec}"
        ),
        f"otherwise, split proof: {split_text}",
    ]


def dry_run_plan(
    planned: PlannedPath,
    protocols: list[str],
    *,
    port: int,
    dest: EndpointSpec | None,
    carrier: str,
) -> list[str]:
    """Build the lines saying what a real run would do, from lab data alone."""
    ids = [p.hop.host for p in planned.chain]
    first, last = ids[0], ids[-1]
    lines = [
        f"would fingerprint {', '.join(ids)} (one probe command each)",
        (
            f"would sweep leftover otto-check echoes older than {age_text(SWEEP_MIN_AGE_S)} "
            f"on {', '.join(ids)}, and remove the throwaway tunnels they name, scanning the lab "
            "for them the way `otto tunnel remove` does; a younger one may be a running "
            "check's, and would be left"
        ),
        (
            f"would check --port {port} is free for {', '.join(protocols)} on {first} and "
            f"{last} (looked at, never bound), and allocate a scratch service port at random "
            "from above every hop's ephemeral range to build on instead"
        ),
    ]
    lines += [
        (
            f"would run a segment echo on {b.hop.host} at {_where(b)}:<scratch> and send it "
            f"{TRIPS} round trips of 1 B from {a.hop.host}"
        )
        for a, b in pairwise(planned.chain)
    ]
    sizes = " and ".join(f"{s} B" for s in SMALL_SIZES)
    bulks = " / ".join(f"{rows.bulk_label(p)} ({p})" for p in protocols)
    lines += [
        (
            f"would run the fwd echo on {last} at {LOOPBACK}:<scratch> and the rev echo on "
            f"{first} at {LOOPBACK}:<scratch>"
        ),
        (
            f"would build a throwaway tunnel {' → '.join(ids)} on the scratch port with "
            f"carrier {carrier}, for {', '.join(protocols)}, one protocol at a time, and "
            "check `tunnel list` sees it ok"
        ),
        (
            f"would send {TRIPS} round trips each of {sizes} and one checksum-compared bulk "
            f"of {bulks}, fwd then rev"
        ),
        (
            "would remove the tunnel and every echo, then re-scan every hop to verify "
            "nothing tagged survives"
        ),
    ]
    if dest is None or planned.dest is None:
        lines.append(f"no --dest: the tunnel would deliver to the fwd echo on {last}")
    else:
        lines += _dest_plan(last, planned.dest, protocols, port=port, spec=spec_text(dest))
    lines.append(NOT_CONTACTED)
    return lines


# --------------------------------------------------------------------------
# The worst-case run length the leftover sweep's age bound must clear
# --------------------------------------------------------------------------


def _echo_start_worst_s() -> float:
    """Launch one echo, poll its socket listing up to the readiness deadline, then scan for it.

    The last listing shows the address up just as the deadline passes, and
    the one sweep scan that confirms the echo is this run's follows it.
    Hand-counted: update it when adding a probe or host command to
    ``otto.tunnel._tunnel_echoes.start_echo``.
    """
    return COMMAND_ALLOWANCE_S + READY_DEADLINE_S + _READY_INTERVAL_S + 2 * COMMAND_ALLOWANCE_S


_KILL_SETTLE_WORST_S = KILL_SETTLE_S + _READY_INTERVAL_S
"""The re-scans after a kill that leaves a survivor: they run to the settle deadline,
and the last one starts just before it (``otto.tunnel._tunnel_echoes._scan_after_kill``)."""


def _removal_worst_s(hosts: int) -> float:
    """``remove_tunnel`` of one throwaway tunnel: a lab-wide scan, a kill per host, a re-scan.

    Hand-counted: update it when adding a host command to
    :func:`~otto.tunnel.manage.remove_tunnel`.
    """
    return (hosts + 2) * COMMAND_ALLOWANCE_S


def _add_worst_s(hosts: int) -> float:
    """``add_tunnel`` refusing late, after every step it takes.

    It resolves each hop, scans for conflicts, checks each hop's tools, probes
    the port budget and launches every process; then it verifies, settles and
    retries, diagnoses the failure, and the rollback kills on every hop.
    Hand-counted: update it when adding a host command to
    :func:`~otto.tunnel.manage.add_tunnel`.
    """
    commands = hosts + 1 + hosts + 1 + 2 * hosts + 2 + hosts
    return commands * COMMAND_ALLOWANCE_S + _VERIFY_RETRY_DELAY + _DIAGNOSIS_TIMEOUT


def _column_worst_s(hosts: int, protocol: str, *, split_dest: bool) -> float:
    """One protocol's column, every probe running to its cut-off.

    Hand-counted: update it when adding a probe or host command to
    ``otto.tunnel.check._column`` or anything it runs (the service-port,
    segment, payload and list rows, and ``_teardown``).
    """
    trips = probe_worst_s(TRIPS * PROBE_TIMEOUT_S)
    eof_wait = _BULK_UDP_EOF_WAIT_S if protocol == "udp" else PROBE_TIMEOUT_S
    bulk = probe_worst_s(SOCAT_CONNECT_S + PROBE_TIMEOUT_S + eof_wait)
    service_port = 4 * COMMAND_ALLOWANCE_S  # a socket listing and a tunnel scan, both ends
    segments = (hosts - 1) * (_echo_start_worst_s() + trips + 2 * COMMAND_ALLOWANCE_S)
    build = 2 * _echo_start_worst_s() + _add_worst_s(hosts)
    payloads = 2 * (len(SMALL_SIZES) * trips + bulk)  # fwd, then rev
    listed = COMMAND_ALLOWANCE_S  # one lab-wide scan, every host at once
    # Per host: scan, kill, re-scan, and the re-scans while a killed echo exits.
    teardown = _removal_worst_s(hosts) + hosts * (3 * COMMAND_ALLOWANCE_S + _KILL_SETTLE_WORST_S)
    handshake = probe_worst_s(SOCAT_CONNECT_S + HANDSHAKE_DRAIN_S) if split_dest else 0.0
    return service_port + segments + build + payloads + listed + teardown + handshake


def worst_case_run_s(echo_hosts: int, protocols: list[str], *, split_dest: bool = False) -> float:
    """Return the longest one ``otto tunnel check`` can run, in seconds, every probe at its cut-off.

    *echo_hosts* counts every host that runs an echo: each hop, plus a
    full-proof ``--dest``. *split_dest* adds the split proof's handshake.
    Before the columns, each host is resolved, fingerprinted, scanned by the
    sweep, has its leftovers killed and is scanned again, for up to the kill
    settle time, to confirm they went, and one leftover tunnel is removed;
    then the free-port probe runs on every host at once. Each column then
    runs as ``_column_worst_s`` charges it, and the run ends on a host
    that stopped answering (:func:`~otto.check.sweep.run_end_worst_s`).
    :data:`~otto.check.sweep.SWEEP_MIN_AGE_S` must stay above this for
    the paths otto expects; a unit test holds it there. Hand-counted: update
    it when adding a probe or host command to
    :func:`~otto.tunnel.check.check_tunnel` before its columns (its sweep
    included), and the totals in ``SWEEP_MIN_AGE_S``'s docstring with it.
    """
    before = (
        (5 * echo_hosts + 1) * COMMAND_ALLOWANCE_S
        + echo_hosts * _KILL_SETTLE_WORST_S
        + _removal_worst_s(echo_hosts)
    )
    columns = sum(_column_worst_s(echo_hosts, p, split_dest=split_dest) for p in protocols)
    # A silent host: remove_tunnel's scan, then each host's clean-up scan.
    return before + columns + run_end_worst_s(echo_hosts + 1)
