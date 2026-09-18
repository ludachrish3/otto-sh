"""Fetch ``.gcda`` files from remote hosts, one product at a time.

Every product on a coverage host names the directory it writes counters
under (``Product.cov_dir``); each product's ``prepare_coverage`` hook runs
first to put its counters on disk, then the fetcher discovers ``.gcda`` there
with ``find`` and pulls them with the host's ``get`` (SCP, SFTP, FTP, netcat,
``docker cp`` — whatever the family provides) into
``<staging_root>/<host_id>/<product>/`` (:func:`otto.layout.cov_product_dir_in`),
where *staging_root* is the already-resolved local cov dir. Cleaning issues
each product's ``reset_coverage`` hook instead of a hardcoded ``find -delete``.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import layout
from ...config.fleet import do_for_all_hosts
from ...utils import Status

if TYPE_CHECKING:
    from ...host.product import Product

logger = logging.getLogger(__name__)


def _skipped_family(host: Any) -> bool:
    """Report whether *host* has no fetchable counters.

    Local and embedded hosts have none: the runner is not a SUT, and an
    embedded target has no filesystem (it dumps over the console instead —
    see :mod:`otto.coverage.fetcher.embedded`). Container hosts are fetched
    like any other Unix host: their ``exec`` is ``docker exec`` and their
    ``get`` is ``docker cp``.
    """
    from ...host.embedded_host import EmbeddedHost
    from ...host.local_host import LocalHost

    return isinstance(host, (LocalHost, EmbeddedHost))


async def _clean_one_host(host: Any) -> None:
    """Delete every instrumented product's ``.gcda`` on one host, logging each outcome."""
    if _skipped_family(host):
        return
    from ...host.product import cov_dir_of
    from ..instrumentation import instrumented_products

    for product in instrumented_products(host):
        # Before the hook runs, not after: a name that is not a single safe
        # path segment must never reach a host.
        layout.validate_product_name(product.name)
        cov_dir = cov_dir_of(product)
        result = await product.reset_coverage(host)
        if result.status is Status.NotRun:
            continue  # dry run: the session already printed the declined command
        if not result.is_ok:
            logger.warning(
                "Failed to clean .gcda for %s on %s (%s): %s",
                product.name,
                host.id,
                cov_dir,
                result.msg or result.value,
            )
        else:
            logger.info("Cleaned .gcda for %s on %s (%s)", product.name, host.id, cov_dir)


async def _fetch_one_product(host: Any, product: "Product", staging_root: Path) -> Path | None:
    """Discover and download one product's ``.gcda`` from *host*.

    Returns the product's staging dir, or ``None`` when nothing was found or
    the transfer failed. The dir is created only once files are known to
    exist, and removed again if the transfer then fails, so the staging tree
    never holds empty leaves — lcov chokes on them, and the report walk
    iterates ``<cov_dir>/<host>/<product>`` and would pick one up.

    A failed ``find`` and an empty one are NOT the same event: the first is a
    WARNING (the host could not be searched), the second is DEBUG, because an
    instrumented product that a run never exercised producing no counters is
    ordinary and would otherwise cry wolf on every collection.

    Raises:
        ValueError: *product* has a name that is not a single safe path
            segment. Checked before any host command is issued.
    """
    from ...host.product import cov_dir_of, gcda_find_cmd

    layout.validate_product_name(product.name)
    cov_dir = cov_dir_of(product)
    prepared = await product.prepare_coverage(host)
    if prepared.status is Status.NotRun:
        return None  # dry run: the session already printed the declined command
    if not prepared.is_ok:
        logger.warning(
            "%s:%s: prepare_coverage failed: %s — product skipped for this run",
            host.id,
            product.name,
            prepared.msg,
        )
        return None
    logger.info("Discovering .gcda for %s on %s:%s", product.name, host.id, cov_dir)
    find_result = await host.exec(gcda_find_cmd(cov_dir), timeout=60)
    if find_result.status is Status.NotRun:
        return None  # dry run: nothing to stage
    if find_result.status != Status.Success:
        logger.warning(
            "find failed for %s on %s at %s: %s",
            product.name,
            host.id,
            cov_dir,
            find_result.value,
        )
        return None
    gcda_files = [
        Path(line.strip())
        for line in find_result.value.strip().splitlines()
        if line.strip().endswith(".gcda")
    ]
    if not gcda_files:
        logger.debug("No .gcda files for %s on %s at %s", product.name, host.id, cov_dir)
        return None
    dest = layout.cov_product_dir_in(staging_root, host.id, product.name)
    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Fetching %d .gcda file(s) for %s from %s", len(gcda_files), product.name, host.id)
    try:
        get_result = await host.get(gcda_files, dest, show_progress=False)
    except BaseException:
        # A RAISED get (transport error, cancellation at a bed teardown) leaves
        # the same half-populated leaf a returned failure does, and a later
        # merge cannot tell a partial product from a complete one. Cleaned on
        # the way out; BaseException so a cancelled fetch is not the one door
        # that leaves debris. The exception is re-raised untouched.
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if not get_result.is_ok:
        logger.error(
            "Failed to fetch .gcda for %s from %s (%s): %s",
            product.name,
            host.id,
            cov_dir,
            get_result.msg,
        )
        # rmtree, not rmdir: a partly-transferred product leaves files behind,
        # and a half-populated leaf is worse than none. The HOST dir is left
        # alone — whether it is now empty is the host-level walk's business.
        shutil.rmtree(dest, ignore_errors=True)
        return None
    return dest


async def _fetch_one_host(host: Any, staging_root: Path) -> dict[str, Path] | None:
    """Fetch every instrumented product on *host*; ``{product: dir}`` or ``None``."""
    if _skipped_family(host):
        return None
    from ..instrumentation import instrumented_products

    products = instrumented_products(host)
    if not products:
        logger.info("No instrumented products on %s — no e2e coverage from it", host.id)
        return None
    fetched: dict[str, Path] = {}
    for product in products:
        dest = await _fetch_one_product(host, product, staging_root)
        if dest is not None:
            fetched[product.name] = dest
    return fetched or None


class GcdaFetcher:
    """Fetch ``.gcda`` from the lab's coverage hosts into a per-host, per-product staging tree.

    ::

        staging_root/
            host1/
                app/foo.gcda
                agent/bar.gcda
            host2/
                app/foo.gcda

    Hosts come from :func:`~otto.config.fleet.all_hosts` (containers included),
    optionally narrowed by *pattern* against each host's ``id``.
    """

    def __init__(
        self,
        staging_root: Path,
        pattern: re.Pattern[str] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.pattern = pattern

    async def fetch_all(self) -> dict[tuple[str, str], Path]:
        """Fetch from every matching host concurrently; ``{(host_id, product): dir}``.

        Hosts and products with no counters, and failed transfers, are omitted.
        """
        self.staging_root.mkdir(parents=True, exist_ok=True)

        fetch_results = await do_for_all_hosts(
            _fetch_one_host,
            self.staging_root,
            pattern=self.pattern,
            include_containers=True,
        )

        results: dict[tuple[str, str], Path] = {}
        for host_id, value in fetch_results.items():
            if isinstance(value, BaseException):
                logger.error("Failed to fetch from %s: %s", host_id, value)
                continue
            if value:
                for product, dest in value.items():
                    results[(host_id, product)] = dest
        return results

    async def clean_remote(self) -> None:
        """Delete every instrumented product's ``.gcda`` on every matching host.

        Should be called **before** a test run to ensure clean coverage
        data, and optionally **after** collection to save disk space.
        """
        clean_results = await do_for_all_hosts(
            _clean_one_host,
            pattern=self.pattern,
            include_containers=True,
        )
        for host_id, result in clean_results.items():
            if isinstance(result, BaseException):
                logger.warning("Failed to clean .gcda files on %s: %s", host_id, result)
