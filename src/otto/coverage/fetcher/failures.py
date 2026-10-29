"""What a coverage fetcher reports for each product it could not fetch, and the walk it shares.

Every fetcher stages each instrumented product's counters under
``<staging_root>/<host_id>/<product>/``: the remote fetcher over the network
(:mod:`otto.coverage.fetcher.remote`), the embedded collector over a board's
console (:mod:`otto.coverage.fetcher.embedded`). A product whose counters could
not be fetched is not an empty one. It is a failure the caller is told about,
by product, with the reason, and the rest of its host is still fetched. The
rule, the record and the walk that applies it live here, once, for both.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import OttoError
from ..tree import ProductKey

if TYPE_CHECKING:
    import re

    from ...host.product import Product

logger = logging.getLogger(__name__)

ALL_PRODUCTS = "*"
"""The product a failure is recorded under when its host failed before its
products could be listed (a raised listing, a cancelled host)."""


@dataclass(frozen=True)
class FetchFailure:
    """Why one product's counters could not be fetched."""

    reason: str
    remedy: str | None = None
    """What the user must change, when fixing the cause and running ``otto cov
    get`` again is not the remedy (a missing declaration, say); ``None`` for
    that usual one."""


FetchFailures = dict[ProductKey, FetchFailure]
"""Every product a fetch could not collect, and why; filled by the fetchers."""


class ProductFetchError(OttoError, RuntimeError):
    """A product's counters could not be fetched; they stay on the host.

    Raised by a per-product fetch once it has logged why. :func:`fetch_each`
    records it as that product's :class:`FetchFailure`.
    """

    def __init__(self, reason: str, *, remedy: str | None = None) -> None:
        super().__init__(reason)
        self.remedy = remedy


def record_failure(
    failures: FetchFailures, host_id: str, product: str, reason: "BaseException | str"
) -> None:
    """Record why *product* on *host_id* was not fetched: the one place a failure is written.

    *reason* is a message, or the exception that stopped the fetch: its own
    text, else its type's name, and a :class:`ProductFetchError`'s remedy.
    """
    if isinstance(reason, str):
        failures[ProductKey(host_id, product)] = FetchFailure(reason)
        return
    failures[ProductKey(host_id, product)] = FetchFailure(
        str(reason) or type(reason).__name__, getattr(reason, "remedy", None)
    )


async def fetch_each(
    host: Any,
    products: "list[Product]",
    failures: FetchFailures,
    fetch_one: "Callable[[Product], Awaitable[Path | None]]",
) -> dict[str, Path]:
    """Fetch each of *host*'s *products* on its own; ``{product: staging dir}``.

    *fetch_one* returns the product's staging dir, or ``None`` for no counters
    (an empty result, never a failure). A product whose fetch raised is
    recorded in *failures* and the next one is still fetched: a
    :class:`ProductFetchError` was logged by its raiser, any other exception
    is logged here. A cancellation propagates.
    """
    staged: dict[str, Path] = {}
    for product in products:
        try:
            dest = await fetch_one(product)
        except ProductFetchError as exc:  # already logged
            record_failure(failures, host.id, product.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — any failure is this product's, never its siblings'
            logger.warning("Failed to fetch .gcda for %s from %s: %s", product.name, host.id, exc)
            record_failure(failures, host.id, product.name, exc)
            continue
        if dest is not None:
            staged[product.name] = dest
    return staged


async def fetch_from_hosts(
    fetch_host: "Callable[..., Awaitable[dict[str, Path] | None]]",
    staging_root: Path,
    pattern: "re.Pattern[str] | None",
    failures: FetchFailures,
) -> dict[ProductKey, Path]:
    """Run *fetch_host* on every matching host concurrently; ``{ProductKey: dir}``.

    *fetch_host* is called as ``fetch_host(host, staging_root, failures)`` and
    returns ``{product: staging dir}`` or ``None``. A host whose call raised
    past it is logged and recorded under :data:`ALL_PRODUCTS`.
    """
    from ...fleet import do_for_all_hosts

    staging_root.mkdir(parents=True, exist_ok=True)
    per_host = await do_for_all_hosts(
        fetch_host, staging_root, failures, pattern=pattern, include_containers=True
    )
    staged: dict[ProductKey, Path] = {}
    for host_id, value in per_host.items():
        if isinstance(value, BaseException):
            logger.error("Failed to fetch coverage from %s: %s", host_id, value)
            record_failure(failures, host_id, ALL_PRODUCTS, value)
            continue
        for product, dest in (value or {}).items():
            staged[ProductKey(host_id, product)] = dest
    return staged


def list_products(host: Any, failures: FetchFailures) -> "list[Product] | None":
    """Return *host*'s instrumented products; ``None`` when they cannot be listed (recorded)."""
    from ..instrumentation import instrumented_products

    try:
        return instrumented_products(host)
    except Exception as exc:  # noqa: BLE001 — a host whose products cannot be listed fails as a whole
        logger.error("Cannot list the instrumented products on %s: %s", host.id, exc)  # noqa: TRY400 — the reason is recorded; no traceback
        record_failure(failures, host.id, ALL_PRODUCTS, exc)
        return None
