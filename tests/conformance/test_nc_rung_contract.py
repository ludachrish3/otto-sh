"""Every netcat rung, pinned, on every bed host that has an ``nc`` cell.

The netcat transfer finds a free port and polls for its listener through two
ladders of strategies (``PORT_LADDER`` and ``LISTENER_LADDER`` in
``src/otto/host/transfer/nc_scripts.py``). Under ``auto`` a host only ever
runs the first rung that is present, so the later rungs would never meet a
real host. Each cell here pins one rung through ``nc_options`` and checks it
against the committed table of which rungs each host has
(``tests/conformance/_nc_rungs.py``):

- AVAILABILITY first and on its own: ``available_rungs`` over the transfer
  backend's control runner must name exactly the rungs the table lists for the
  host. A tool that appeared or disappeared fails here, by name, before any
  transfer.
- A LISTED rung moves a file: put lands its content on the host, and on a
  tunnelled host (the BusyBox guests behind ``test1``) get brings it back.
  Which rung ran is OBSERVED: ``run_port_rung`` and ``run_listener_rung`` are
  wrapped with call-through recorders for each operation. A pinned scan
  leaves ``_resolved_port_strategy`` unset, so that cache is no witness.
- An UNLISTED rung fails as a result that proves the pinned rung ran (the
  recorder saw it, and no other) and that its tool failed: the text opens as a
  pinned rung's tool failure (``PINNED_TOOL_FAILED``), never as a readiness
  timeout, and names the option that pins another. A direct get runs neither
  ladder (the listener is local), so only a tunnelled host has a get cell to
  check.

The companion ladder is pinned to the first rung the table lists for the
host, so a broken companion cannot fail a cell that is about the other ladder.

NOT ``resolved_cell`` cells and not in the support matrix: the module
parametrizes its own argument and takes the BusyBox family lock itself
(``tests/conformance/conftest.py`` locks only ``resolved_cell`` items). It is
marked ``conformance_bed`` so the hermetic ``make conformance`` lane never
collects it; ``make conformance-bed`` runs it.
"""

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

import otto.host.transfer.nc as nc_module
from otto.host.host import BaseHost
from otto.host.transfer.nc import available_rungs
from tests.conformance._bed import BED_BUSYBOX, BedCell, bed_scratch_dir, bed_topology
from tests.conformance._console_safety import serialized_family
from tests.conformance._nc_rungs import (
    BED_NC_RUNGS,
    LADDERS,
    LISTENER,
    NC_RUNG_CELLS,
    PIN_OPTION,
    PORT,
    assert_failed_as_the_pin,
    listed_rungs,
    nc_rung_id,
)
from tests.conformance._transfer import transfer_backend_of
from tests.conftest import remote_name

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.conformance,
    pytest.mark.conformance_bed,
]

_PAYLOAD = "otto-nc-rung"


def _nc_cell(element: str) -> BedCell:
    """*element*'s ``nc`` cell over its first declared non-console term."""
    for bed_cell in bed_topology():
        cell = bed_cell.cell
        if cell.element == element and cell.transfer == "nc" and cell.term != "console":
            return bed_cell
    raise RuntimeError(f"the bed space offers no non-console nc cell for {element!r}")


def _pins(element: str, ladder: str, rung: str) -> dict[str, dict[str, object]]:
    """``nc_options`` pinning *ladder* to *rung* and the other ladder to its first listed rung."""
    pins = {
        PIN_OPTION[other]: BED_NC_RUNGS[element][other][0] for other in LADDERS if other != ladder
    }
    pins[PIN_OPTION[ladder]] = rung
    return {"nc_options": pins}


@contextlib.contextmanager
def _family_lock(bed_cell: BedCell) -> Iterator[None]:
    """The BusyBox family lock for a guest's cell; nothing for any other host."""
    if bed_cell.kind == BED_BUSYBOX:
        with serialized_family():
            yield
    else:
        yield


@contextlib.contextmanager
def recording_rungs() -> Iterator[dict[str, list[str]]]:
    """Record the name of every port and listener rung that runs, calling through.

    Patched where the backend looks the two functions up (the module globals of
    ``otto.host.transfer.nc``) and restored when the block ends.
    """
    record: dict[str, list[str]] = {PORT: [], LISTENER: []}
    real_port = nc_module.run_port_rung
    real_listener = nc_module.run_listener_rung

    async def port(rung, run, *, base_port, reserved):
        record[PORT].append(rung.name)
        return await real_port(rung, run, base_port=base_port, reserved=reserved)

    async def listener(rung, run, port_number):
        record[LISTENER].append(rung.name)
        return await real_listener(rung, run, port_number)

    nc_module.run_port_rung = port
    nc_module.run_listener_rung = listener
    try:
        yield record
    finally:
        nc_module.run_port_rung = real_port
        nc_module.run_listener_rung = real_listener


def assert_availability(
    table: dict[str, dict[str, tuple[str, ...]]], element: str, available: frozenset[str]
) -> None:
    """The host's available rungs are exactly the table's row for *element*, both ways."""
    listed = listed_rungs(table, element)
    assert available == listed, (
        f"{element}: the availability probe names rungs {sorted(available)} but the table "
        f"lists {sorted(listed)}; appeared: {sorted(available - listed)}, "
        f"disappeared: {sorted(listed - available)}. Re-measure and update BED_NC_RUNGS "
        f"in tests/conformance/_nc_rungs.py"
    )


def assert_ran_only(record: dict[str, list[str]], label: str, ladder: str, rung: str, op: str):
    """The *ladder* rungs that ran during *op* are *rung*, at least once, and no other."""
    ran = record[ladder]
    assert set(ran or [None]) == {rung}, (
        f"{label}: {op} pinned the {ladder} ladder to '{rung}' but the rungs that ran were {ran!r}"
    )


async def _read_remote(host: BaseHost, path: Path) -> str:
    answered = await host.exec(f"cat {path}")
    return answered.value if answered.retcode == 0 else f"<cat exited {answered.retcode}>"


async def _check_availability(host: BaseHost, bed_cell: BedCell, element: str) -> None:
    backend = transfer_backend_of(
        host, bed_cell.cell, refusal_tail="its netcat rungs cannot be asked for"
    )
    assert_availability(BED_NC_RUNGS, element, await available_rungs(backend._control_run))


@pytest.mark.parametrize("nc_rung", NC_RUNG_CELLS, ids=nc_rung_id)
async def test_every_rung_runs_where_the_table_lists_it(
    nc_rung: tuple[str, str, str], tmp_path: Path, worker_id: str
) -> None:
    """A listed rung moves a file and is the rung that ran; an unlisted one fails, named."""
    element, ladder, rung = nc_rung
    label = nc_rung_id(nc_rung)
    bed_cell = _nc_cell(element)
    scratch = bed_scratch_dir(element)
    assert scratch is not None, f"{label}: the bed gives {element} nowhere to put a file"
    name = remote_name(worker_id, f"ncrung_{ladder}_{rung}")
    source = tmp_path / "source" / name
    source.parent.mkdir()
    source.write_text(_PAYLOAD)
    retrieved_dir = tmp_path / "retrieved"
    retrieved_dir.mkdir()
    remote = scratch / name
    listed = rung in BED_NC_RUNGS[element][ladder]

    with _family_lock(bed_cell):
        async with bed_cell.open_host_with(_pins(element, ladder, rung)) as host:
            await _check_availability(host, bed_cell, element)
            tunnelled = host.hop is not None
            try:
                with recording_rungs() as record:
                    put = await host.put(source, scratch)
                if listed:
                    assert put.is_ok, f"{label}: put reported {put.status!r} -- {put.msg!r}"
                    assert_ran_only(record, label, ladder, rung, "put")
                    landed = await _read_remote(host, remote)
                    assert landed == _PAYLOAD, f"{label}: put landed {landed!r}"
                else:
                    assert_failed_as_the_pin(put, record[ladder], label, ladder, rung, "put")
                if not tunnelled:
                    return
                if not listed:
                    seeded = await host.exec(f"printf %s {_PAYLOAD} > {remote}")
                    assert seeded.retcode == 0, f"{label}: seeding {remote} failed: {seeded}"
                with recording_rungs() as record:
                    got = await host.get(remote, retrieved_dir)
                if listed:
                    assert got.is_ok, f"{label}: get reported {got.status!r} -- {got.msg!r}"
                    assert_ran_only(record, label, ladder, rung, "get")
                    back = (retrieved_dir / name).read_text()
                    assert back == _PAYLOAD, f"{label}: get brought back {back!r}"
                else:
                    assert_failed_as_the_pin(got, record[ladder], label, ladder, rung, "get")
            finally:
                await host.exec(f"rm -f {remote}")


# --- positive controls: each assertion above can go red --------------------------

_CONTROL_ELEMENTS = ["test2", "bb1350"]


@pytest.mark.parametrize("element", _CONTROL_ELEMENTS)
async def test_control_the_availability_check_fires_on_a_wrong_table(element: str) -> None:
    """A table missing a rung the host has, or listing one it lacks, fails the check."""
    bed_cell = _nc_cell(element)
    first_port = BED_NC_RUNGS[element][PORT][0]
    with _family_lock(bed_cell):
        async with bed_cell.open_host_with(_pins(element, PORT, first_port)) as host:
            backend = transfer_backend_of(
                host, bed_cell.cell, refusal_tail="its netcat rungs cannot be asked for"
            )
            available = await available_rungs(backend._control_run)
    assert_availability(BED_NC_RUNGS, element, available)

    row = BED_NC_RUNGS[element]
    gone = row[PORT][0]
    removed = {
        **BED_NC_RUNGS,
        element: {ladder: tuple(r for r in rungs if r != gone) for ladder, rungs in row.items()},
    }
    with pytest.raises(AssertionError, match=f"appeared: \\['{gone}'\\]"):
        assert_availability(removed, element, available)

    missing = [r for r in LADDERS[PORT] if r not in listed_rungs(BED_NC_RUNGS, element)]
    # the GNU hosts have every rung, so the name added there is one no ladder holds
    unlisted = missing[0] if missing else "invented"
    added = {**BED_NC_RUNGS, element: {**row, PORT: (*row[PORT], unlisted)}}
    with pytest.raises(AssertionError, match=f"disappeared: \\['{unlisted}'\\]"):
        assert_availability(added, element, available)


async def test_control_the_witness_fires_on_a_wrong_rung(tmp_path: Path, worker_id: str) -> None:
    """A recorder that writes down the wrong rung fails the which-rung-ran check."""
    element, ladder, rung, wrong = "test2", PORT, "netstat", "ss"
    label = f"control[{element}:{ladder}:{rung}]"
    bed_cell = _nc_cell(element)
    scratch = bed_scratch_dir(element)
    assert scratch is not None
    name = remote_name(worker_id, "ncrung_control")
    source = tmp_path / name
    source.write_text(_PAYLOAD)
    async with bed_cell.open_host_with(_pins(element, ladder, rung)) as host:
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
