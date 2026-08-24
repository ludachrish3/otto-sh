"""The machine-readable axis space: what a host *is*, for conformance sampling.

Almost every axis is read off a host that otto built, never off ``lab.json``.
Ten of the nineteen bed hosts do not declare ``valid_terms`` (the seven
Zephyr guests, which DO declare ``valid_transfers: ["console"]``; and
alt1/alt2/alt3, which are the only three omitting ``os_type``,
``valid_terms`` and ``valid_transfers`` alike) and the factory supplies what
they omit, so reading the raw file would produce wrong axes for more than
half the bed while looking correct on the nine that do declare theirs
(test1-4 and the five bb guests).

Two fields are read from the raw entry rather than the host: ``hop``, walked
across entries in ``_hop_depth`` to count chain depth (a property of the lab,
not of any one host), and ``userland_options``, which must be read raw
because the host normalizes its absence into a defaults object that is
TRUTHY -- measured: ``test1`` declares no ``userland_options`` in
``lab.json``, yet ``host.userland_options`` is an all-``None``
``UserlandOptions(...)`` whose truth value is ``True``, so reading it off the
host would send every plain unix host down the busybox branch. Two more
values are neither host- nor raw-read in the ordinary sense: ``element`` is
the function's own argument, and ``os_version``/``sw_version`` come from
``getattr(host, ..., None)`` -- a host class that doesn't define the
attribute (``ZephyrHost`` has no ``sw_version``) reads as ``None`` rather
than raising, a manufactured absence marker, not a value the host reported.
``UnixHost`` defines both and simply reports ``None`` when unset, so the
two absences are indistinguishable downstream.

Named ``HostAxes`` rather than ``Profile``: ``otto.host.os_profile.OsProfile``
is a product concept that ``os_type`` selects, and it supplies defaults *into*
the factory. This records what a host came out with.
"""

import importlib
import json
import sys
from dataclasses import dataclass

from otto.host.command_frame import FRAME_CLASSES
from otto.host.embedded_host import EmbeddedHost
from otto.host.factory import create_host_from_dict
from tests._fixtures.labdata import lab_data_path
from tests._fixtures.paths import ensure_custom_hosts_on_path


@dataclass(frozen=True)
class HostAxes:
    """The axis values of one host, as otto resolved them."""

    os_type: str
    userland: str
    terms: list[str]
    transfers: list[str]
    hop_depth: int
    docker_capable: bool


def _userland(data: dict) -> str:
    """``"gnu"`` | ``"busybox-<ver>"`` | ``"zephyr-<ver>"`` from a lab entry.

    Deliberately not derived from ``os_type``: the BusyBox guests report
    ``os_type: unix``, so the flavor lives in the userland layer instead;
    and ``os_type`` is a profile SELECTOR, not a family (a project can name
    a profile ``zephyr-3.7`` with ``base = "embedded"``), so a Zephyr guest
    can select a renamed profile whose ``os_type`` never equals -- or even
    starts with -- the literal string ``"zephyr"``. ``os_family`` is the
    authoritative, host-derived answer instead.
    """
    # `os_family` is the authoritative, host-derived answer (axes_for sets it
    # via `isinstance(host, EmbeddedHost)`). No `os_type` fallback: an
    # `os_type.startswith("zephyr")` half was tried and rejected -- it was
    # unpinned (nothing distinguished it from `os_family` alone; deleting it
    # changed no test outcome) and, worse, it could be WRONG rather than
    # merely redundant: a unix-based profile literally named `zephyr-9` would
    # resolve to zephyr, and `os_family` could never veto it once `or`-ed in.
    if data.get("os_family") == "embedded":
        version = data.get("os_version")
        if not version:
            raise ValueError(f"zephyr host {data.get('element')!r} declares no os_version")
        return f"zephyr-{version}"
    if data.get("userland_options"):
        version = data.get("sw_version")
        if not version:
            raise ValueError(f"busybox host {data.get('element')!r} declares no sw_version")
        return f"busybox-{version}"
    return "gnu"


def _entries(tech: str) -> dict[str, dict]:
    hosts = json.loads(lab_data_path(tech).read_text())["hosts"]
    return {h["element"]: h for h in hosts}


def _hop_depth(element: str, entries: dict[str, dict]) -> int:
    """How many hops sit between the runner and *element*."""
    depth, seen, current = 0, {element}, entries[element]
    while current.get("hop"):
        hop = current["hop"]
        if hop in seen:
            raise ValueError(f"hop cycle through {hop!r}")
        seen.add(hop)
        depth += 1
        current = entries[hop]
    return depth


def _ensure_custom_frames() -> None:
    """Register the out-of-tree frames the lab data references.

    `zephyr27_fat` declares `command_frame: "zephyr-inline"`, a class that
    lives in tests/custom_hosts rather than in otto. Without this the host
    cannot be built at all -- and, worse, it builds fine under a whole-suite
    run where some other conftest already imported the module, so the same
    code passes or fails depending on the pytest invocation. Importing it
    here makes the resolver independent of what else has been collected.

    Checks ``FRAME_CLASSES`` itself first, rather than trusting
    ``sys.modules``: if the frame is already registered there is nothing to
    do, and this returns immediately. Measured: the root conftest's
    ``_isolate_registries`` fixture (autouse, ``tests/conftest.py:1711``)
    snapshots ``FRAME_CLASSES`` per test and, on teardown, its
    ``_restore_registries`` helper (``tests/conftest.py:1766-1837``) evicts
    the *registering* module (``custom_hosts``, the origin
    ``register_command_frame`` records) from ``sys.modules`` -- but not the
    already-imported ``custom_hosts.zephyr_inline`` submodule, which stays
    cached. So when the frame is NOT already registered -- including right
    after that eviction -- a plain re-import of the dotted submodule would
    hit Python's cached-module fast path and never re-run
    ``custom_hosts/__init__.py``, so ``register_command_frame`` would never
    fire again. That is why, only on this branch, both ``sys.modules``
    entries are evicted before importing: it forces a truly fresh import
    that re-runs ``__init__.py`` and re-registers.
    """
    if "zephyr-inline" in FRAME_CLASSES:
        return
    ensure_custom_hosts_on_path()
    sys.modules.pop("custom_hosts", None)
    sys.modules.pop("custom_hosts.zephyr_inline", None)
    importlib.import_module("custom_hosts.zephyr_inline")


def axes_for(element: str, tech: str = "tech1") -> HostAxes:
    """Resolve *element*'s axes by building it and reading what otto produced."""
    _ensure_custom_frames()
    entries = _entries(tech)
    if element not in entries:
        raise KeyError(f"{element!r} is not in the {tech} lab data")
    data = entries[element]
    host = create_host_from_dict(dict(data))
    # Every axis the host object carries is read OFF THE HOST, including the
    # version fields and docker_capable. Measured: bb1350 declares no
    # `docker_capable` in lab.json but its host reports False, so reading the
    # raw dict with a `.get(..., False)` default would restate a rule the host
    # class already owns -- the drift this module exists to avoid. An attribute
    # the class does not define at all (EmbeddedHost has no `docker_capable`)
    # is the class saying "not applicable"; that reads as False, and getattr's
    # default is the only place a value is invented here.
    resolved = {
        "element": element,
        "os_type": host.os_type,
        # `isinstance`, not an `os_type` string comparison: `os_type` on the
        # built host/spec is the profile SELECTOR, and a renamed profile like
        # `zephyr-3.7` never equals the literal `"zephyr"`. Class identity is
        # the one thing a renamed profile can't disguise -- and, with no
        # `os_type` fallback left in `_userland`, this is now the ONLY path
        # to a zephyr userland; the existing Zephyr `axes_for` tests are what
        # exercise it.
        "os_family": "embedded" if isinstance(host, EmbeddedHost) else "unix",
        "os_version": getattr(host, "os_version", None),
        "sw_version": getattr(host, "sw_version", None),
        # `userland_options` must be read from the raw entry, not the host:
        # UnixHost normalizes its absence into a defaults object
        # (`UserlandOptions()`) that is TRUTHY -- measured, see the module
        # docstring -- so reading it off the host would send every plain
        # unix host down the busybox branch. (`hop`, the other raw-read
        # field, is walked directly in `_hop_depth` because chain depth is a
        # property of the lab, not of any one host.)
        "userland_options": data.get("userland_options"),
    }
    return HostAxes(
        os_type=host.os_type,
        userland=_userland(resolved),
        terms=list(host.valid_terms),
        transfers=list(host.valid_transfers),
        hop_depth=_hop_depth(element, entries),
        docker_capable=bool(getattr(host, "docker_capable", False)),
    )


@dataclass(frozen=True)
class Cell:
    """One resolvable conformance target: a host reached a specific way."""

    element: str
    term: str
    transfer: str


def axis_space(lab: str, tech: str = "tech1") -> list[Cell]:
    """Every ``(host, term, transfer)`` the menus permit for hosts in *lab*.

    Crossed from each host's own menus rather than enumerated as a literal
    list of cells: the menus are what ``axes_for`` just resolved off the built
    host, so a host that gains a transfer or loses a term changes its own cell
    count with no edit here. A hand-written enumeration would be this module
    restating otto's answer -- the exact drift the module exists to avoid --
    and it would keep passing after the host stopped agreeing with it.

    Menus are emitted in the order the host reported them, never sorted.
    Measured: ``test2`` reports ``['telnet', 'ssh']`` while ``test1`` and
    ``test3`` report ``['ssh', 'telnet']``, so a sort here would be this
    module inventing a value the host did not give. Callers that need a
    stable order must impose their own.
    """
    entries = _entries(tech)
    # Membership is the one axis with no host-side answer to defer to.
    # Measured: `hasattr(host, "labs")` is False on a built host -- the spec
    # declares `labs: list[str]` (default_factory=list) but never forwards it
    # -- and `BaseHost.source_lab` is a different concept, the loader's
    # `lab_name` argument (factory.py:177), which is `""` here because
    # `axes_for` builds hosts without one. So the raw entry is the only
    # source, and because the spec's default is an empty list, `or []`
    # reproduces the factory's answer exactly; unlike the menus, there is no
    # substantive defaulting here to lose by reading raw.
    members = [e for e, d in entries.items() if lab in (d.get("labs") or [])]
    if not members:
        # Raising beats returning `[]`: an empty space satisfies every
        # sampling assertion vacuously, so a typo'd lab name would read as
        # full conformance instead of as no coverage at all.
        raise KeyError(f"no host in {tech} declares membership in lab {lab!r}")
    cells: list[Cell] = []
    for element in members:
        axes = axes_for(element, tech)
        cells += [Cell(element, t, x) for t in axes.terms for x in axes.transfers]
    return cells
