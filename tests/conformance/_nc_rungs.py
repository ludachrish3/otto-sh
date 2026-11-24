"""Which netcat rungs each bed host can run: the committed table and the cells it makes.

MEASURED, NOT INFERRED. Each row is what
:func:`~otto.host.transfer.nc_scripts.availability_probe` printed on that host,
run through ``host.exec`` over the host's first declared non-console term
(2026-11-24). No row is derived from the host's family or its BusyBox version:
a guest that gains or loses a tool is a change this table has to be told about,
and ``tests/conformance/test_nc_rung_contract.py`` fails, naming the rung, until
it is.

The ladders' own rung order lives in ``src/otto/host/transfer/nc_scripts.py``
(``PORT_LADDER``, ``LISTENER_LADDER``); a row here keeps that order so that its
first entry is the rung a cell pins its companion ladder to.
"""

from typing import Any

from otto.host.transfer.nc_scripts import LADDER_OPTION, LISTENER_LADDER, PORT_LADDER

PORT = "port"
LISTENER = "listener"

LADDERS: dict[str, tuple[str, ...]] = {
    PORT: tuple(rung.name for rung in PORT_LADDER),
    LISTENER: tuple(rung.name for rung in LISTENER_LADDER),
}
"""Every rung of each ladder, in the ladder's own order."""

PIN_OPTION: dict[str, str] = LADDER_OPTION
"""The ``nc_options`` field that pins a rung of each ladder."""

_GNU = {PORT: ("ss", "netstat", "python", "proc"), LISTENER: ("ss", "netstat", "proc")}
_BUSYBOX = {PORT: ("netstat", "proc"), LISTENER: ("netstat", "proc")}

BED_NC_RUNGS: dict[str, dict[str, tuple[str, ...]]] = {
    # availability_probe printed "ss netstat python proc" on each of the four.
    "test1": _GNU,
    "test2": _GNU,
    "test3": _GNU,
    "test4": _GNU,
    # availability_probe printed "netstat proc" on each of the five guests.
    "bb1161": _BUSYBOX,
    "bb1211": _BUSYBOX,
    "bb1281": _BUSYBOX,
    "bb1310": _BUSYBOX,
    "bb1350": _BUSYBOX,
}
"""Bed host element -> ladder -> the rungs it can run, in ladder order."""

NC_RUNG_CELLS: list[tuple[str, str, str]] = [
    (element, ladder, rung)
    for element in BED_NC_RUNGS
    for ladder, rungs in LADDERS.items()
    for rung in rungs
]
"""One ``(element, ladder, rung)`` per bed host and rung of each ladder, listed or not."""


def nc_rung_id(cell: tuple[str, str, str]) -> str:
    """The pytest id of one rung cell: ``nc-rung[<element>:<ladder>:<rung>]``."""
    element, ladder, rung = cell
    return f"nc-rung[{element}:{ladder}:{rung}]"


def listed_rungs(table: dict[str, dict[str, tuple[str, ...]]], element: str) -> frozenset[str]:
    """Every rung *table* lists for *element*, over both ladders."""
    return frozenset(name for rungs in table[element].values() for name in rungs)


PINNED_TOOL_FAILED: dict[str, str] = {
    PORT: "The netcat port finder's pinned '{rung}' rung failed",
    LISTENER: "The netcat listener check's '{rung}' rung failed",
}
"""How the failure of a pinned rung whose tool failed opens, per ladder.

A readiness timeout names the rung and the option too, so naming them is no
proof that the rung ran and its tool failed; this opening is.
"""

_READINESS_TIMEOUT = "not ready within"


def assert_failed_as_the_pin(
    result: Any, ran: list[str], label: str, ladder: str, rung: str, op: str
) -> None:
    """*result* failed because the pinned *rung* ran and its tool failed.

    Args:
        result: The operation's result.
        ran: The *ladder* rungs the recorder saw run during *op*.
        label: The cell's id, for the message.
        ladder: ``port`` or ``listener``.
        rung: The unlisted rung the cell pinned.
        op: ``put`` or ``get``.
    """
    assert not result.is_ok, (
        f"{label}: {op} with the unlisted {ladder} rung '{rung}' pinned reported "
        f"{result.status!r} -- the table says this host cannot run it"
    )
    assert set(ran or [None]) == {rung}, (
        f"{label}: {op} pinned the {ladder} ladder to '{rung}' but the rungs that ran were {ran!r}"
    )
    opening = PINNED_TOOL_FAILED[ladder].format(rung=rung)
    not_the_tool = f"{label}: {op} failed, but not as the '{rung}' rung's tool failure"
    assert opening in result.msg, f"{not_the_tool}: {result.msg!r}"
    assert _READINESS_TIMEOUT not in result.msg, f"{not_the_tool}: {result.msg!r}"
    option = f"nc_options.{PIN_OPTION[ladder]}"
    assert option in result.msg, f"{label}: {op} failed without naming {option}: {result.msg!r}"
