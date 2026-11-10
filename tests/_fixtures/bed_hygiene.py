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
the existing suite-wide detectors; this module is remote-state only.
"""

import dataclasses

from otto.link.netem import NetEmImpairer, parse_scoped_outputs
from otto.link.sentinel import IMPAIR_PS_COMMAND, parse_impair_ps
from otto.logger.mode import LogMode
from otto.result import Result
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery
from tests._fixtures.docker_reap import E2E_IMAGE_LABEL, ORPHAN_FRAGMENTS, image_ids_command


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


# Every netdev the host lists, not a fixed pair: eth2 carries the declared
# data-plane link, eth1 (mgmt) must stay impairment-free ALWAYS (a qdisc there
# means a placement guard failed), and the link e2e creates VLAN links whose
# qdiscs a fixed list would never see. Per netdev the probe reads the qdisc
# and filter pair `NetEmImpairer.scoped_read_commands` reads, because a scoped
# otto tree classifies as foreign without its filters.
_NETDEV_PROBE = "tc qdisc show 2>/dev/null || true"
_IMPAIRER = NetEmImpairer()
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
_STAGING_PROBE = "ls -d /tmp/otto-* /tmp/otto_* /run/chaos/shutdown_hold 2>/dev/null || true"
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
_DOCKER_IMAGES_PROBE = (
    f"command -v docker >/dev/null 2>&1 && {image_ids_command(f'label={E2E_IMAGE_LABEL}')} || true"
)
_PROBE_TIMEOUT = 30


def probe_lines(text: str) -> list[str]:
    """The lines of a line-per-item probe's exact ``value``; ``[]`` when it printed nothing.

    ``split("\\n")``, not ``splitlines()``: ``splitlines()`` drops one trailing
    newline, so a stray empty line (#711) would vanish instead of surfacing as
    an ``""`` item that a before/after comparison reports.
    """
    return text.split("\n") if text else []


def local_nc_listeners() -> list[str]:
    """``nc`` listeners on THIS box, as ``pgrep -af`` lines.

    The local counterpart of running :data:`_NC_LISTENER_PROBE` on a bed host,
    and deliberately the same probe text rather than a second spelling: the
    flag-cluster pattern is the accumulated answer to ``-Nl``, ``-lp`` and
    ``nc -l -p PORT``, and a hand-rolled local ``pgrep -af "nc -l"`` re-opens
    every hole that comment records. Running it through ``sh -c`` is what lets
    the probe stay one string; the bracket trick in :func:`argv_pattern` is
    what keeps that wrapper shell — whose own argv carries the pattern — from
    matching itself.

    Returns the raw lines. Callers compare a BEFORE list against an AFTER one:
    an absolute "no listeners exist" assertion inherits every orphan the box
    was already carrying, which is not this test's leak and reddens it for
    reasons no run of it can fix.
    """
    import subprocess

    return (
        subprocess.run(
            ["sh", "-c", _NC_LISTENER_PROBE],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT,
        )
        .stdout.strip()
        .splitlines()
    )


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
class QdiscReads:
    """One netdev's raw ``tc qdisc show`` and ``tc filter show ... parent 1:`` outputs."""

    qdisc: str
    filters: str = ""


@dataclasses.dataclass(frozen=True)
class HygieneSnapshot:
    tunnel_procs: frozenset  # str lines: "pid tunnel-id" from sentinel parse
    impair_timers: frozenset  # str lines: "pid link-id netdev [selector]"
    nc_listeners: frozenset  # raw pgrep lines
    qdiscs: dict  # netdev -> QdiscReads (exact values)
    staging: frozenset  # /tmp/otto* entries
    history_digest: str  # sha256 line of ~/.bash_history
    docker_containers: frozenset  # docker ps lines: "id names"
    docker_networks: frozenset  # docker network names
    docker_images: frozenset = frozenset()  # full ids of the e2e-labelled images


def netdevs_listed(qdisc_show: str) -> list[str]:
    """The netdevs a bare ``tc qdisc show`` names, in first-seen order."""
    seen: list[str] = []
    for line in qdisc_show.splitlines():
        tokens = line.split()
        if "dev" in tokens[:-1]:
            dev = tokens[tokens.index("dev") + 1]
            if dev not in seen:
                seen.append(dev)
    return seen


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
        return result.value

    tunnel_raw = await _out(DISCOVERY_PS_COMMAND)
    impair_raw = await _out(IMPAIR_PS_COMMAND)
    qdiscs = {}
    for dev in netdevs_listed(await _out(_NETDEV_PROBE)):
        qdisc_cmd, filter_cmd = _IMPAIRER.scoped_read_commands(netdev=dev)
        qdiscs[dev] = QdiscReads(await _out(qdisc_cmd), await _out(filter_cmd))
    return HygieneSnapshot(
        tunnel_procs=frozenset(
            f"{o.pid} {o.parsed.tunnel.id}" for o in parse_process_discovery(tunnel_raw)
        ),
        impair_timers=frozenset(
            f"{t.pid} {t.link_id} {t.netdev} {t.selector.describe() if t.selector else ''}".strip()
            for t in parse_impair_ps(impair_raw)
        ),
        nc_listeners=frozenset(probe_lines(await _out(_NC_LISTENER_PROBE))),
        qdiscs=qdiscs,
        staging=frozenset(probe_lines(await _out(_STAGING_PROBE))),
        history_digest=await _out(_HISTORY_PROBE),
        docker_containers=frozenset(probe_lines(await _out(_DOCKER_PS_PROBE))),
        docker_networks=frozenset(probe_lines(await _out(_DOCKER_NET_PROBE))),
        docker_images=frozenset(probe_lines(await _out(_DOCKER_IMAGES_PROBE))),
    )


def tunnel_ids(snapshot: HygieneSnapshot) -> list[str]:
    """The tunnel ids a snapshot's ``tunnel_procs`` lines name (the formatter above's inverse)."""
    return sorted({line.split(maxsplit=1)[1] for line in snapshot.tunnel_procs if " " in line})


def listener_pids(snapshot: HygieneSnapshot) -> list[int]:
    """The pids of a snapshot's nc listeners: pgrep's leading field on each line."""
    pids = []
    for line in snapshot.nc_listeners:
        head = line.split(maxsplit=1)[0] if line.strip() else ""
        if head.isdigit():
            pids.append(int(head))
    return sorted(pids)


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
        ("docker image", before.docker_images, after.docker_images),
    ):
        leftovers.extend(f"new {label}: {item}" for item in sorted(a - b))
    # Every netdev either side lists. One a scenario added or removed (a veth,
    # a VLAN) is a change only when the tree on it is not the kernel's default:
    # the netdev itself is the link layer's business, its qdisc is ours.
    for dev in sorted(set(before.qdiscs) | set(after.qdiscs)):
        b, a = before.qdiscs.get(dev), after.qdiscs.get(dev)
        if a == b:
            continue
        if (a is None or b is None) and _kind(a if b is None else b) == "clean":
            continue
        leftovers.append(f"{dev}: qdisc changed: before={b!r} after={a!r}")
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


def _kind(reads: "QdiscReads | None") -> str:
    """The impairer's classification of one netdev's reads (``clean`` for none)."""
    return "clean" if reads is None else parse_scoped_outputs(reads.qdisc, reads.filters).kind


def format_hygiene_report(element: str, leftovers: list) -> str:
    lines = "\n  ".join(leftovers)
    return (
        f"BedHygiene: scenario left {element} dirty ({len(leftovers)} leftover(s)):\n"
        f"  {lines}\n"
        f"Pre-existing state was snapshotted out — these appeared DURING the scenario."
    )


def _orphan_named(name_line: str) -> bool:
    return any(fragment in name_line for fragment in ORPHAN_FRAGMENTS)


def residual_dirt(snapshot: HygieneSnapshot) -> list[str]:
    """Everything on the host that is the test suite's to remove: the ABSOLUTE predicate.

    :func:`diff_snapshots` is new-only (the chaos bracket must never blame a
    scenario for dirt it inherited); the bed reap asks the other question,
    "what is still here", and this answers it: every otto-tunnel daemon,
    otto-impair timer, nc listener the probe lists and ``/tmp/otto*`` staging
    entry (the bed hosts are the suite's fixtures, and those namespaces are
    the suite's by the oracle's definition); every docker container and
    network whose name carries an orphan fragment; every e2e-labelled image;
    and, per netdev, a qdisc tree otto placed (``whole`` or ``scoped`` by the
    classifier ``netem.py`` already has). A ``foreign`` tree is reported too,
    marked as such: the reap never deletes what otto did not create, but a
    bed is not clean while it is there. A ``clean`` netdev is silent.
    """
    found: list[str] = []
    for label, items in (
        ("otto-tunnel daemon", snapshot.tunnel_procs),
        ("otto-impair timer", snapshot.impair_timers),
        ("nc listener", snapshot.nc_listeners),
        ("staging entry", snapshot.staging),
        ("docker image", snapshot.docker_images),
    ):
        found.extend(f"{label}: {item}" for item in sorted(items))
    for label, items in (
        ("docker container", snapshot.docker_containers),
        ("docker network", snapshot.docker_networks),
    ):
        found.extend(f"{label}: {item}" for item in sorted(items) if _orphan_named(item))
    for dev in sorted(snapshot.qdiscs):
        reads = snapshot.qdiscs[dev]
        kind = _kind(reads)
        if kind in ("whole", "scoped"):
            found.append(f"{dev}: otto qdisc ({kind}): {reads.qdisc.strip()!r}")
        elif kind == "foreign":
            found.append(f"{dev}: foreign qdisc (not otto's, kept): {reads.qdisc.strip()!r}")
    return found


def format_residual_report(host: str, leftovers: list) -> str:
    """The absolute report: what is STILL on *host*, pre-existing or not."""
    lines = "\n  ".join(leftovers)
    return f"bed-reap: {host} still carries {len(leftovers)} leftover(s):\n  {lines}"
