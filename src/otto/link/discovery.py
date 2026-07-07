"""Dynamic-link discovery contract + the all-provenance accessor.

Cost-split by design (foundation spec §6): ``Lab.static_links()`` is free and
synchronous; everything here is async because the dynamic layer costs one
round-trip per lab host. The live ``asyncio.gather`` of ``pgrep`` across hosts
(feeding :func:`otto.link.sentinel.parse_discovery`) arrives with the
``otto link`` CLI — this module fixes the signatures consumers rely on.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .model import Link

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

DiscoverFn = Callable[["Lab"], Awaitable[list[Link]]]


async def discover_dynamic_links(lab: "Lab") -> list[Link]:
    """Discover live otto tunnels across the lab's Unix hosts.

    Contract only in the foundation: the live implementation (gather a
    ``pgrep -af '^otto-link:'`` across hosts, parse via
    ``sentinel.parse_discovery``, resolve endpoint ips against *lab*) ships
    with the ``otto link`` CLI (sub-project #2).
    """
    raise NotImplementedError(
        "dynamic-link discovery arrives with the otto link CLI (sub-project #2)"
    )


async def all_links(lab: "Lab", *, discover: DiscoverFn = discover_dynamic_links) -> list[Link]:
    """Every link across provenances, merged by route id.

    Static (implicit and declared) plus dynamic; on a shared id the
    **dynamic** entry wins — an observed tunnel is higher-fidelity than the
    declaration it realizes. *discover* is injectable for tests and for #2's
    live layer.
    """
    merged = {link.id: link for link in lab.static_links()}
    for link in await discover(lab):
        merged[link.id] = link
    return list(merged.values())
