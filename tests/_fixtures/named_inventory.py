"""Inventory and lab-source doubles for named-inventory tests (lab templating, #1176).

:class:`FakeInventory` is the smallest inventory: a dict of records and a
declared supplies set. ``tests/unit/inventory/test_resolve.py`` re-exports it
for the suite's older importers.

:class:`LookupCounting` counts the record lookups an inventory serves, so a
test can assert that an inventory a configuration must never consult saw
``lookups == 0``. It is not ``tests/unit/inventory/test_cache.py``'s
``Counting``, which counts *fetches* (``list_keys``) and can be taken offline.

:func:`register_lookup_counting` makes it a registered inventory backend, so a
settings file's ``[inventory] backend = "fake:counting"`` (or an
``[[inventories]]`` entry naming that backend) builds it through the real
configuration path rather than a test injecting it.

:class:`FakeDiscoveryInventory` is a remote-shaped backend for the snapshot
cache's staleness rules: it answers discovery, reports no fingerprint and can
be made to fail with a typed error.

:class:`FakeViewSource` is a lab source that implements every optional view
capability of :mod:`otto.labs.protocol` (view loads, summaries in view, status,
scope) and counts its calls, so composite and dispatch tests can model a
source's policy, scope and outage without lab files.

:class:`LinkedViewSource` is a :class:`FakeViewSource` whose loads carry
declared links already resolved, as a backend that resolves its own links
hands them over.

:func:`json_twin` writes a discovery to a file the ``json`` backend reads, and
:func:`assert_same_discovery` asserts two inventories discover the same
records and instances, so a test can show a backend and its file twin agree.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict, host_identity
from otto.inventory import (
    Discovery,
    InventoryDataError,
    InventoryError,
    InventoryKeyError,
    InventoryUnreachableError,
    InventoryView,
    JsonInventory,
    ResolvedEntry,
    check_supplies,
    discover_inventory,
    discovery_to_document,
    parse_max_stale,
    register_inventory_backend,
    resolve_host_entry,
    resolve_view_entry,
)
from otto.labs import HostSummary, LabNotFoundError, LabScope, LabStatus, logins_of_host_data
from otto.labs.loader import Lab
from otto.link.model import Link
from otto.models.base import OttoModel
from otto.models.inventory import InventoryInstance, InventoryRecord


class FakeInventory:
    """The smallest Inventory: a dict of records and a declared supplies set."""

    def __init__(self, records: dict[str, dict], supplies=None, label="fake:mem"):
        self._records = {k: InventoryRecord.model_validate(v) for k, v in records.items()}
        self.supplies = check_supplies(supplies)
        self.label = label

    def lookup(self, key):
        try:
            return self._records[key]
        except KeyError:
            raise InventoryKeyError(key, label=self.label) from None

    def list_keys(self):
        return sorted(self._records)

    def fingerprint(self):
        return "fake"


LOOKUP_COUNTING_BACKEND = "fake:counting"
"""The backend name :func:`register_lookup_counting` registers by default."""


class LookupCounting(FakeInventory):
    """A :class:`FakeInventory` that counts the lookups it serves."""

    def __init__(self, records: dict[str, dict[str, Any]], **kw: Any) -> None:
        super().__init__(records, **kw)
        self.lookups = 0
        """Every :meth:`lookup` call, found or not."""

    def lookup(self, key):
        self.lookups += 1
        return super().lookup(key)


class _NoSettings(OttoModel, frozen=True):
    """The backend's configuration: it takes no keys beyond ``backend``."""


def register_lookup_counting(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: dict[str, dict[str, Any]],
    name: str = LOOKUP_COUNTING_BACKEND,
) -> LookupCounting:
    """Register backend *name* as an inventory serving *records*; return its inventory.

    Every build of the backend returns the one returned :class:`LookupCounting`,
    so the test reads ``.lookups`` on exactly what otto consulted. Its label is
    *name*. It supplies ``FakeInventory``'s default set, reports a fixed
    fingerprint and is never wrapped in the snapshot cache.

    A test with several inventories (``default`` and ``physical``, say)
    registers one per inventory under distinct names, e.g. ``name=
    "fake:physical"``, and names each in its settings table. Registering one
    name twice in a test raises ``DuplicateRegistration``.

    *monkeypatch* is the test's own. The registration needs no undo of its
    own: the root conftest's ``_isolate_registries`` restores every backend
    table after each test.
    """
    inventory = LookupCounting(records, label=name)
    register_inventory_backend(
        name,
        config=_NoSettings,
        factory=lambda configured: inventory,
        snapshot_cache=False,
    )
    return inventory


class FakeDiscoveryInventory:
    """A remote-shaped inventory: answers discovery, reports no fingerprint, can fail typed.

    ``fingerprint()`` is ``None``, so a registration with ``snapshot_cache=True``
    gets it wrapped in the snapshot cache, as NetBox is. *fail* makes every
    fetch (``inventory_discovery()`` and ``list_keys()``) raise the typed error
    a real backend would: ``"data"`` an
    :class:`~otto.inventory.InventoryDataError`, ``"unreachable"`` an
    :class:`~otto.inventory.InventoryUnreachableError` carrying *max_stale*,
    parsed by :func:`~otto.inventory.parse_max_stale` exactly as a backend's
    own setting is. It is a plain attribute, so a test may take the inventory
    down after a first good fetch. :attr:`raised` holds the last error raised,
    so a test can assert it is a refusal's ``__cause__``.

    :attr:`fetches` counts fetch ATTEMPTS, failed ones included, starting at
    *fetches*.
    """

    def __init__(
        self,
        records: dict[str, dict[str, Any]],
        *,
        instances: "dict[str, dict[str, Any]] | None" = None,
        supplies: "list[str] | None" = None,
        label: str = "fake:disc",
        fail: 'Literal["data", "unreachable"] | None' = None,
        max_stale: "str | None" = None,
        fetches: int = 0,
    ) -> None:
        self._records = {k: InventoryRecord.model_validate(v) for k, v in records.items()}
        self._instances = (
            None
            if instances is None
            else {k: InventoryInstance.model_validate(v) for k, v in instances.items()}
        )
        self.supplies = check_supplies(supplies)
        self.label = label
        self.fail = fail
        self.max_stale = parse_max_stale(text=max_stale)
        self.fetches = fetches
        self.raised: "InventoryError | None" = None

    def _fetch(self) -> None:
        """Count one attempt; raise the configured typed error, if any."""
        self.fetches += 1
        if self.fail == "data":
            self.raised = InventoryDataError(f"{self.label}: field 'ip' missing from the output")
        elif self.fail == "unreachable":
            self.raised = InventoryUnreachableError(
                f"{self.label}: connection refused", max_stale=self.max_stale
            )
        else:
            return
        raise self.raised

    def inventory_discovery(self) -> Discovery:
        self._fetch()
        return Discovery(records=dict(self._records), instances=self._instances)

    def list_keys(self) -> list[str]:
        self._fetch()
        return sorted(self._records)

    def lookup(self, key: str) -> InventoryRecord:
        try:
            return self._records[key]
        except KeyError:
            raise InventoryKeyError(key, label=self.label) from None

    def fingerprint(self) -> None:
        return None


class FakeViewSource:
    """A lab source implementing every view capability, for composite and dispatch tests.

    *labs* maps a lab name to its element dicts, each ``{"name": ..., "hosts":
    [...]}`` with host dicts as a lab file holds them (a host may carry a bare
    or qualified ``inventory`` reference). Every entry is resolved with
    :func:`~otto.inventory.resolve_view_entry` (the legacy :meth:`load_lab`,
    with :func:`~otto.inventory.resolve_host_entry`) and built by
    :func:`~otto.host.factory.create_host_from_dict`; each summary's
    ``inventory_ref`` is the resolved entry's.

    *statuses* is what :meth:`list_lab_status` returns; ``None`` reports every
    lab in *labs* available. *scope* is what :meth:`lab_scope` returns.

    *fail*, when set, is raised by the calls *fail_on* names: ``"status"``
    (:meth:`list_lab_status`), ``"load"`` (both loads and
    :meth:`list_host_summaries_in_view`) or ``"both"``, which models a source
    that is down. :meth:`list_labs` and :meth:`lab_scope` never fail.

    :attr:`calls` counts every call by kind: ``"list_labs"``, ``"status"``,
    ``"load"`` (either load), ``"summaries"`` and ``"scope"``. :attr:`views`
    keeps, by the same kinds, every view a view capability received
    (``"status"``, ``"load"`` for :meth:`load_lab_in_view`, ``"summaries"``).
    """

    def __init__(
        self,
        labs: dict[str, list[dict[str, Any]]],
        *,
        statuses: "list[LabStatus] | None" = None,
        scope: "LabScope | None" = None,
        fail: "Exception | None" = None,
        fail_on: Literal["status", "load", "both"] = "status",
    ) -> None:
        self.labs = labs
        self.statuses = statuses
        self.scope = scope
        self.fail = fail
        self.fail_on = fail_on
        self.calls: Counter[str] = Counter()
        self.views: dict[str, list[InventoryView]] = {"status": [], "load": [], "summaries": []}

    def _maybe_fail(self, kind: Literal["status", "load"]) -> None:
        """Raise :attr:`fail` when it is set and *fail_on* covers *kind*."""
        if self.fail is not None and self.fail_on in (kind, "both"):
            raise self.fail

    def _entries(self, name: str) -> list[dict[str, Any]]:
        if name not in self.labs:
            raise LabNotFoundError(f"Lab {name!r} not found. Known labs: {sorted(self.labs)}")
        return self.labs[name]

    def _build(self, name: str, *, preferences, resolve) -> Lab:
        self.calls["load"] += 1
        self._maybe_fail("load")
        lab = Lab(name=name)
        for entry in self._entries(name):
            element = Element(name=entry["name"])
            for host_data in entry["hosts"]:
                resolved: ResolvedEntry = resolve(host_data, element)
                lab.add_host(
                    create_host_from_dict(
                        resolved.host_data,
                        preferences=preferences,
                        lab_name=name,
                        element=element,
                        inventory_ref=resolved.ref,
                    )
                )
        return lab

    def list_labs(self) -> list[str]:
        self.calls["list_labs"] += 1
        return sorted(self.labs)

    def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
        return self._build(
            name,
            preferences=preferences,
            resolve=lambda d, e: resolve_host_entry(d, inventory=inventory, element=e),
        )

    def load_lab_in_view(self, name, *, preferences=None, view: InventoryView) -> Lab:
        self.views["load"].append(view)
        return self._build(
            name,
            preferences=preferences,
            resolve=lambda d, e: resolve_view_entry(d, view=view, element=e),
        )

    def list_host_summaries_in_view(self, *, view: InventoryView) -> list[HostSummary]:
        self.calls["summaries"] += 1
        self.views["summaries"].append(view)
        self._maybe_fail("load")
        by_id: dict[str, HostSummary] = {}
        for name in sorted(self.labs):
            for entry in self.labs[name]:
                element = Element(name=entry["name"])
                for host_data in entry["hosts"]:
                    try:
                        resolved = resolve_view_entry(host_data, view=view, element=element)
                        identity = host_identity(resolved.host_data, element=element)
                    except (ValueError, TypeError, KeyError, InventoryError):
                        continue
                    existing = by_id.get(identity.id)
                    if existing is not None:
                        if name not in existing.labs:
                            existing.labs.append(name)
                        continue
                    by_id[identity.id] = HostSummary(
                        id=identity.id,
                        labs=[name],
                        ip=identity.ip,
                        docker_capable=identity.docker_capable,
                        docker_priority=identity.docker_priority,
                        os_type=str(resolved.host_data.get("os_type", "unix")),
                        logins=logins_of_host_data(resolved.host_data),
                        inventory_ref=resolved.ref,
                    )
        return sorted(by_id.values(), key=lambda s: s.id)

    def list_lab_status(self, *, view: InventoryView) -> list[LabStatus]:
        self.calls["status"] += 1
        self.views["status"].append(view)
        self._maybe_fail("status")
        if self.statuses is not None:
            return list(self.statuses)
        return [LabStatus(name=name) for name in sorted(self.labs)]

    def lab_scope(self) -> "LabScope | None":
        self.calls["scope"] += 1
        return self.scope


class LinkedViewSource(FakeViewSource):
    """A view source that resolves its own declared links: every lab it loads carries *links*.

    The shape of a backend without
    :class:`~otto.labs.protocol.SupportsLabContribution`, which hands the
    composite its links resolved.
    """

    def __init__(self, labs: dict[str, list[dict[str, Any]]], *, links: "list[Link]") -> None:
        super().__init__(labs)
        self._links = links

    def load_lab_in_view(self, name, *, preferences=None, view: InventoryView) -> Lab:
        lab = super().load_lab_in_view(name, preferences=preferences, view=view)
        lab.links = list(self._links)
        return lab


def json_twin(*, tmp_path: Path, discovery: Discovery) -> JsonInventory:
    """Return a :class:`~otto.inventory.JsonInventory` over *discovery*, written under *tmp_path*.

    The file is :func:`~otto.inventory.discovery_to_document`'s document: flat
    when *discovery* has no instances, ``otto.inventory.v2`` otherwise.
    """
    path = tmp_path / "twin-inventory.json"
    path.write_text(json.dumps(discovery_to_document(discovery=discovery)))
    return JsonInventory(path)


def assert_same_discovery(*, left: Any, right: Any) -> None:
    """Assert inventories *left* and *right* discover equal records and instances."""
    found_left = discover_inventory(inventory=left)
    found_right = discover_inventory(inventory=right)
    assert found_left.records == found_right.records
    assert found_left.instances == found_right.instances
