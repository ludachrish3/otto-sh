"""Every host a lab repository knows, summarized without building a lab when it can.

:func:`host_summaries` is what host-id completion and tunnel narrowing read:
the backend's cheap :class:`~otto.labs.protocol.SupportsHostSummaries` route
when it offers one, and a best-effort walk over every lab otherwise.
:func:`list_host_ids` is its id-only view.
"""

import logging
from typing import TYPE_CHECKING

from .protocol import HostSummary, LabRepository, SupportsHostSummaries, logins_of_creds

if TYPE_CHECKING:
    from ..inventory import Inventory

logger = logging.getLogger(__name__)


def host_summaries(
    repository: LabRepository, inventory: "Inventory | None" = None
) -> list[HostSummary]:
    """Every host *repository* knows — cheaply when it can, correctly always.

    Uses :class:`~otto.labs.protocol.SupportsHostSummaries` when the backend
    implements it (the built-in JSON backend does, deriving ids without
    constructing hosts). Otherwise falls back to enumerating every lab and
    loading it, which is slower but works for ANY backend — so a custom
    host source gets tab completion and tunnel narrowing with no extra code.

    *inventory* reaches BOTH routes (spec 2026-08-28 host-inventory §6): a
    referenced entry is identified through its record, so without it the fast
    path skips such an entry and the fallback's ``load_lab`` fails for the
    whole lab. ``None`` is the no-inventory process, unchanged.

    Best-effort by contract: a lab that fails to load is skipped, because
    every caller is a completion or discovery path that must never crash the
    shell over one bad record.
    """
    if isinstance(repository, SupportsHostSummaries):
        return repository.list_host_summaries(inventory=inventory)

    # No built-in filtering here: a backend's ``load_lab`` returns exactly its
    # own hosts (otto's ``local`` is injected later, by ``config.lab.load_lab``,
    # not by any backend). Filtering would drop a lab that legitimately defines
    # its own ``local`` — which otto explicitly allows — and would make this
    # path disagree with the ``SupportsHostSummaries`` one.
    by_id: dict[str, HostSummary] = {}
    for name in repository.list_labs():
        try:
            lab = repository.load_lab(name, inventory=inventory)
        except Exception as e:  # noqa: BLE001 — enumeration is best-effort
            from .drops import record_drop

            logger.debug(f"skipping lab {name!r} while summarizing hosts: {e}")
            record_drop(f"lab {name!r}", f"could not load: {e}")
            continue
        for host in lab.hosts.values():
            existing = by_id.get(host.id)
            if existing is not None:
                if name not in existing.labs:
                    existing.labs.append(name)
                continue
            by_id[host.id] = HostSummary(
                id=host.id,
                labs=[name],
                ip=getattr(host, "ip", "") or "",
                docker_capable=bool(getattr(host, "docker_capable", False)),
                os_type=getattr(host, "os_type", None),
                logins=logins_of_creds(getattr(host, "creds", None) or []),
            )
    return sorted(by_id.values(), key=lambda s: s.id)


def list_host_ids(repository: LabRepository) -> list[str]:
    """Every host id *repository* knows, sorted.

    The id-only view of :func:`host_summaries`.
    """
    return [summary.id for summary in host_summaries(repository)]
