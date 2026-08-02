"""BedHygiene: per-host snapshot/diff oracle for the tier-3 chaos lane.

Consolidates the piecemeal leftover checks (chaos spec, Tier 3): otto-tagged
tunnel daemons, impair timers, nc listeners, tc qdisc state, /tmp/otto*
staging entries, and the shell-history digest — probed over a FRESH
connection, snapshot before / diff after, failure naming the host and each
leftover. Pre-existing dirt is snapshotted out (the tunnel_bed.py pattern):
a dirty bed going in is never blamed on the scenario, and never masks a NEW
leftover of the same kind.

Docker probes deliberately absent — they ride Plan 5 with the docker
scenarios. Local-side leaks (transports, loops, fds) stay with the existing
repo-wide detectors; this module is remote-state only.
"""

import dataclasses

from otto.link.sentinel import IMPAIR_PS_COMMAND, parse_impair_ps
from otto.logger.mode import LogMode
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery

# Both bed netdevs matter: eth2 carries the declared data-plane link the
# connection-drop scenario impairs; eth1 (mgmt) must stay impairment-free
# ALWAYS — a qdisc appearing there means a placement guard failed.
_QDISC_DEVS = ("eth1", "eth2")
_NC_LISTENER_PROBE = 'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
_STAGING_PROBE = "ls -d /tmp/otto-* /tmp/otto_* 2>/dev/null || true"
_HISTORY_PROBE = "cat ~/.bash_history 2>/dev/null | sha256sum || true"
_PROBE_TIMEOUT = 30


@dataclasses.dataclass(frozen=True)
class HygieneSnapshot:
    tunnel_procs: frozenset  # str lines: "pid tunnel-id" from sentinel parse
    impair_timers: frozenset  # str lines: "pid link-id netdev [selector]"
    nc_listeners: frozenset  # raw pgrep lines
    qdiscs: dict  # netdev -> raw `tc qdisc show dev X` text (stripped)
    staging: frozenset  # /tmp/otto* entries
    history_digest: str  # sha256 line of ~/.bash_history


async def snapshot_host(host) -> HygieneSnapshot:
    """Probe one host over its (fresh) connection; never mutates anything."""

    async def _out(cmd: str) -> str:
        result = await host.exec(cmd, timeout=_PROBE_TIMEOUT, log=LogMode.QUIET)
        return (result.value or "").strip()

    tunnel_raw = await _out(DISCOVERY_PS_COMMAND)
    impair_raw = await _out(IMPAIR_PS_COMMAND)
    qdiscs = {}
    for dev in _QDISC_DEVS:
        qdiscs[dev] = await _out(f"tc qdisc show dev {dev} 2>/dev/null || true")
    return HygieneSnapshot(
        tunnel_procs=frozenset(
            f"{o.pid} {o.parsed.tunnel.id}" for o in parse_process_discovery(tunnel_raw)
        ),
        impair_timers=frozenset(
            f"{t.pid} {t.link_id} {t.netdev} {t.selector.describe() if t.selector else ''}".strip()
            for t in parse_impair_ps(impair_raw)
        ),
        nc_listeners=frozenset(
            line for line in (await _out(_NC_LISTENER_PROBE)).splitlines() if line
        ),
        qdiscs=qdiscs,
        staging=frozenset(line for line in (await _out(_STAGING_PROBE)).splitlines() if line),
        history_digest=await _out(_HISTORY_PROBE),
    )


def _short_digest(digest: str) -> str:
    """First 12 chars of a ``sha256sum`` line's hash column.

    Safe on empty/malformed input: ``digest`` is only ``""`` when the probe
    itself produced no output (a timed-out or failed ``host.exec`` — exactly
    what an SSH-blackhole or reboot scenario manufactures), never a real
    sha256sum of anything. ``.split()[0][:12]`` on that would raise
    ``IndexError`` — INSIDE the diff, which would kill the autouse bracket
    with a bare traceback instead of the oracle's host-named report.
    """
    if not digest:
        return "<none>"
    return digest.split(maxsplit=1)[0][:12]


def diff_snapshots(before: HygieneSnapshot, after: HygieneSnapshot) -> list:
    """Human-readable leftover lines; empty means clean. New-only semantics."""
    leftovers = []
    for label, b, a in (
        ("otto-tunnel daemon", before.tunnel_procs, after.tunnel_procs),
        ("otto-impair timer", before.impair_timers, after.impair_timers),
        ("nc listener", before.nc_listeners, after.nc_listeners),
        ("staging entry", before.staging, after.staging),
    ):
        leftovers.extend(f"new {label}: {item}" for item in sorted(a - b))
    leftovers.extend(
        f"{dev}: qdisc changed: before={before.qdiscs.get(dev, '')!r} "
        f"after={after.qdiscs.get(dev, '')!r}"
        for dev in sorted(after.qdiscs)
        if after.qdiscs.get(dev, "") != before.qdiscs.get(dev, "")
    )
    before_empty = not before.history_digest
    after_empty = not after.history_digest
    if before_empty != after_empty:
        # Exactly one side came back empty: the PROBE failed to read history
        # on that side (host unreachable, command timed out) — it is not
        # evidence the history changed, and calling it "digest changed" would
        # misattribute a probe failure as a scenario leftover. Say what
        # actually happened instead.
        leftovers.append(
            "shell history probe returned no output on one side "
            f"(before={_short_digest(before.history_digest)} "
            f"after={_short_digest(after.history_digest)}) — probe/read "
            "failure, not a confirmed digest change"
        )
    elif not before_empty and after.history_digest != before.history_digest:
        leftovers.append(
            f"shell history digest changed ({_short_digest(before.history_digest)} -> "
            f"{_short_digest(after.history_digest)}) — suppression leak?"
        )
    return leftovers


def format_hygiene_report(element: str, leftovers: list) -> str:
    lines = "\n  ".join(leftovers)
    return (
        f"BedHygiene: scenario left {element} dirty ({len(leftovers)} leftover(s)):\n"
        f"  {lines}\n"
        f"Pre-existing state was snapshotted out — these appeared DURING the scenario."
    )
