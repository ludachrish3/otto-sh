"""The netcat contract: every implementation and strategy, on every netcat bed host.

The netcat transfer finds a free port and polls for its listener through two
ladders of strategies (``PORT_LADDER`` and ``LISTENER_LADDER`` in
``src/otto/host/transfer/nc_scripts.py``), and runs whichever netcat
``nc_options.exec_name`` names. Under ``auto`` a host only ever runs the first
strategy that is present, and only its default netcat, so the rest would never
meet a real host. Each cell here selects what it is about through
``nc_options`` and checks it against the committed table
(``tests/conformance/_nc_rungs.py``):

- AVAILABILITY, per host: ``available_rungs`` over the transfer backend's
  control runner names exactly the tools the table records. A tool that
  appeared or disappeared fails here, by name.
- IDENTITY, per implementation: ``readlink -f "$(command -v <exec_name>)"``
  prints the path the table records, so an ``nc`` that has become another
  netcat fails here, asking for the row to be measured again.
- Both of those also run inside every transfer cell before any put or get,
  so whatever order pytest runs the cells in, a drifted host fails naming
  what drifted rather than as a transfer failure.
- EACH PORT STRATEGY, per host, pinned: a listed one moves a file and is the
  strategy that ran; an unlisted one fails as that strategy's tool failure.
- ROUND TRIP, per implementation: three files (the transfer contract's
  tripwire payload, an empty file, one larger than a pipe buffer) put and got
  back byte for byte, and a ``put(mode=)`` read back with ``stat`` on the host.
- EACH LISTENER CHECK, per implementation, pinned: ``works`` moves a file
  and is the check that ran; ``tool-absent`` fails as the check's tool
  failure; ``does-not-work`` fails as its row records. For ``ncat`` the
  recorder also reads, while the listener is up, that the host lists the port
  on both address families (the socket shape #1304 was about).
- THE ORPHAN-LISTENER PROBE, per implementation: a listener planted with
  ``host.exec`` is the one line the bed's hygiene probe prints for it, and
  there is no such line before or after.
- A NETCAT THAT DOES NOT EXIST, per host: put and get fail naming it and the
  exit status the table records, and run it once when that status is 126 or
  127, twice (one retry) otherwise.

Which strategy ran is OBSERVED: ``run_port_rung`` and ``run_listener_rung``
are wrapped with call-through recorders. A direct get runs neither ladder (the
listener is local), so only a tunnelled host has get cells about a ladder.

NOT ``resolved_cell`` cells and not in the support matrix: the module
parametrizes its own arguments, and ``tests/conformance/conftest.py``'s
``_shared_cpu_family`` fixture reads them to take the shared-CPU lock the
selector (:func:`~tests.conformance._console_safety.shared_cpu_lock`) names,
outside the timed body, as it does for every other cell. It is marked
``conformance_bed`` so the hermetic ``make conformance`` lane never collects
it; ``make conformance-bed`` runs it.
"""

import contextlib
import dataclasses
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

import otto.host.transfer.nc as nc_module
from otto.host.host import BaseHost, exec_own
from otto.host.transfer.nc import available_rungs
from otto.host.transfer.nc_scripts import ListenerRung
from otto.result import Result, Status
from otto.utils import WaitTimeoutError, wait_for_async
from tests._fixtures.bed_hygiene import (
    _NC_LISTENER_PROBE,
    netcat_listeners,
    probe_lines,
)
from tests._fixtures.busybox import BUSYBOX_BANNER_COMMAND, reports_busybox_version
from tests._fixtures.netcat_names import NETCAT_EXEC_NAMES
from tests.conformance._bed import BedCell, bed_scratch_dir, nc_bed_cell
from tests.conformance._nc_rungs import (
    BED_NETCAT,
    BUSYBOX_GUEST_VERSIONS,
    DOES_NOT_WORK,
    LADDERS,
    LISTENER,
    NC_HOST_CELLS,
    NC_IMPLEMENTATION_CELLS,
    NC_LISTENER_CELLS,
    NC_PORT_CELLS,
    PIN_OPTION,
    PORT,
    TOOL_ABSENT,
    BedNetcat,
    ListenerCheck,
    NcImplementation,
    assert_failed_as_recorded,
    assert_failed_as_the_pin,
    available_rungs_of,
    implementation,
    nc_host_id,
    nc_implementation_id,
    nc_listener_id,
    nc_port_id,
    working_checks,
)
from tests.conformance._roundtrip import MODE, PAYLOAD, assert_mode_landed, assert_roundtrip
from tests.conformance._transfer import transfer_backend_of
from tests.conftest import remote_name

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.conformance,
    pytest.mark.conformance_bed,
]

_PAYLOAD = "otto-nc-rung"

# Larger than a Linux pipe buffer (64 KiB) several times over, so a transfer
# that only works while the whole file fits in one buffer fails here.
_LARGER_THAN_A_PIPE = 200_000
_LARGE_PAYLOAD = bytes(i % 251 for i in range(_LARGER_THAN_A_PIPE))

# A name no bed host has a program for: the missing-binary cell's exec_name.
_MISSING_NETCAT = "otto-no-such-netcat"

# The exit statuses a retry would meet again; otto runs the listener once for
# these (NcListenerExitedError in src/otto/host/transfer/nc.py) and twice for
# any other status. It restates the product's `_NC_STATUSES_A_RETRY_MEETS_AGAIN`
# on purpose, as an independent oracle: a cell that imported the product's set
# could not catch that set being wrong.
_UNRETRIED_STATUSES = frozenset({126, 127})

# How long the orphan-probe cell waits for the listener it killed to leave the
# probe's output. It bounds the time between `kill` and the process being gone
# from /proc, which is immediate on an idle host; the bound is for an emulated
# guest under load.
_KILLED_LISTENER_GONE_WITHIN_S = 10.0

# The base port the orphan-probe cell plants its listener from, through the
# backend's own finder. A transfer scans upward from a start within the
# product's `_NC_PORT_SCAN_SPREAD` of its base, 9000 by default on every bed
# host, and takes the first free port, so it reaches 20000 only when every
# port from its start to 19999 is in use; the `python` port strategy takes a
# kernel ephemeral port (32768 and up on Linux). This cell's window, from 20000
# up through its own spread and scan, is therefore disjoint from every transfer's,
# and only one probe cell runs per host (the guests' are serialized by lock).
_PROBE_PORT_BASE = 20000


def _options(
    element: str, exec_name: str, ladder: str | None = None, rung: str | None = None
) -> dict[str, dict[str, object]]:
    """``nc_options`` selecting *exec_name*, *ladder* pinned to *rung*, the other to its first.

    The companion ladder is pinned to the first port strategy the host lists,
    or the first listener check that works with *exec_name*, so a broken
    companion cannot fail a cell about the other ladder. With no *ladder*
    both are pinned to those firsts.
    """
    firsts = {
        PORT: BED_NETCAT[element].port[0],
        LISTENER: _first_check(element, exec_name).name,
    }
    pins: dict[str, object] = {PIN_OPTION[name]: first for name, first in firsts.items()}
    if ladder is not None:
        pins[PIN_OPTION[ladder]] = rung
    pins["exec_name"] = exec_name
    return {"nc_options": pins}


Witness = Callable[[Callable[[str], Awaitable[object]], int], Awaitable[None]]


@contextlib.contextmanager
def recording_rungs(witness: Witness | None = None) -> Iterator[dict[str, list[str]]]:
    """Record the name of every port and listener rung that runs, calling through.

    Patched where the backend looks the two functions up (the module globals of
    ``otto.host.transfer.nc``) and restored when the block ends. *witness*, if
    given, is awaited with the check's runner and port after a listener check
    has counted at least one listener, while that listener is up; the recorder
    also runs on polls that count none, and those do not call it.
    """
    record: dict[str, list[str]] = {PORT: [], LISTENER: []}
    real_port = nc_module.run_port_rung
    real_listener = nc_module.run_listener_rung

    async def port(rung, run, *, base_port, reserved):
        record[PORT].append(rung.name)
        return await real_port(rung, run, base_port=base_port, reserved=reserved)

    async def listener(rung, run, port_number):
        record[LISTENER].append(rung.name)
        count = await real_listener(rung, run, port_number)
        if witness is not None and count is not None and count >= 1:
            await witness(run, port_number)
        return count

    nc_module.run_port_rung = port
    nc_module.run_listener_rung = listener
    try:
        yield record
    finally:
        nc_module.run_port_rung = real_port
        nc_module.run_listener_rung = real_listener


_FAMILIES = ("-4", "-6")


def _socket_shape_witness() -> tuple[Witness, dict[str, list[str]]]:
    """A witness that reads ``ss -tln <family> sport = :PORT`` for both families.

    Returns the witness and what it read, per family, one entry per call.
    """
    seen: dict[str, list[str]] = {family: [] for family in _FAMILIES}

    async def witness(run, port_number: int) -> None:
        for family in _FAMILIES:
            answer = await run(f"ss -tln {family} sport = :{port_number}")
            seen[family].append(f"{port_number}\n{getattr(answer, 'value', '')}")

    return witness, seen


def assert_listed_on_both_families(seen: dict[str, list[str]], label: str) -> None:
    """The witness ran, and each family's ``ss`` listed the listener's port in a data row."""
    for family in _FAMILIES:
        reads = seen[family]
        assert reads, (
            f"{label}: the socket-shape witness never ran for {family}: no listener check "
            f"counted the listener, so the shape #1304 was about was never read"
        )
        listed = False
        for read in reads:
            port_number, _, text = read.partition("\n")
            rows = [line for line in text.splitlines()[1:] if f":{port_number}" in line]
            listed = listed or bool(rows)
        assert listed, f"{label}: `ss -tln {family}` listed no row for the port: {reads!r}"


def assert_availability(table: dict[str, BedNetcat], element: str, available: frozenset[str]):
    """The host's available rungs are exactly the tools the table records for *element*."""
    listed = available_rungs_of(table, element)
    assert available == listed, (
        f"{element}: the availability probe names rungs {sorted(available)} but the table "
        f"lists {sorted(listed)}; appeared: {sorted(available - listed)}, "
        f"disappeared: {sorted(listed - available)}. Re-measure and update BED_NETCAT "
        f"in tests/conformance/_nc_rungs.py"
    )


def assert_identity(impl: NcImplementation, element: str, resolved: str) -> None:
    """*impl*'s ``exec_name`` resolves on the host to the path the table records."""
    assert resolved == impl.resolves_to, (
        f"{element}: '{impl.exec_name}' resolves to {resolved!r}, but the table records "
        f"{impl.resolves_to!r} ({impl.package}). The netcat it selects has changed: measure "
        f"the row again and update BED_NETCAT in tests/conformance/_nc_rungs.py"
    )


def assert_ran_only(record: dict[str, list[str]], label: str, ladder: str, rung: str, op: str):
    """The *ladder* rungs that ran during *op* are *rung*, at least once, and no other."""
    ran = record[ladder]
    assert set(ran or [None]) == {rung}, (
        f"{label}: {op} pinned the {ladder} ladder to '{rung}' but the rungs that ran were {ran!r}"
    )


async def _read_remote(host: BaseHost, path: Path) -> str:
    answered = await exec_own(host, f"cat {path}")
    return answered.value if answered.retcode == 0 else f"<cat exited {answered.retcode}>"


async def _resolved_path(host: BaseHost, exec_name: str) -> str:
    answered = await exec_own(host, f'readlink -f "$(command -v {exec_name})"')
    return answered.value if answered.retcode == 0 else f"<exit {answered.retcode}>"


async def _available_rungs(host: BaseHost, bed_cell: BedCell) -> frozenset[str]:
    """``available_rungs`` over *host*'s netcat backend's control runner."""
    backend = transfer_backend_of(
        host, bed_cell.cell, refusal_tail="its netcat rungs cannot be asked for"
    )
    return await available_rungs(backend._control_run)


async def _guard(host: BaseHost, bed_cell: BedCell, element: str, exec_name: str) -> None:
    """Availability and identity, asserted before any transfer of a cell."""
    assert_availability(BED_NETCAT, element, await _available_rungs(host, bed_cell))
    impl = implementation(element, exec_name)
    assert_identity(impl, element, await _resolved_path(host, exec_name))


def _scratch(element: str, label: str) -> Path:
    scratch = bed_scratch_dir(element)
    assert scratch is not None, f"{label}: the bed gives {element} nowhere to put a file"
    return scratch


async def _pinned_transfer(
    host: BaseHost,
    label: str,
    ladder: str,
    rung: str,
    expect: ListenerCheck | None,
    source: Path,
    scratch: Path,
    retrieved_dir: Path,
    witness: tuple[Witness, dict[str, list[str]]] | None = None,
) -> None:
    """Put (and, through a hop, get) with *ladder* pinned to *rung*, judged by *expect*.

    *expect* is None for a listed port strategy and for a working listener
    check: the file moves and *rung* is what ran. A ``tool-absent`` state, or
    an unlisted port strategy (*expect* with ``tool-absent``), fails as the
    pin's tool failure; ``does-not-work`` fails as the row records.
    """
    remote = scratch / source.name
    tunnelled = host.hop is not None
    hook = witness[0] if witness is not None else None

    def judge(result, record, op: str) -> None:
        if expect is None:
            assert result.is_ok, f"{label}: {op} reported {result.status!r} -- {result.msg!r}"
            assert_ran_only(record, label, ladder, rung, op)
        elif expect.state == DOES_NOT_WORK:
            assert_failed_as_recorded(result, record[ladder], expect, label, rung, op)
        else:
            assert_failed_as_the_pin(result, record[ladder], label, ladder, rung, op)

    with recording_rungs(hook) as record:
        put = await host.put(source, scratch)
    judge(put, record, "put")
    if expect is None:
        landed = await _read_remote(host, remote)
        assert landed == _PAYLOAD, f"{label}: put landed {landed!r}"
    if not tunnelled:
        return
    if expect is not None:
        seeded = await host.exec(f"printf %s {_PAYLOAD} > {remote}")
        assert seeded.retcode == 0, f"{label}: seeding {remote} failed: {seeded}"
    with recording_rungs(hook) as record:
        got = await host.get(remote, retrieved_dir)
    judge(got, record, "get")
    if expect is None:
        back = (retrieved_dir / source.name).read_text()
        assert back == _PAYLOAD, f"{label}: get brought back {back!r}"


def _local_files(tmp_path: Path, name: str) -> tuple[Path, Path]:
    source = tmp_path / "source" / name
    source.parent.mkdir()
    source.write_text(_PAYLOAD)
    retrieved_dir = tmp_path / "retrieved"
    retrieved_dir.mkdir()
    return source, retrieved_dir


# --- availability and identity ------------------------------------------------------


@pytest.mark.parametrize("element", NC_HOST_CELLS, ids=nc_host_id)
async def test_the_host_has_exactly_the_tools_the_table_records(element: str) -> None:
    """``available_rungs`` names exactly the port strategies and listener tools of the row."""
    bed_cell = nc_bed_cell(element)
    first = BED_NETCAT[element].implementations[0].exec_name
    async with bed_cell.open_host_with(_options(element, first)) as host:
        available = await _available_rungs(host, bed_cell)
    assert_availability(BED_NETCAT, element, available)


@pytest.mark.parametrize("nc_impl", NC_IMPLEMENTATION_CELLS, ids=nc_implementation_id)
async def test_each_netcat_name_resolves_to_the_recorded_implementation(
    nc_impl: tuple[str, str],
) -> None:
    """``readlink -f "$(command -v <exec_name>)"`` prints the path the table records.

    On a BusyBox guest the applet's own banner must also report the version
    the image builder pins for it, which is the version the table shows.
    """
    element, exec_name = nc_impl
    bed_cell = nc_bed_cell(element)
    version = BUSYBOX_GUEST_VERSIONS.get(element)
    async with bed_cell.open_host_with(_options(element, exec_name)) as host:
        resolved = await _resolved_path(host, exec_name)
        banner = (await exec_own(host, BUSYBOX_BANNER_COMMAND)).value if version else ""
    assert_identity(implementation(element, exec_name), element, resolved)
    if version is not None:
        assert reports_busybox_version(banner, version), (
            f"{element}: `busybox` answered {banner!r}, not the BusyBox v{version} "
            f"the image builder pins for it"
        )


# --- each strategy, pinned --------------------------------------------------------


@pytest.mark.parametrize("nc_port", NC_PORT_CELLS, ids=nc_port_id)
async def test_each_port_strategy_runs_where_the_table_lists_it(
    nc_port: tuple[str, str], tmp_path: Path, worker_id: str
) -> None:
    """A listed port strategy moves a file and is the one that ran; an unlisted one fails, named."""
    element, rung = nc_port
    label = f"nc-port[{nc_port_id(nc_port)}]"
    exec_name = BED_NETCAT[element].implementations[0].exec_name
    bed_cell = nc_bed_cell(element)
    scratch = _scratch(element, label)
    source, retrieved_dir = _local_files(tmp_path, remote_name(worker_id, f"ncport_{rung}"))
    listed = rung in BED_NETCAT[element].port
    expect = None if listed else ListenerCheck(state=TOOL_ABSENT)

    async with bed_cell.open_host_with(_options(element, exec_name, PORT, rung)) as host:
        await _guard(host, bed_cell, element, exec_name)
        try:
            await _pinned_transfer(host, label, PORT, rung, expect, source, scratch, retrieved_dir)
        finally:
            await host.exec(f"rm -f {scratch / source.name}")


@pytest.mark.parametrize("nc_listener", NC_LISTENER_CELLS, ids=nc_listener_id)
async def test_each_listener_check_meets_each_implementation_as_the_table_records(
    nc_listener: tuple[str, str, str], tmp_path: Path, worker_id: str
) -> None:
    """``works`` moves a file and is the check that ran; the other states fail as recorded.

    For ``ncat`` the recorder also reads, once a check has counted the
    listener, that ``ss`` lists the port on both address families.
    """
    element, exec_name, rung = nc_listener
    label = f"nc-listener[{nc_listener_id(nc_listener)}]"
    check = implementation(element, exec_name).listener[rung]
    expect = None if check.state not in (TOOL_ABSENT, DOES_NOT_WORK) else check
    bed_cell = nc_bed_cell(element)
    scratch = _scratch(element, label)
    tag = f"nclsn_{exec_name}_{rung}"
    source, retrieved_dir = _local_files(tmp_path, remote_name(worker_id, tag))
    witness = _socket_shape_witness() if exec_name == "ncat" and expect is None else None

    async with bed_cell.open_host_with(_options(element, exec_name, LISTENER, rung)) as host:
        await _guard(host, bed_cell, element, exec_name)
        try:
            await _pinned_transfer(
                host, label, LISTENER, rung, expect, source, scratch, retrieved_dir, witness
            )
        finally:
            await host.exec(f"rm -f {scratch / source.name}")
    if witness is not None:
        assert_listed_on_both_families(witness[1], label)


# --- round trip per implementation ----------------------------------------------------


@pytest.mark.parametrize("nc_impl", NC_IMPLEMENTATION_CELLS, ids=nc_implementation_id)
async def test_each_implementation_round_trips_files_and_lands_the_mode(
    nc_impl: tuple[str, str], tmp_path: Path, worker_id: str
) -> None:
    """The tripwire payload, an empty file and one larger than a pipe come back byte for byte."""
    element, exec_name = nc_impl
    label = f"nc-roundtrip[{nc_implementation_id(nc_impl)}]"
    bed_cell = nc_bed_cell(element)
    scratch = _scratch(element, label)
    files = {
        remote_name(worker_id, f"ncrt_{exec_name}_tripwire"): PAYLOAD,
        remote_name(worker_id, f"ncrt_{exec_name}_empty"): b"",
        remote_name(worker_id, f"ncrt_{exec_name}_large"): _LARGE_PAYLOAD,
    }
    moded = remote_name(worker_id, f"ncrt_{exec_name}_mode")
    mode_source = tmp_path / "mode" / moded
    mode_source.parent.mkdir()
    mode_source.write_bytes(PAYLOAD)

    async with bed_cell.open_host_with(_options(element, exec_name)) as host:
        await _guard(host, bed_cell, element, exec_name)
        try:
            for name, payload in files.items():
                await assert_roundtrip(
                    host, scratch, tmp_path, name=name, payload=payload, label=label
                )
            put = await host.put(mode_source, scratch, mode=MODE)
            await assert_mode_landed(host, put, scratch / moded, label=label)
        finally:
            for name in (*files, moded):
                await host.exec(f"rm -f {scratch / name}")


# --- the orphan-listener probe -------------------------------------------------------


def _lines_for_port(lines: list[str], port: int) -> list[str]:
    return [line for line in lines if re.search(rf" -l -p {port}( |$)", line)]


@pytest.mark.parametrize("nc_impl", NC_IMPLEMENTATION_CELLS, ids=nc_implementation_id)
async def test_the_orphan_probe_sees_a_listener_of_each_implementation(
    nc_impl: tuple[str, str],
) -> None:
    """A planted ``<exec_name> -l -p PORT`` is exactly one probe line, absent before and after.

    The bed hygiene check relies on this probe to find a listener a test left
    behind; on BusyBox 1.16.1 and 1.21.1 it once printed nothing for one
    (#1320), and only a run on those hosts shows it no longer does. The
    product's own first working listener check must also count the planted
    listener once, so the probe is seen to report a live, bound listener.

    It relies on these private seams of the netcat backend, each the
    product's own decision read rather than restated here: ``prepare()``,
    which resolves the host's userland; ``_nc_listener_prefix``, the lifetime
    cap the product puts on every listener it spawns (the planted listener
    carries it, so a kill that misses it still leaves it finite);
    ``_control_run``, through which the product's listener check counts the
    planted listener; ``_find_free_port`` and ``_release_port``, which choose
    and give back the port; and ``nc_module._NC_LISTENER_READY_TIMEOUT``,
    the bound on that count. Cleanup kills the pid captured from ``$!`` as
    well as whatever the probe reports, so a probe that misses the listener
    does not leave it running.
    """
    element, exec_name = nc_impl
    label = f"nc-probe[{nc_implementation_id(nc_impl)}]"
    assert exec_name in NETCAT_EXEC_NAMES, f"{label}: the probe does not look for {exec_name!r}"
    bed_cell = nc_bed_cell(element)

    options = _options(element, exec_name)
    options["nc_options"]["port"] = _PROBE_PORT_BASE

    async with bed_cell.open_host_with(options) as host:
        backend = transfer_backend_of(
            host, bed_cell.cell, refusal_tail="it cannot be asked for a free port"
        )
        # Resolves the userland, which the lifetime-cap prefix is read from.
        await backend.prepare()
        cap = backend._nc_listener_prefix
        assert cap, f"{label}: no lifetime cap for a planted listener on this host"
        port = await backend._find_free_port()
        pid = ""
        try:
            before = _lines_for_port(await netcat_listeners(host, label), port)
            assert before == [], f"{label}: a listener on port {port} before planting: {before}"
            # The form measured on every netcat bed host, BusyBox telnet
            # sessions included: the pid is the last non-empty line. The
            # listener carries the product's own lifetime cap, so a kill
            # that misses it still leaves it finite.
            planted = await exec_own(
                host,
                f"sh -c '{cap}{exec_name} -l -p {port} < /dev/null > /dev/null 2>&1 & echo $!'",
            )
            lines = [line for line in planted.value.splitlines() if line]
            pid = lines[-1] if lines else ""
            assert planted.retcode == 0, f"{label}: planting the listener failed: {planted}"
            assert pid.isdigit(), f"{label}: planting the listener gave no pid: {planted}"
            count = await _live_listener_count(backend, element, exec_name, port)
            assert count == 1, (
                f"{label}: the listener check '{_first_check(element, exec_name).name}' "
                f"counted {count} listener(s) on port {port}, not the one planted"
            )
            seen = _lines_for_port(await netcat_listeners(host, label), port)
            # Which process carries which argv depends on the host's
            # `timeout` (GNU forks the program and stays; newer BusyBox
            # execs it in place and leaves a watchdog), so no arrangement
            # is assumed: one line is the netcat's exact argv, at most one
            # is the capped command's, `$!` is among them, nothing else.
            bare = f"{exec_name} -l -p {port}"
            capped = " ".join(f"{cap}{bare}".split())
            argvs = [line.split(" ", 1)[1] if " " in line else "" for line in seen]
            pids = [line.split(" ", 1)[0] for line in seen]
            arrangement = f"{label}: the probe printed {seen!r} for port {port} (pid {pid})"
            assert argvs.count(bare) == 1, arrangement
            assert argvs.count(capped) <= 1, arrangement
            assert set(argvs) <= {bare, capped}, arrangement
            assert pid in pids, arrangement
        finally:
            left = await _kill_port_listeners(host, port, pid if pid.isdigit() else "")
            backend._release_port(port)
        assert left == [], f"{label}: the killed listener on port {port} is still there: {left}"


def _first_check(element: str, exec_name: str) -> ListenerRung:
    """The first listener check the table says works with *exec_name* on *element*.

    The one choice of it, for :func:`_options` and the probe cell; the rung is
    the product's own (``_LISTENER_RUNGS``), not a second lookup of the ladder.
    """
    return nc_module._LISTENER_RUNGS[working_checks(implementation(element, exec_name))[0]]


async def _live_listener_count(backend, element: str, exec_name: str, port: int) -> int | None:
    """How many listeners the product's own listener check counts on *port*, once it sees one.

    Polled within the product's readiness bound, since a listener just
    spawned may not have bound yet; the last count read is returned.
    """
    rung = _first_check(element, exec_name)
    counted: list[int | None] = [None]

    async def bound() -> bool:
        counted[0] = await nc_module.run_listener_rung(rung, backend._control_run, port)
        return counted[0] is not None and counted[0] >= 1

    with contextlib.suppress(WaitTimeoutError):
        await wait_for_async(
            bound,
            nc_module._NC_LISTENER_READY_TIMEOUT,
            interval=0.1,
            on_timeout=f"no listener counted on port {port}",
        )
    return counted[0]


async def _kill_port_listeners(host: BaseHost, port: int, planted: str) -> list[str]:
    """Kill the *planted* pid and every listener the probe reports on *port*; return what is left.

    The captured pid (empty when none was read) is killed whatever the probe
    says, so a probe that misses the listener (the failure the probe cell
    exists to catch) still leaves nothing behind, and it is gone only when
    ``kill -0`` fails for it AND the probe reports no line for the port.
    Cleanup: never raises for what it finds, so a failure already on its way
    out is not replaced. The wait is bounded by
    :data:`_KILLED_LISTENER_GONE_WITHIN_S`, and after its last kill it always
    looks once more before reporting what is left.
    """
    left: list[str] = []

    async def look() -> list[str]:
        answered = await exec_own(host, _NC_LISTENER_PROBE)
        if answered.retcode != 0:
            # A failed probe is never "nothing left": its value is the tool's text.
            return [f"<probe exited {answered.retcode}: {answered.value!r}>"]
        found = _lines_for_port(probe_lines(answered.value), port)
        if planted and (await host.exec(f"kill -0 {planted} 2>/dev/null")).retcode == 0:
            found = [*found, f"<pid {planted} is still alive>"]
        return found

    async def gone() -> bool:
        left[:] = await look()
        pids = {line.split(" ", 1)[0] for line in left if not line.startswith("<")}
        if planted:
            pids.add(planted)
        if left:
            await host.exec(f"kill {' '.join(sorted(pids))} 2>/dev/null; true")
        return not left

    with contextlib.suppress(WaitTimeoutError):
        await wait_for_async(
            gone,
            _KILLED_LISTENER_GONE_WITHIN_S,
            interval=0.5,
            on_timeout=f"listeners on port {port} outlived the kill",
        )
    if left:
        # The poll that sent the last kill may have used the whole bound.
        left[:] = await look()
    return left


# --- a netcat that does not exist ----------------------------------------------------


@contextlib.contextmanager
def recording_netcat_commands(backend, exec_name: str) -> Iterator[dict[str, list[str]]]:
    """Record every remote listener and every direct-get sender *backend* spawns for *exec_name*.

    Wraps the backend's two command runners, calling through, and restores
    them when the block ends: ``_spawn_cmd``, the host's spawn through which a
    listener is started where the host has one, and ``_exec_cmd``, through
    which the direct get's sender and the control commands go, and a listener
    too on a host without a spawn. A listener is ``<exec_name> -l -p PORT``,
    after any lifetime-cap prefix; a sender is
    ``<exec_name> <ip> <port> < <file>``.
    """
    record: dict[str, list[str]] = {"listener": [], "sender": []}
    name = re.escape(exec_name)
    is_listener = re.compile(rf"(^|\s){name} -l -p \d+ ")
    is_sender = re.compile(rf"^{name} \S+ \d+ < ")
    real_exec, real_spawn = backend._exec_cmd, backend._spawn_cmd

    def note(cmd: str) -> None:
        if is_listener.search(cmd):
            record["listener"].append(cmd)
        elif is_sender.match(cmd):
            record["sender"].append(cmd)

    async def exec_recorder(cmd, *args, **kwargs):
        note(cmd)
        return await real_exec(cmd, *args, **kwargs)

    async def spawn_recorder(cmd, *args, **kwargs):
        note(cmd)
        return await real_spawn(cmd, *args, **kwargs)

    backend._exec_cmd = exec_recorder
    if real_spawn is not None:
        backend._spawn_cmd = spawn_recorder
    try:
        yield record
    finally:
        backend._exec_cmd, backend._spawn_cmd = real_exec, real_spawn


def assert_missing_netcat_reported(result, status: int, label: str, op: str) -> None:
    """*result* failed with *status*, worded as the status means.

    126 and 127 name the missing netcat and ``nc_options.exec_name``; any other
    status is the whole command line's, so it names the command instead.
    """
    assert not result.is_ok, f"{label}: {op} with a netcat that does not exist reported success"
    the_netcat = status in _UNRETRIED_STATUSES
    words = (
        f"'{_MISSING_NETCAT}' exited with status {status}"
        if the_netcat
        else f"command exited with status {status}"
    )
    assert words in result.msg, f"{label}: {op} did not report {words!r}: {result.msg!r}"
    assert ("nc_options.exec_name" in result.msg) == the_netcat, (
        f"{label}: {op} with status {status} points at nc_options.exec_name "
        f"{'only' if the_netcat else 'wrongly'}: {result.msg!r}"
    )
    not_found = "not found on the host"
    assert (not_found in result.msg) == (status == 127), (
        f"{label}: {op} with status {status} {'lacks' if status == 127 else 'has'} "
        f"{not_found!r}: {result.msg!r}"
    )


def _expected_runs(status: int) -> int:
    return 1 if status in _UNRETRIED_STATUSES else 2


_RETRIED = "(two attempts were made)"


def assert_retried_as_measured(result, runs: int, label: str, op: str) -> None:
    """The product's own retry wording agrees with the number of runs expected."""
    assert (_RETRIED in result.msg) == (runs == 2), (
        f"{label}: {op} expected {runs} run(s) of the missing netcat: {result.msg!r}"
    )


@pytest.mark.parametrize("element", NC_HOST_CELLS, ids=nc_host_id)
async def test_a_netcat_that_does_not_exist_fails_put_and_get_with_its_status(
    element: str, tmp_path: Path, worker_id: str
) -> None:
    """Put and get name the missing netcat and its status, and run it as often as measured."""
    row = BED_NETCAT[element]
    status = row.missing_binary_status
    label = f"nc-missing[{element}]"
    bed_cell = nc_bed_cell(element)
    scratch = _scratch(element, label)
    source, retrieved_dir = _local_files(tmp_path, remote_name(worker_id, "ncmissing_put"))
    known = scratch / remote_name(worker_id, "ncmissing_get")
    runs = _expected_runs(status)

    async with bed_cell.open_host_with({"nc_options": {"exec_name": _MISSING_NETCAT}}) as host:
        backend = transfer_backend_of(
            host, bed_cell.cell, refusal_tail="the netcat it spawns cannot be recorded"
        )
        try:
            with recording_netcat_commands(backend, _MISSING_NETCAT) as spawned:
                put = await host.put(source, scratch)
            assert_missing_netcat_reported(put, status, label, "put")
            assert_retried_as_measured(put, runs, label, "put")
            assert (len(spawned["listener"]), spawned["sender"]) == (runs, []), (
                f"{label}: put ran the missing netcat {spawned!r}, expected {runs} listener(s)"
            )

            seeded = await host.exec(f"printf %s {_PAYLOAD} > {known} && test -s {known}")
            assert seeded.retcode == 0, f"{label}: the get's source {known} is not there"
            with recording_netcat_commands(backend, _MISSING_NETCAT) as spawned:
                got = await host.get(known, retrieved_dir)
            assert_missing_netcat_reported(got, status, label, "get")
            assert_retried_as_measured(got, runs if row.route == "tunnelled" else 1, label, "get")
            # A direct get spawns one sender and is never retried.
            expected = (runs, 0) if row.route == "tunnelled" else (0, 1)
            assert (len(spawned["listener"]), len(spawned["sender"])) == expected, (
                f"{label}: get ran the missing netcat {spawned!r}, expected "
                f"{expected[0]} listener(s) and {expected[1]} sender(s)"
            )
        finally:
            await host.exec(f"rm -f {scratch / source.name} {known}")


# --- positive controls: each assertion above can go red --------------------------

_CONTROL_ELEMENTS = ["test2", "bb1350"]


@pytest.mark.parametrize("element", _CONTROL_ELEMENTS)
async def test_control_the_availability_check_fires_on_a_wrong_table(element: str) -> None:
    """A table missing a rung the host has, or listing one it lacks, fails the check."""
    bed_cell = nc_bed_cell(element)
    first = BED_NETCAT[element].implementations[0].exec_name
    async with bed_cell.open_host_with(_options(element, first)) as host:
        available = await _available_rungs(host, bed_cell)
    assert_availability(BED_NETCAT, element, available)

    row = BED_NETCAT[element]
    # The first port strategy is also a listener tool on both hosts, so it is
    # dropped from both, as a host that lost the tool would be.
    gone = row.port[0]
    without = tuple(
        NcImplementation(
            impl.exec_name,
            impl.resolves_to,
            impl.package,
            {r: ListenerCheck(TOOL_ABSENT) if r == gone else c for r, c in impl.listener.items()},
        )
        for impl in row.implementations
    )
    removed = {
        **BED_NETCAT,
        element: dataclasses.replace(
            row, port=tuple(r for r in row.port if r != gone), implementations=without
        ),
    }
    with pytest.raises(AssertionError, match=f"appeared: \\['{gone}'\\]"):
        assert_availability(removed, element, available)

    missing = [r for r in LADDERS[PORT] if r not in available_rungs_of(BED_NETCAT, element)]
    # the GNU hosts have every rung, so the name added there is one no ladder holds
    unlisted = missing[0] if missing else "invented"
    added = {**BED_NETCAT, element: dataclasses.replace(row, port=(*row.port, unlisted))}
    with pytest.raises(AssertionError, match=f"disappeared: \\['{unlisted}'\\]"):
        assert_availability(added, element, available)


async def test_control_the_identity_check_fires_on_a_wrong_path() -> None:
    """A row recording another path than the host prints fails the identity check."""
    element = "test2"
    bed_cell = nc_bed_cell(element)
    impl = BED_NETCAT[element].implementations[0]
    async with bed_cell.open_host_with(_options(element, impl.exec_name)) as host:
        resolved = await _resolved_path(host, impl.exec_name)
    assert_identity(impl, element, resolved)
    wrong = NcImplementation(impl.exec_name, "/usr/bin/nc.traditional", impl.package, impl.listener)
    with pytest.raises(AssertionError, match="measure the row again"):
        assert_identity(wrong, element, resolved)


async def test_control_the_witness_fires_on_a_wrong_rung(tmp_path: Path, worker_id: str) -> None:
    """A recorder that writes down the wrong rung fails the which-rung-ran check."""
    element, ladder, rung, wrong = "test2", PORT, "netstat", "ss"
    label = f"control[{element}:{ladder}:{rung}]"
    bed_cell = nc_bed_cell(element)
    scratch = _scratch(element, label)
    name = remote_name(worker_id, "ncrung_control")
    source = tmp_path / name
    source.write_text(_PAYLOAD)
    exec_name = BED_NETCAT[element].implementations[0].exec_name
    async with bed_cell.open_host_with(_options(element, exec_name, ladder, rung)) as host:
        try:
            with recording_rungs() as record:
                put = await host.put(source, scratch)
        finally:
            await host.exec(f"rm -f {scratch / name}")
    assert put.is_ok, f"{label}: put reported {put.status!r} -- {put.msg!r}"
    assert_ran_only(record, label, ladder, rung, "put")
    stubbed = {**record, ladder: [wrong if r == rung else r for r in record[ladder]]}
    with pytest.raises(AssertionError, match="but the rungs that ran were"):
        assert_ran_only(stubbed, label, ladder, rung, "put")
    with pytest.raises(AssertionError, match="but the rungs that ran were"):
        assert_ran_only({**record, ladder: []}, label, ladder, rung, "put")


async def test_control_the_socket_shape_check_fires_on_one_family_or_none() -> None:
    """A witness that never ran, or that read a row on one family only, fails the shape check."""
    header = "State Recv-Q Send-Q Local Address:Port Peer Address:PortProcess"
    both = {
        "-4": [f"9151\n{header}\nLISTEN 0 1 0.0.0.0:9151 0.0.0.0:*"],
        "-6": [f"9151\n{header}\nLISTEN 0 1 [::]:9151 [::]:*"],
    }
    assert_listed_on_both_families(both, "control")
    with pytest.raises(AssertionError, match="listed no row"):
        assert_listed_on_both_families({**both, "-6": [f"9151\n{header}"]}, "control")
    with pytest.raises(AssertionError, match="never ran"):
        assert_listed_on_both_families({"-4": [], "-6": []}, "control")


async def test_control_the_missing_netcat_check_fires_on_the_wrong_report() -> None:
    """A success, another status, or the wrong wording for the status fails the check."""
    ok = Result(status=Status.Success, msg="")
    tail = " (nc_options.exec_name names the netcat to run)"
    found = f"'{_MISSING_NETCAT}' exited with status 127, so it was not found on the host{tail}"
    one = "the listener command exited with status 1"
    assert_missing_netcat_reported(Result(status=Status.Error, msg=found), 127, "control", "put")
    assert_missing_netcat_reported(Result(status=Status.Error, msg=one), 1, "control", "put")
    blamed = f"'{_MISSING_NETCAT}' exited with status 1{tail}"
    for result, status in (
        (ok, 127),
        (Result(status=Status.Error, msg=one), 127),
        (Result(status=Status.Error, msg=blamed), 1),
    ):
        with pytest.raises(AssertionError):
            assert_missing_netcat_reported(result, status, "control", "put")
    bare = f"'{_MISSING_NETCAT}' exited with status 127{tail}"
    with pytest.raises(AssertionError, match="lacks"):
        assert_missing_netcat_reported(Result(status=Status.Error, msg=bare), 127, "control", "put")


async def test_control_the_does_not_work_check_fires_on_a_pass_or_another_failure() -> None:
    """A pass, or a failure without the row's opening, fails the recorded-state check."""
    check = ListenerCheck(
        state=DOES_NOT_WORK,
        reason="the check counts one socket per address family",
        opens_with="Remote port",
        issue="#1304",
    )
    shared = Result(status=Status.Error, msg="nc put to /tmp/f: Remote port 9001 is shared")
    assert_failed_as_recorded(shared, ["ss"], check, "control", "ss", "put")
    other = Result(status=Status.Error, msg="nc put to /tmp/f: remote readiness: not ready")
    for result in (Result(status=Status.Success, msg=""), other):
        with pytest.raises(AssertionError):
            assert_failed_as_recorded(result, ["ss"], check, "control", "ss", "put")
    with pytest.raises(AssertionError, match="but the rungs that ran were"):
        assert_failed_as_recorded(shared, [], check, "control", "ss", "put")
