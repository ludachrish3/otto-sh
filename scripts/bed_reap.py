#!/usr/bin/env python3
"""The bed reap: remove what a run left on the bed, under the lease, by hand or at a lane's edges.

Every bed lane runs this after its acquire and before its release (the quick
reap); ``make bed-reap`` runs everything (``--full``) and is what the docs name
after a killed or interrupted run. The spec is
docs/superpowers/specs/2026-11-01-bed-lease-design.md ("Decisions: the reap").

``uv run python scripts/bed_reap.py --lab <lab.json> --pid PID
[--unless-all-under DIR -- PATHS...] [--full] [--restart-qemu]``

In order, printing one line per thing removed and ``bed-reap: clean`` when
there was nothing:

0. Under the lease, after a drain: the lease is acquired for *PID*'s run
   (re-entrant inside a lane's ``make``). A lease another live run holds is a
   refusal before any probe; so are this run's own survivors past the drain.
1. The unix bed hosts (the loaded hosts with ``os_type`` unix and no hop),
   each over a fresh connection, snapshotted by the hygiene oracle: the
   otto-tunnel daemons it lists (by id), otto-impair timers, otto-placed
   qdiscs, nc listeners and ``/tmp/otto*`` staging entries are removed through
   otto's own removers.
2. Docker, on the cell's container parents, under each host's own pool lease:
   orphan stacks, e2e-labelled images and expired otto scan snapshots.
3. The embedded coverage product, local to this machine: a declared build
   whose artifact is missing is rebuilt (a cut ``west build`` left it).
4. With ``--full`` only, the consoles: ``make console-logout`` (after ``make
   qemu-restart`` with ``--restart-qemu``), re-entering this run's lease.
5. The after-snapshot of every host, taken last: its residual dirt is the
   verdict, printed, and exit 1 if anything remains, any host was unreachable
   or any step failed.

Nothing here names a host, an IP or a lab path: a different ``--lab`` reaps a
different cell. Exit 0 clean, 1 refused or still dirty, 2 usage.
"""

import argparse
import asyncio
import contextlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import bed_lease  # noqa: E402
from scripts.bed_lease import (  # noqa: E402
    DEFAULT_DRAIN_S,
    DRAIN_ENV,
    EXIT_USAGE,
    Cell,
    UsageError,
    add_cell_arguments,
    add_run_arguments,
    classifier_says_skip,
    require_pid,
    root_from,
    seconds_from_env,
)
from scripts.lab_health import direct_ssh_host_ids  # noqa: E402

if TYPE_CHECKING:
    from tests._fixtures.bed_hygiene import HygieneSnapshot

PRODUCT_DIR = REPO_ROOT / "tests" / "project3" / "product"
CONSOLE_TARGETS = {False: ["console-logout"], True: ["qemu-restart", "console-logout"]}
"""The console step, by ``restart_qemu``: the Makefile's own targets, so the lines live there."""
EXIT_DIRTY = 1
PURPOSE = "bed-reap"


# --- what the reap calls (module names, so a test injects doubles) --------------


def _hygiene() -> Any:
    from tests._fixtures import bed_hygiene

    return bed_hygiene


def _docker() -> Any:
    from tests._fixtures import docker_reap

    return docker_reap


def _builds() -> Any:
    if str(PRODUCT_DIR) not in sys.path:
        sys.path.insert(0, str(PRODUCT_DIR))
    import product_builds

    return product_builds


async def remove_tunnels(lab: Any, tunnel_ids: list[str]) -> list[str]:
    """Remove the tunnels *tunnel_ids* (the ones a bed host's scan named); survivors raised.

    By id through :func:`otto.tunnel.remove_tunnel`, never the owner-agnostic
    sweep: that one reaps every lab host, the dev VM's own ``local`` included.
    """
    from otto.tunnel import remove_tunnel

    removed: list[str] = []
    for tunnel_id in tunnel_ids:
        report = await remove_tunnel(lab, tunnel_id)
        if report.survivors:
            raise RuntimeError(
                f"tunnel {tunnel_id}: processes survived removal: {report.survivors}"
            )
        removed.extend(report.removed_ids)
    return removed


async def cancel_timers(host: Any) -> int:
    """Cancel every otto expire-timer on *host*; the count."""
    from otto.link.manage import cancel_all_timers

    return await cancel_all_timers(host)


async def repair_links(lab: Any) -> list[str]:
    """Repair every declared link; the failures, if any (a skipped link is no failure)."""
    from otto.link import repair_all

    report = await repair_all(lab)
    return list(report.failures)


async def clear_qdisc(host: Any, netdev: str) -> None:
    """Delete the otto qdisc tree on *netdev* with the impairer's own ``tc qdisc del`` line.

    Through the link layer's privileged run (sudo unless root); its result is
    checked here, as every other removal's is.
    """
    from otto.link.manage import _root_run
    from otto.link.netem import NetEmImpairer

    _hygiene().check_probe_result(
        host.id, await _root_run(host, NetEmImpairer().clear_command(netdev))
    )


async def kill_listeners(host: Any, pids: list[int]) -> None:
    """Kill the nc listeners *pids* on *host*, as the user that owns them (the login user)."""
    from otto.host.daemon import kill_command

    await _checked(host, kill_command(pids))


async def remove_staging(host: Any, entries: list[str]) -> None:
    """Remove the ``/tmp/otto*`` staging *entries* (the oracle's probe lists them) on *host*."""
    await _checked(host, "rm -rf -- " + " ".join(entries))


async def run_lab_health(*, restart_qemu: bool) -> int:
    """Run the console step's Makefile targets in order; the first non-zero status, else 0."""
    for target in CONSOLE_TARGETS[restart_qemu]:
        proc = await asyncio.create_subprocess_exec(
            "make", "--no-print-directory", target, cwd=REPO_ROOT
        )
        status = await proc.wait()
        if status != 0:
            return status
    return 0


async def _checked(host: Any, command: str) -> None:
    from otto.logger.mode import LogMode

    result = await host.exec(command, timeout=60, log=LogMode.QUIET)
    _hygiene().check_probe_result(host.id, result)


# --- the hosts --------------------------------------------------------------------


def unix_bed_hosts(lab: Any, lab_file: Path) -> list[Any]:
    """Return the cell's hosts reached directly over SSH, by ``lab_health.py``'s one rule."""
    return [lab.hosts[host_id] for host_id in direct_ssh_host_ids(lab_file) if host_id in lab.hosts]


@dataclass
class Outcome:
    """What one reap did and found."""

    removed: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    """Hosts a snapshot could not be taken on: nothing was judged there."""
    residual: dict[str, list[str]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    """Steps that raised: a remover on a host (the rest of that host is skipped), a build."""
    links_touched: bool = False
    """A timer was cancelled or an otto qdisc cleared: the declared links get a repair pass."""

    @property
    def clean(self) -> bool:
        """Nothing left, every host reached, every step done."""
        return not (self.unreachable or self.failures or any(self.residual.values()))


async def _reap_host_state(lab: Any, host: Any, snapshot: "HygieneSnapshot", out: Outcome) -> None:
    """Remove what *snapshot* lists on *host*: tunnels, timers, listeners, staging, qdiscs."""
    hygiene = _hygiene()
    tunnel_ids = hygiene.tunnel_ids(snapshot)
    if tunnel_ids:
        for tunnel_id in await remove_tunnels(lab, tunnel_ids):
            out.removed.append(f"{host.id}: otto-tunnel {tunnel_id}")
    if snapshot.impair_timers:
        cancelled = await cancel_timers(host)
        out.removed.append(f"{host.id}: {cancelled} otto-impair timer(s)")
        out.links_touched = True
    pids = hygiene.listener_pids(snapshot)
    if pids:
        await kill_listeners(host, pids)
        out.removed.extend(f"{host.id}: nc listener pid {pid}" for pid in pids)
    entries = sorted(snapshot.staging)  # the oracle's probe lists only /tmp/otto* entries
    if entries:
        await remove_staging(host, entries)
        out.removed.extend(f"{host.id}: staging entry {entry}" for entry in entries)
    for dev in sorted(snapshot.qdiscs):
        reads = snapshot.qdiscs[dev]
        state = hygiene.parse_scoped_outputs(reads.qdisc, reads.filters)
        if state.kind in ("whole", "scoped"):
            await clear_qdisc(host, dev)
            out.removed.append(f"{host.id}: otto qdisc on {dev} ({state.kind})")
            out.links_touched = True


async def _guarded(host: Any, bucket: list[str], step: Any) -> bool:
    """Await *step*; a failure is reported in *bucket* under the host's name, and the rest go on."""
    try:
        await step
    except Exception as exc:  # noqa: BLE001 -- reported, never fatal to the other hosts
        bucket.append(f"{host.id}: {exc}")
        return False
    return True


async def _snapshot(host: Any, snapshots: dict) -> None:
    snapshots[host.id] = await _hygiene().snapshot_host(host)


async def _snapshot_residual(host: Any, out: Outcome) -> None:
    hygiene = _hygiene()
    out.residual[host.id] = hygiene.residual_dirt(await hygiene.snapshot_host(host))


async def _reap_unix(lab: Any, hosts: list[Any], out: Outcome) -> dict[str, Any]:
    """Snapshot and reap every host; the snapshots of the hosts that answered."""
    snapshots: dict[str, Any] = {}
    for host in hosts:
        if await _guarded(host, out.unreachable, _snapshot(host, snapshots)):
            await _guarded(host, out.failures, _reap_host_state(lab, host, snapshots[host.id], out))
    if out.links_touched:
        out.failures.extend(await repair_links(lab))
    return snapshots


async def _judge_unix(hosts: list[Any], snapshots: dict[str, Any], out: Outcome) -> None:
    """Take the after-snapshot of every host that answered, then close every connection."""
    for host in hosts:
        if host.id in snapshots:
            await _guarded(host, out.unreachable, _snapshot_residual(host, out))
    for host in hosts:
        with contextlib.suppress(Exception):
            await host.close()


async def _reap_docker(lab: Any, hosts: list[Any], out: Outcome) -> None:
    docker = _docker()
    from tests._fixtures._host_pool import lease_unix_host

    try:
        parents = docker.docker_parents(hosts)
    except Exception as exc:  # noqa: BLE001
        out.failures.append(f"docker parents: {exc}")
        return
    for name in parents:
        host = lab.hosts[name]
        try:
            with lease_unix_host([name]):
                out.removed.extend(
                    f"{name}: docker {item}" for item in await docker.reap_docker(host)
                )
                images = await docker.remove_labelled_images(host, docker.E2E_IMAGE_LABEL)
                out.removed.extend(f"{name}: docker image {image}" for image in images)
                swept = await docker.sweep_stale_scans(host)
                out.removed.extend(f"{name}: docker {item}" for item in swept.removed)
                out.failures.extend(f"{name}: docker scan sweep: {e}" for e in swept.errors)
        except Exception as exc:  # noqa: BLE001
            out.failures.append(f"{name}: docker: {exc}")


async def _rebuild_products(out: Outcome) -> None:
    builds = _builds()
    try:
        declared = builds.declared_builds()
    except Exception as exc:  # noqa: BLE001
        out.failures.append(f"product builds: {exc}")
        return
    for build in declared:
        if build.artifact.exists():
            continue
        if Path(build.build_dir).is_dir():
            shutil.rmtree(build.build_dir)
            out.removed.append(f"product build dir {build.build_dir} (artifact missing)")
        try:
            await builds.build_extension(build.build_dir, build.zver)
            out.removed.append(f"product {build.os_version} rebuilt into {build.build_dir}")
        except Exception as exc:  # noqa: BLE001
            out.failures.append(f"product {build.os_version}: {exc}")


# --- the reap -----------------------------------------------------------------


def _say(text: str) -> None:
    print(f"bed-reap: {text}", flush=True)


def _hold(cell: Cell, pid: int) -> "str | None":
    """Take or re-enter the lease and drain this run; the refusal, or None to proceed."""
    if bed_lease.acquire(cell, pid, PURPOSE) != 0:
        return "the bed is held by another live run; no reap"
    record = bed_lease.read_record(cell)
    if record is None:
        return "the lease vanished under the reap; no reap"
    current, left = bed_lease.drained_run(
        cell, record, seconds_from_env(DRAIN_ENV, DEFAULT_DRAIN_S)
    )
    if not bed_lease.same_run(record, current):
        return "the lease changed hands under the reap; no reap"
    if left:
        pids = " ".join(map(str, left))
        return f"this run still has {len(left)} process(es) running (pids {pids}); no reap"
    return None


def reap(cell: Cell, pid: int, *, full: bool = False, restart_qemu: bool = False) -> int:
    """Run the reap for *pid*'s run on *cell*; print what it did; the exit status."""
    refusal = _hold(cell, pid)
    if refusal is not None:
        _say(refusal)
        return EXIT_DIRTY
    lab = bed_lease.load_labs(cell.lab_file, cell.labs)
    out = Outcome()
    hosts = unix_bed_hosts(lab, cell.lab_file)

    async def _run() -> None:
        snapshots = await _reap_unix(lab, hosts, out)
        await _reap_docker(lab, hosts, out)
        await _rebuild_products(out)
        if full and await run_lab_health(restart_qemu=restart_qemu) != 0:
            out.failures.append("the console step reported an unhealthy bed")
        await _judge_unix(hosts, snapshots, out)

    asyncio.run(_run())
    for line in out.removed:
        _say(f"removed {line}")
    for line in out.unreachable:
        _say(f"unreachable {line}")
    for line in out.failures:
        _say(f"failed {line}")
    for host, leftovers in out.residual.items():
        if leftovers:
            print(_hygiene().format_residual_report(host, leftovers), flush=True)
    if out.clean:
        _say("clean")
        return 0
    return EXIT_DIRTY


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="the bed reap: clean the cell under its lease")
    add_cell_arguments(parser)
    add_run_arguments(parser, skip_help="reap nothing when every PATH after `--` is under DIR")
    parser.add_argument("--full", action="store_true", help="add the console step")
    parser.add_argument(
        "--restart-qemu", action="store_true", help="with --full: restart the guests"
    )
    return parser


def _check_flags(args: argparse.Namespace) -> None:
    if args.restart_qemu and not args.full:
        raise UsageError("--restart-qemu needs --full")


def main(argv: "list[str] | None" = None) -> int:
    """Parse the CLI and reap; 0 clean, 1 refused or dirty, 2 usage."""
    args = _parser().parse_args(argv)
    try:
        if classifier_says_skip(args):
            _say("no bed path in PATHS, no reap")
            return 0
        _check_flags(args)
        require_pid(args.pid)
        cell = bed_lease.load_cell(args.lab, root_from(args.root))
        return reap(cell, args.pid, full=args.full, restart_qemu=args.restart_qemu)
    except UsageError as exc:
        print(f"bed-reap: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
