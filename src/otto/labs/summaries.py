"""Every host a lab repository knows, summarized without building a lab when it can.

:func:`host_summaries_in_view` is what host-id completion and tunnel narrowing
read: the backend's cheap summaries route when it offers one, and a
best-effort walk over every lab otherwise. :func:`host_summaries` is the same
over a single ``default`` inventory.
:func:`list_host_ids` is its id-only view.
"""

import logging
from typing import TYPE_CHECKING

from ..host.inventory_ref import InventoryRef
from .drops import skip_failure
from .protocol import (
    HostSummary,
    LabRepository,
    SupportsHostSummaries,
    SupportsHostSummariesInView,
    logins_of_creds,
)
from .views import labs_view, load_lab_through

if TYPE_CHECKING:
    from ..inventory.protocol import Inventory
    from ..inventory.view import InventoryView

logger = logging.getLogger(__name__)


def host_summaries(
    *, repository: LabRepository, inventory: "Inventory | None" = None
) -> list[HostSummary]:
    """Every host *repository* knows — cheaply when it can, correctly always.

    :func:`host_summaries_in_view` over a view holding only *inventory* as
    ``default`` (:meth:`~otto.inventory.InventoryView.of_default`): a
    referenced entry is identified through its record (spec 2026-08-28
    host-inventory §6), and ``None`` is the no-inventory process, unchanged.
    """
    from ..inventory.view import InventoryView  # deferred: completion path

    return host_summaries_in_view(
        repository=repository, view=InventoryView.of_default(inventory=inventory)
    )


def host_summaries_in_view(
    *, repository: LabRepository, view: "InventoryView"
) -> list[HostSummary]:
    """Every host *repository* knows, each reference resolved through *view*.

    The view is first given the drop-recording refusal hook
    (:func:`~otto.labs.labs_view`), so every route below hands out an
    inventory that records a bare-reference refusal as one drop. Then, in
    order:

    1. :class:`~otto.labs.protocol.SupportsHostSummariesInView` when the
       backend implements it;
    2. :class:`~otto.labs.protocol.SupportsHostSummaries` (the built-in JSON
       backend's cheap route, deriving ids without constructing hosts) with
       :attr:`~otto.inventory.InventoryView.bare_inventory`;
    3. otherwise enumerating every lab and loading it through
       :func:`~otto.labs.load_lab_through`, which is slower but works for ANY
       backend — so a custom host source gets tab completion and tunnel
       narrowing with no extra code. Each summary's ``inventory_ref`` is the
       built host's.

    Best-effort by contract: a lab that fails to load is skipped with one
    drop and the others are summarized, because every caller is a completion
    or discovery path that must never crash the shell over one bad record. A
    lab refused for a bare reference records no second drop: the refusal
    recorded its own. A cancelled operation's error propagates
    (``otto.labs.drops.skip_failure``).
    """
    view = labs_view(view=view)
    if isinstance(repository, SupportsHostSummariesInView):
        return repository.list_host_summaries_in_view(view=view)
    if isinstance(repository, SupportsHostSummaries):
        return repository.list_host_summaries(inventory=view.bare_inventory)

    # No built-in filtering here: a backend's ``load_lab`` returns exactly its
    # own hosts (otto's ``local`` is injected later, by ``labs.loader.load_lab``,
    # not by any backend). Filtering would drop a lab that legitimately defines
    # its own ``local`` — which otto explicitly allows — and would make this
    # path disagree with the ``SupportsHostSummaries`` one.
    by_id: dict[str, HostSummary] = {}
    for name in repository.list_labs():
        try:
            lab = load_lab_through(repository=repository, name=name, view=view)
        except Exception as e:  # noqa: BLE001 — enumeration is best-effort
            logger.debug(f"skipping lab {name!r} while summarizing hosts: {e}")
            skip_failure(where=f"lab {name!r}", error=e, reason=f"could not load: {e}")
            continue
        for host in lab.hosts.values():
            existing = by_id.get(host.id)
            if existing is not None:
                if name not in existing.labs:
                    existing.labs.append(name)
                continue
            ref = getattr(host, "inventory_ref", None)
            by_id[host.id] = HostSummary(
                id=host.id,
                labs=[name],
                ip=getattr(host, "ip", "") or "",
                docker_capable=bool(getattr(host, "docker_capable", False)),
                docker_priority=int(getattr(host, "docker_priority", 0)),
                os_type=getattr(host, "os_type", None),
                logins=logins_of_creds(creds=getattr(host, "creds", None) or []),
                inventory_ref=ref if isinstance(ref, InventoryRef) else InventoryRef(),
            )
    return sorted(by_id.values(), key=lambda s: s.id)


def list_host_ids(*, repository: LabRepository) -> list[str]:
    """Every host id *repository* knows, sorted.

    The id-only view of :func:`host_summaries`.
    """
    return [summary.id for summary in host_summaries(repository=repository)]
