"""What each netcat bed host can run: the committed table and the cells it makes.

MEASURED, NOT INFERRED. Per bed host the table records:

- the port strategies the host can run, as
  :func:`~otto.host.transfer.nc_scripts.availability_probe` printed them over
  ``host.exec`` on the host's first declared non-console term. A port strategy
  never runs the netcat binary, so it is a fact of the host, not of an
  implementation;
- how otto reaches the host for a transfer (``direct``, or ``tunnelled``
  through a hop), which decides whether a get runs the ladders at all;
- the exit status otto receives when the listener command names a program the
  host does not have, through that host's lifetime-cap wrapper (127 on every
  host but ``bb1161``, whose BusyBox 1.16.1 ``timeout`` answers 1);
- the netcat implementations on the host, each by the ``nc_options.exec_name``
  that selects it and the path ``readlink -f "$(command -v <name>)"`` printed
  for that name, with each listener check's state for that implementation.

No capability is derived from the host's family or its BusyBox version: a
guest that gains or loses a tool, or whose ``nc`` starts pointing at another
netcat, is a change this table has to be told about, and
``tests/conformance/test_nc_rung_contract.py`` fails, naming the host and the
implementation, until it is. A BusyBox guest's version is not typed here: it
is the image builder's (``GUEST_TABLE`` in
``scripts/build_busybox_guest_images.py``, whose versions are the
``BUSYBOX_MATRIX`` releases the guests are built from), and the identity cell
asserts the applet reports it. ``docs/cli/host/netcat-compatibility.md`` is
generated from this table (``scripts/render_netcat_compatibility.py``), so it
is importable with no bed and holds everything that page shows.

The ladders' own order lives in ``src/otto/host/transfer/nc_scripts.py``
(``PORT_LADDER``, ``LISTENER_LADDER``); a row here keeps that order, so that
the first strategy that works is the one a cell pins its companion ladder to.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from otto.host.transfer.nc_scripts import LADDER_OPTION, LISTENER_LADDER, PORT_LADDER
from scripts.build_busybox_guest_images import GUEST_TABLE

PORT = "port"
LISTENER = "listener"

LADDERS: dict[str, tuple[str, ...]] = {
    PORT: tuple(rung.name for rung in PORT_LADDER),
    LISTENER: tuple(rung.name for rung in LISTENER_LADDER),
}
"""Every rung of each ladder, in the ladder's own order."""

PIN_OPTION: dict[str, str] = LADDER_OPTION
"""The ``nc_options`` field that pins a rung of each ladder."""

WORKS: Final = "works"
"""A listener check that, pinned, moved a file with this implementation."""

TOOL_ABSENT: Final = "tool-absent"
"""A listener check whose tool the host does not have."""

DOES_NOT_WORK: Final = "does-not-work"
"""A listener check whose tool is there and that fails with this implementation."""


@dataclass(frozen=True)
class ListenerCheck:
    """One listener check's state for one netcat implementation on one host.

    Attributes:
        state: ``works``, ``tool-absent`` or ``does-not-work``.
        reason: Why the check does not work, in plain language; only for
            ``does-not-work``.
        opens_with: Text the failed transfer's message holds, so the cell can
            tell this failure from any other; only for ``does-not-work``.
        issue: The issue that tracks the failure, as ``#1234``; only for
            ``does-not-work``.
    """

    state: Literal["works", "tool-absent", "does-not-work"]
    reason: str = ""
    opens_with: str = ""
    issue: str = ""


@dataclass(frozen=True)
class NcImplementation:
    """One netcat implementation on one host.

    Attributes:
        exec_name: What ``nc_options.exec_name`` is set to, to select it.
        resolves_to: What ``readlink -f "$(command -v <exec_name>)"`` printed.
        package: Where the implementation comes from, as the documentation
            names it.
        listener: Each ``LISTENER_LADDER`` rung's state for it.
    """

    exec_name: str
    resolves_to: str
    package: str
    listener: dict[str, ListenerCheck]


@dataclass(frozen=True)
class BedNetcat:
    """What one bed host's netcat transfer can run.

    Attributes:
        port: The port strategies the host can run, in ladder order.
        route: ``direct``, or ``tunnelled`` through the host's hop.
        missing_binary_status: The exit status of a listener command that
            names a program the host does not have.
        implementations: The netcat implementations on the host; the first
            is the one a cell about the port ladder runs.
        userland: What the host is, in the words a reader matches their own
            host by (``Ubuntu lab VM``, ``BusyBox 1.16.1 guest``).
    """

    port: tuple[str, ...]
    route: Literal["direct", "tunnelled"]
    missing_binary_status: int
    implementations: tuple[NcImplementation, ...]
    userland: str


def _states(**states: 'Literal["works", "tool-absent"]') -> dict[str, ListenerCheck]:
    """One :class:`ListenerCheck` per listener rung, from ``rung=state`` pairs."""
    return {rung: ListenerCheck(state=state) for rung, state in states.items()}


# availability_probe printed "ss netstat python proc" on test1-test4 and on
# ubu2404, and "netstat proc" on each of the five BusyBox guests.
_GNU_PORT = ("ss", "netstat", "python", "proc")
_BUSYBOX_PORT = ("netstat", "proc")

# test1-test4: every listener check moved a file with the pinned rung (the
# strategy cells' bed passes). On ubu2404 the three tools are present and each
# implementation binds, transfers and exits; each check's state there is works,
# which the listener-check cells assert in the bed gate lane by moving a file
# with that check pinned.
_GNU_LISTENER = _states(ss=WORKS, netstat=WORKS, proc=WORKS)
# The BusyBox guests have no ss; netstat and proc moved a file with the pinned rung.
_BUSYBOX_LISTENER = _states(ss=TOOL_ABSENT, netstat=WORKS, proc=WORKS)

_OPENBSD_NC = NcImplementation(
    exec_name="nc",
    resolves_to="/usr/bin/nc.openbsd",
    package="netcat-openbsd",
    listener=_GNU_LISTENER,
)


def _busybox(version: str) -> NcImplementation:
    """The BusyBox ``nc`` applet of *version*; ``nc`` is ``/bin/busybox`` on every guest."""
    return NcImplementation(
        exec_name="nc",
        resolves_to="/bin/busybox",
        package=f"BusyBox {version} applet",
        listener=_BUSYBOX_LISTENER,
    )


def _lab_vm() -> BedNetcat:
    return BedNetcat(
        port=_GNU_PORT,
        route="direct",
        missing_binary_status=127,
        implementations=(_OPENBSD_NC,),
        userland="Ubuntu lab VM",
    )


BUSYBOX_GUEST_VERSIONS: dict[str, str] = {guest.element: guest.version for guest in GUEST_TABLE}
"""BusyBox guest element -> the BusyBox version it runs, as the image builder pins it."""

# BusyBox 1.16.1's `timeout -t N PROG` exits 1, not 127, for a missing PROG.
_MISSING_BINARY_STATUS = {"bb1161": 1}


def _busybox_guest(element: str) -> BedNetcat:
    version = BUSYBOX_GUEST_VERSIONS[element]
    return BedNetcat(
        port=_BUSYBOX_PORT,
        route="tunnelled",
        missing_binary_status=_MISSING_BINARY_STATUS.get(element, 127),
        implementations=(_busybox(version),),
        userland=f"BusyBox {version} guest",
    )


BED_NETCAT: dict[str, BedNetcat] = {
    "test1": _lab_vm(),
    "test2": _lab_vm(),
    "test3": _lab_vm(),
    "test4": _lab_vm(),
    **{element: _busybox_guest(element) for element in BUSYBOX_GUEST_VERSIONS},
    # The Ubuntu 24.04 QEMU guest behind test4: `nc` and `netcat` resolve to
    # nc.openbsd there, so the three implementations are selected by their own names.
    "ubu2404": BedNetcat(
        port=_GNU_PORT,
        route="tunnelled",
        missing_binary_status=127,
        implementations=(
            NcImplementation(
                exec_name="nc.openbsd",
                resolves_to="/usr/bin/nc.openbsd",
                package="netcat-openbsd",
                listener=_GNU_LISTENER,
            ),
            NcImplementation(
                exec_name="nc.traditional",
                resolves_to="/usr/bin/nc.traditional",
                package="netcat-traditional",
                listener=_GNU_LISTENER,
            ),
            NcImplementation(
                exec_name="ncat",
                resolves_to="/usr/bin/ncat",
                package="ncat (Nmap)",
                listener=_GNU_LISTENER,
            ),
        ),
        userland="Ubuntu 24.04 guest",
    ),
}
"""Bed host element -> what its netcat transfer can run."""


def implementation(element: str, exec_name: str) -> NcImplementation:
    """The implementation *exec_name* selects on *element*."""
    for impl in BED_NETCAT[element].implementations:
        if impl.exec_name == exec_name:
            return impl
    raise KeyError(f"{element} has no netcat implementation selected by {exec_name!r}")


def working_checks(impl: NcImplementation) -> tuple[str, ...]:
    """The listener checks that work with *impl*, in ladder order."""
    return tuple(r for r in LADDERS[LISTENER] if impl.listener[r].state == WORKS)


def available_rungs_of(table: dict[str, BedNetcat], element: str) -> frozenset[str]:
    """Every rung whose tool *table* says *element* has, over both ladders.

    What :func:`~otto.host.transfer.nc_scripts.availability_probe` prints on
    the host: the port strategies, and every listener check whose tool is
    there, whether or not it works with an implementation.
    """
    row = table[element]
    tools = {
        rung
        for impl in row.implementations
        for rung, check in impl.listener.items()
        if check.state != TOOL_ABSENT
    }
    return frozenset(row.port) | frozenset(tools)


NC_HOST_CELLS: list[str] = list(BED_NETCAT)
"""One cell per netcat bed host: its availability, and a netcat that does not exist."""

NC_PORT_CELLS: list[tuple[str, str]] = [
    (element, rung) for element in BED_NETCAT for rung in LADDERS[PORT]
]
"""One ``(element, rung)`` per bed host and port strategy, listed or not."""

NC_IMPLEMENTATION_CELLS: list[tuple[str, str]] = [
    (element, impl.exec_name) for element, row in BED_NETCAT.items() for impl in row.implementations
]
"""One ``(element, exec_name)`` per bed host and netcat implementation."""

NC_LISTENER_CELLS: list[tuple[str, str, str]] = [
    (element, exec_name, rung)
    for element, exec_name in NC_IMPLEMENTATION_CELLS
    for rung in LADDERS[LISTENER]
]
"""One ``(element, exec_name, rung)`` per implementation and listener check, absent tools too."""


NC_CONTRACT_MODULE = "test_nc_rung_contract.py"
"""The netcat contract's file, whose items ``tests/conformance/conftest.py`` reads."""

_NC_CELL_PARAMS = ("element", "nc_impl", "nc_port", "nc_listener")
"""The netcat contract's cell parameters; each value is an element or a tuple led by one."""


def nc_cell_element(params: Mapping[str, object]) -> str | None:
    """The bed host a netcat contract item runs on, read from its parameters.

    ``None`` for an item with no cell parameter: the pure-function controls,
    which open no host.
    """
    for name in _NC_CELL_PARAMS:
        value = params.get(name)
        if isinstance(value, tuple):
            value = value[0]
        if isinstance(value, str):
            return value
    return None


def nc_host_id(element: str) -> str:
    """The pytest id of a host cell: ``<element>``."""
    return element


def nc_port_id(cell: tuple[str, str]) -> str:
    """The pytest id of a port strategy cell: ``<element>:<rung>``."""
    element, rung = cell
    return f"{element}:{rung}"


def nc_implementation_id(cell: tuple[str, str]) -> str:
    """The pytest id of an implementation cell: ``<element>:<exec_name>``."""
    element, exec_name = cell
    return f"{element}:{exec_name}"


def nc_listener_id(cell: tuple[str, str, str]) -> str:
    """The pytest id of a listener check cell: ``<element>:<exec_name>:<rung>``."""
    element, exec_name, rung = cell
    return f"{element}:{exec_name}:{rung}"


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


def assert_failed_as_recorded(
    result: Any, ran: list[str], check: ListenerCheck, label: str, rung: str, op: str
) -> None:
    """*result* failed with the pinned listener check *rung*, as its ``does-not-work`` row says.

    Args:
        result: The operation's result.
        ran: The listener rungs the recorder saw run during *op*.
        check: The table's row for the rung, whose ``opens_with`` the message holds.
        label: The cell's id, for the message.
        rung: The listener check the cell pinned.
        op: ``put`` or ``get``.
    """
    assert not result.is_ok, (
        f"{label}: {op} with the listener check '{rung}' pinned reported {result.status!r}, "
        f"but the table records it as not working ({check.issue}: {check.reason}). "
        f"Measure the row again"
    )
    assert set(ran or [None]) == {rung}, (
        f"{label}: {op} pinned the listener ladder to '{rung}' but the rungs that ran were {ran!r}"
    )
    assert check.opens_with in result.msg, (
        f"{label}: {op} failed, but not as the table records ({check.issue}: "
        f"{check.opens_with!r}): {result.msg!r}"
    )
