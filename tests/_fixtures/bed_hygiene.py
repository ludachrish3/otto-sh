"""BedHygiene: per-host snapshot/diff oracle for the tier-3 chaos lane.

Consolidates the piecemeal leftover checks (chaos spec, Tier 3): otto-tagged
tunnel daemons, impair timers, nc listeners, tc qdisc state, /tmp/otto*
staging entries, shell-history digest, and docker container/network probes —
probed over a FRESH connection, snapshot before / diff after, failure naming
the host and each leftover. Pre-existing dirt is snapshotted out (the
tunnel_bed.py pattern): a dirty bed going in is never blamed on the scenario,
and never masks a NEW leftover of the same kind.

Docker probes run on Plan 5 scenarios; they tolerate docker-less hosts by
collapsing to empty sets. Local-side leaks (transports, loops, fds) stay with
the existing repo-wide detectors; this module is remote-state only.
"""

import dataclasses

from otto.link.sentinel import IMPAIR_PS_COMMAND, parse_impair_ps
from otto.logger.mode import LogMode
from otto.result import Result
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery


def argv_pattern(needle: str) -> str:
    """Bracket-trick ``needle`` for a ``pgrep -f``/``pkill -f`` pattern.

    The remote side runs every probe through a shell whose OWN argv carries
    the full command text — so a plain ``pkill -f 'sleep 313'`` matches (and
    kills) its own wrapper shell, which dies mid-command and reports
    ``Failed/retcode=-1``. Wrapping the first char in a regex class breaks
    the adjacency inside the wrapper's argv (``[s]leep 313`` never matches
    the literal text ``[s]leep 313``) while still matching the real target's
    ``sleep 313``. Before the G5 probe contract these self-kills were
    REPORTED as failures nobody read.

    One spelling for every lane: Wave 14 retired the last hand-rolled
    siblings outside the chaos lanes (tunnel_stability's ``cancel_auto_cont``,
    the tunnel/link e2e socat cleanups, and the session-stability
    ``grep -v "$$"`` nc-probe mirrors, which now use ``_NC_LISTENER_PROBE``
    below). Total by contract: the needle must be non-empty and start with a
    character that is literal inside a regex class — every real needle starts
    with a letter.
    """
    if not needle or not needle[0].isalnum():
        raise ValueError(f"argv_pattern needs a needle starting alphanumeric, got {needle!r}")
    return f"[{needle[0]}]{needle[1:]}"


# Both bed netdevs matter: eth2 carries the declared data-plane link the
# connection-drop scenario impairs; eth1 (mgmt) must stay impairment-free
# ALWAYS — a qdisc appearing there means a placement guard failed.
_QDISC_DEVS = ("eth1", "eth2")
# Bracket-tricked so the probe's own wrapper shell can never appear in (or be
# filtered from) the listener list: the old `| grep -v "$$"` self-filter also
# dropped any REAL listener whose line contained the wrapper's pid as a
# substring (e.g. wrapper pid 1520 vs a listener on port 15200) — the same
# "oracle reads clean" class the G5 contract exists to kill.
# WIDENED because the GET direction used to spawn ``nc -Nl``, which ``nc -l``
# cannot match: for as long as this probe existed it saw only half the
# listeners it exists to find. Proved 2026-08-10 — a sweep with a wider pattern
# turned three leaked pairs on test2 into six, the invisible half all ``-Nl``.
# Matching the flag cluster — ``l`` anywhere in it, not just last — rather than
# listing spellings keeps a new one from re-opening the same hole silently. `l`
# last was the first attempt and still missed ``nc -lp PORT``, the standard GNU
# netcat listener spelling: a narrower fix for a blind spot is still a blind
# spot. The cluster reading paid off again on 2026-08-25, when otto's listener
# became ``nc -l -p PORT`` in both directions: this pattern needed no edit to
# keep matching it, nor to keep matching the ``-Nl`` orphans an older build may
# still have left on the bed. ``tests/unit/test_bed_hygiene.py`` carries all
# three eras as rows.
_NC_LISTENER_PROBE = f'pgrep -af "{argv_pattern("nc")} -[A-Za-z]*l[A-Za-z]*( |$)" || true'
_STAGING_PROBE = "ls -d /tmp/otto-* /tmp/otto_* 2>/dev/null || true"
_HISTORY_PROBE = "cat ~/.bash_history 2>/dev/null | sha256sum || true"
# Docker accumulation probes (Plan 5). `-a` deliberately: exited containers
# are accumulation too (the pile-up failure mode). Tolerant of docker-less
# hosts — the guard collapses to empty output, never an error.
_DOCKER_PS_PROBE = (
    "command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.ID}} {{.Names}}' || true"
)
_DOCKER_NET_PROBE = (
    "command -v docker >/dev/null 2>&1 && docker network ls --format '{{.Name}}' || true"
)
_PROBE_TIMEOUT = 30


class ProbeFailedError(RuntimeError):
    """A bed probe itself failed — its answer is MISSING, not clean.

    Raised instead of letting a non-ok ``host.exec`` result flow into an
    oracle: a timed-out probe's ``value`` is the error text ("Command timed
    out after 30s"), which the snapshot parsers happily turn into empty sets
    and phantom entries — i.e. a clean-looking bed manufactured by the exact
    failure (SSH blackhole, reboot) the chaos lane exists to create.
    """


def check_probe_result(host_name: str, result: Result) -> None:
    """Raise :class:`ProbeFailedError` for a non-ok probe ``Result``.

    The single spelling of the status check (G5): both ``snapshot_host`` and
    the tier-3 ``run_probe``/``probe_text`` route through here. The check
    binds the HELPERS — a factory that unwraps ``.value`` before returning
    forfeits it, which is why the honesty pins also carry an AST scan over
    the chaos lanes banning exactly that shape (and unbracketed pattern
    kills). Consumer spellings outside those two scans remain review
    territory, stated per the quality-gates page's blind-spot rule.
    """
    if result.is_ok:
        return
    command = getattr(result, "command", "") or "<no command recorded>"
    raise ProbeFailedError(
        f"probe on {host_name} failed (status={result.status.name}): "
        f"{command!r} -> {result.value!r}"
    )


@dataclasses.dataclass(frozen=True)
class HygieneSnapshot:
    tunnel_procs: frozenset  # str lines: "pid tunnel-id" from sentinel parse
    impair_timers: frozenset  # str lines: "pid link-id netdev [selector]"
    nc_listeners: frozenset  # raw pgrep lines
    qdiscs: dict  # netdev -> raw `tc qdisc show dev X` text (stripped)
    staging: frozenset  # /tmp/otto* entries
    history_digest: str  # sha256 line of ~/.bash_history
    docker_containers: frozenset  # docker ps lines: "id names"
    docker_networks: frozenset  # docker network names


async def snapshot_host(host) -> HygieneSnapshot:
    """Probe one host over its (fresh) connection; never mutates anything.

    Raises :class:`ProbeFailedError` (host- and probe-named) when any probe
    comes back non-ok — a snapshot built from failed probes is not a
    snapshot, and diffing one reports "clean" for the exact scenarios the
    chaos lane manufactures.
    """

    async def _out(cmd: str) -> str:
        result = await host.exec(cmd, timeout=_PROBE_TIMEOUT, log=LogMode.QUIET)
        check_probe_result(getattr(host, "id", "<unknown host>"), result)
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
        docker_containers=frozenset(
            line for line in (await _out(_DOCKER_PS_PROBE)).splitlines() if line
        ),
        docker_networks=frozenset(
            line for line in (await _out(_DOCKER_NET_PROBE)).splitlines() if line
        ),
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
        ("docker container", before.docker_containers, after.docker_containers),
        ("docker network", before.docker_networks, after.docker_networks),
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
