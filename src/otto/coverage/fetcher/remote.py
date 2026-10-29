"""Fetch ``.gcda`` files from remote hosts, one product at a time.

Every product on a coverage host names the directory it writes counters
under (``Product.cov_dir``); each product's ``prepare_coverage`` hook runs
first to put its counters on disk, then the fetcher archives the whole
``cov_dir`` with a plain ``tar`` into one archive in the host's temp dir (a
file ``mktemp`` makes under ``$TMPDIR``, else ``/tmp``), pulls that with the host's ``get`` (SCP,
SFTP, FTP, netcat, ``docker cp`` — whatever the family provides) and extracts
its ``.gcda`` (:func:`safe_extract`) into ``<staging_root>/<host_id>/<product>/``
(:func:`otto.layout.cov_product_dir_in`), where *staging_root* is the
already-resolved local cov dir. Each ``.gcda`` keeps its path relative to the
product's ``cov_dir``, so two units with one basename stay apart and each can
be paired with its own notes. Clearing counters
is not this module's job — see :func:`otto.coverage.collect.clean_coverage`,
which calls each product's own ``reset_coverage`` hook directly.
"""

import contextlib
import logging
import re
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import layout
from ...host.shell_quote import path_operand
from ...utils import Status
from ..tree import ProductKey
from .failures import FetchFailures, ProductFetchError, fetch_each, fetch_from_hosts, list_products

if TYPE_CHECKING:
    from ...host.product import Product

logger = logging.getLogger(__name__)

#: Printed instead of archiving when a product's ``cov_dir`` does not exist.
_NO_COV_DIR = "otto-no-cov-dir"

#: Printed before the host-side temp archive's path, as mktemp chose it.
_ARCHIVE_MARK = "otto-archive:"


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


def _discard_leaf(dest: Path) -> None:
    """Remove a product leaf that holds no fetched counters, and its host dir if now empty.

    rmtree, not rmdir: a partly-transferred product leaves files behind, and a
    half-populated leaf is worse than none. The host dir goes too when this
    leaf was its only entry, so a host whose products produced nothing leaves
    no empty dir for the report walk; a sibling product's leaf keeps it.
    """
    shutil.rmtree(dest, ignore_errors=True)
    with contextlib.suppress(OSError):
        dest.parent.rmdir()


async def _fetch_one_product(host: Any, product: "Product", staging_root: Path) -> Path | None:
    """Archive, download and extract one product's ``.gcda`` from *host*.

    The whole ``cov_dir`` is packed by a plain ``tar`` into one archive that
    ``mktemp`` makes in the host's temp dir, fetched with ``host.get`` only
    once tar exited 0, and extracted with :func:`safe_extract` keeping only
    ``.gcda``, so each lands at its path relative to ``cov_dir`` inside the
    product's staging dir. Reading ``cov_dir`` is all this needs; nothing is
    written there. The host-side archive is removed on every path, a raised
    one included (a cancellation during that removal propagates).

    Returns that staging dir, or ``None`` when nothing was found (or under a
    dry run). Every fetch stages into a fresh dir — one left by an
    earlier collection is removed first, so stale counters are never
    reported as fetched — and the dir is removed again when the transfer
    fails or yields no ``.gcda``, so the staging tree never holds empty or
    half-populated leaves: lcov chokes on them, and the report walk iterates
    ``<cov_dir>/<host>/<product>`` and would pick one up.

    A failed fetch and an empty result are NOT the same event: an archive
    command that exits non-zero (no writable temp dir for ``mktemp``, an
    unreadable file or directory, no tar at all), a transfer that failed or
    delivered no archive, and an archive :func:`safe_extract` refuses are
    WARNINGs that raise :class:`ProductFetchError` for this product only — its
    counters stay on the host for the next collection, never half-collected —
    and the host walk records it; an empty result is DEBUG,
    because an instrumented product that a run never exercised producing no
    counters (or no ``cov_dir`` at all) is ordinary and would otherwise cry
    wolf on every collection.

    Raises:
        ProductFetchError: The fetch failed, as above (already logged).
        ValueError: *product* has a name that is not a single safe path
            segment. Checked before any host command is issued.
    """
    from ...host.product import cov_dir_of

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
        raise ProductFetchError(f"prepare_coverage failed: {prepared.msg}")
    q = path_operand(cov_dir)
    # The archive goes to a file mktemp makes in the host's temp dir ($TMPDIR,
    # else /tmp), never into cov_dir: reading cov_dir is all a fetch needs, so
    # a root-owned one (a container's bind mount, a kernel's dump) fetches
    # unelevated. The explicit template (absolute, ending in XXXXXX) is the
    # one form GNU and BusyBox mktemp both take alike; its unique token lets
    # the removal find the file even when the archive command raised before
    # otto learned the path mktemp chose.
    token = f"otto-fetch-{uuid.uuid4().hex[:12]}"
    remove = f'"${{TMPDIR:-/tmp}}"/{token}.*'
    logger.info("Archiving .gcda for %s on %s:%s", product.name, host.id, cov_dir)
    # A missing cov_dir echoes the marker and exits 0 without an archive: the
    # product never ran, the same quiet event as a dir with no counters.
    # Otherwise the path mktemp printed is echoed after a marker before tar
    # runs, and the line's status is mktemp's, cd's or tar's: any of them
    # failing (no writable temp dir, an unreadable file or directory, no tar)
    # fails the product's fetch, and the partial archive is never fetched.
    # The removal follows every archive command that was issued, whatever it
    # answered or raised — a dry run's declined command made nothing. The
    # remove swallows an Exception of its own, so one being unwound is
    # re-raised as is; a cancellation arriving during the remove propagates.
    issued = True
    try:
        archive = await host.exec(
            f"! test -d {q} && echo {_NO_COV_DIR} || "
            f'(t=$(mktemp "${{TMPDIR:-/tmp}}/{token}.XXXXXX") && echo "{_ARCHIVE_MARK}$t" '
            f'&& cd {q} && tar -cf "$t" .)',
            timeout=120,
        )
        if archive.status is Status.NotRun:
            issued = False
            return None  # dry run: nothing was archived
        return await _take_archive(host, product, cov_dir, archive, staging_root)
    finally:
        if issued:
            await _remove_remote(host, remove)


async def _take_archive(
    host: Any, product: "Product", cov_dir: str, archive: Any, staging_root: Path
) -> Path | None:
    """Fetch and extract the archive the archive command answered, if it made one.

    Raises :class:`ProductFetchError` (after its WARNING) when the archive
    command failed, made no archive, or the transfer or extraction failed.

    Both markers are whole lines: the archive line starts with
    :data:`_ARCHIVE_MARK` and wins; the no-cov-dir sentinel is a line equal
    to :data:`_NO_COV_DIR`. Neither is ever found as a substring of other
    output — a ``$TMPDIR`` path or a login banner may contain either.
    """
    dest = layout.cov_product_dir_in(staging_root, host.id, product.name)
    lines = (archive.value or "").splitlines()
    remote_tar = next(
        (line[len(_ARCHIVE_MARK) :].strip() for line in lines if line.startswith(_ARCHIVE_MARK)),
        "",
    )
    if archive.status != Status.Success:
        output = "\n".join(line for line in lines if not line.startswith(_ARCHIVE_MARK)).strip()
        logger.warning(
            "Failed to archive .gcda for %s on %s at %s: %s",
            product.name,
            host.id,
            cov_dir,
            output,
        )
        raise ProductFetchError(f"archiving {cov_dir} failed: {output}")
    if not remote_tar:
        if any(line.strip() == _NO_COV_DIR for line in lines):
            logger.debug("No cov_dir for %s on %s at %s", product.name, host.id, cov_dir)
            return None
        answer = repr("\n".join(lines).strip())
        logger.warning(
            "Failed to archive .gcda for %s on %s at %s: no archive path in %s",
            product.name,
            host.id,
            cov_dir,
            answer,
        )
        raise ProductFetchError(f"archiving {cov_dir} failed: no archive path in {answer}")
    local_tar = dest / Path(remote_tar).name
    failure: str | None = None
    try:
        # A fresh leaf for every fetch: counters left by an earlier collection
        # must never be reported as this one's. Without ignore_errors, so the
        # real cause surfaces (the host-side archive is removed by the caller
        # whatever happens here).
        _reset_leaf(dest)
        got = await host.get([Path(remote_tar)], dest, show_progress=False)
        if not got.is_ok:
            failure = str(got.msg) or "transfer failed"
        elif not local_tar.is_file():
            failure = "the transfer reported success but delivered no archive"
        else:
            try:
                safe_extract(local_tar, dest, suffix=".gcda")
            except (tarfile.TarError, ValueError) as exc:
                failure = f"unusable archive: {exc}"
    except BaseException:
        # A RAISED get (transport error, cancellation at a bed teardown) leaves
        # the same half-populated leaf a returned failure does, and a later
        # merge cannot tell a partial product from a complete one. Cleaned on
        # the way out; BaseException so a cancelled fetch is not the one door
        # that leaves debris. The exception is re-raised untouched.
        _discard_leaf(dest)
        raise
    finally:
        with contextlib.suppress(OSError):
            local_tar.unlink(missing_ok=True)
    if failure is not None:
        logger.warning(
            "Failed to fetch .gcda for %s from %s (%s): %s", product.name, host.id, cov_dir, failure
        )
        _discard_leaf(dest)
        raise ProductFetchError(f"fetching {cov_dir} failed: {failure}")
    return _keep_if_counters(dest, host, product, cov_dir)


def _reset_leaf(dest: Path) -> None:
    """Make *dest* an empty, freshly created dir; any earlier contents are removed."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)


def _keep_if_counters(dest: Path, host: Any, product: "Product", cov_dir: str) -> Path | None:
    """Return *dest* if it holds a ``.gcda``; otherwise discard it (DEBUG) and return ``None``."""
    if next(dest.rglob("*.gcda"), None) is None:
        _discard_leaf(dest)
        logger.debug("No .gcda files for %s on %s at %s", product.name, host.id, cov_dir)
        return None
    return dest


async def _remove_remote(host: Any, path: str) -> None:
    """Remove the host-side temp archive (*path*: a shell word, glob allowed).

    This runs in cleanup paths, so an ``Exception`` from the remove is DEBUG
    and never raised over the error being unwound. A cancellation
    (``BaseException``) arriving during the remove propagates: that is
    asyncio's contract for a cancelled task.
    """
    try:
        await host.exec(f"rm -f {path}", timeout=30)
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the real error
        logger.debug("Could not remove %s on %s: %s", path, host.id, exc)


def safe_extract(archive: Path, dest: Path, *, suffix: str | None = None) -> None:
    """Extract only regular files that stay inside *dest*.

    With *suffix*, only members whose name ends with it are considered at
    all — the rest of the archive (a log beside the counters, a link, a
    stray archive a crashed fetch left) is skipped before any check, never
    written. Every considered member must be a regular file or a directory;
    a link, device or FIFO is refused before anything is written.
    Extraction then uses tarfile's ``data`` filter where the interpreter has
    it (3.10.12+, 3.11.4+), else validates each member's destination by
    hand. Either way an absolute name or a ``..`` escape is refused.

    Raises:
        ValueError: A considered member is not a regular file or directory,
            or (without the ``data`` filter) would land outside *dest*.
        tarfile.TarError: The ``data`` filter refused a member, or *archive*
            is not a readable tar.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        if suffix is not None:
            members = [m for m in members if m.name.endswith(suffix)]
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"{archive}: refusing non-regular member {member.name}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
            return
        root = dest.resolve()
        for member in members:
            if member.isdir():
                continue
            target = (dest / member.name).resolve()
            if not target.is_relative_to(root):
                raise ValueError(
                    f"{archive}: refusing member outside the destination: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is not None:
                target.write_bytes(src.read())


async def _fetch_one_host(
    host: Any, staging_root: Path, failures: FetchFailures
) -> dict[str, Path] | None:
    """Fetch every instrumented product on *host*; ``{product: dir}`` or ``None``.

    Each product is fetched on its own (:func:`~otto.coverage.fetcher.failures.fetch_each`):
    one that fails is recorded in *failures* and the next one is still fetched.
    """
    if _skipped_family(host):
        return None
    products = list_products(host, failures)
    if products is None:
        return None
    if not products:
        logger.info("No instrumented products on %s — no e2e coverage from it", host.id)
        return None

    async def fetch_one(product: "Product") -> Path | None:
        return await _fetch_one_product(host, product, staging_root)

    return await fetch_each(host, products, failures, fetch_one) or None


class GcdaFetcher:
    """Fetch ``.gcda`` from the lab's coverage hosts into a per-host, per-product staging tree.

    ::

        staging_root/
            host1/
                app/build/src/foo.gcda      # path relative to app's cov_dir
                agent/bar.gcda
            host2/
                app/build/src/foo.gcda

    Hosts come from :func:`~otto.fleet.all_hosts` (containers included),
    optionally narrowed by *pattern* against each host's ``id``.
    """

    def __init__(
        self,
        staging_root: Path,
        pattern: re.Pattern[str] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.pattern = pattern

    async def fetch_all(self, *, failures: FetchFailures) -> dict[ProductKey, Path]:
        """Fetch from every matching host concurrently; ``{ProductKey: dir}``.

        Hosts and products with no counters are omitted. A product whose fetch
        failed is recorded in *failures* instead, with the reason (under
        ``"*"`` when its host failed before its products could be listed).
        Required: a caller that ignored failures would report a lost product
        as one that ran nothing.
        """
        return await fetch_from_hosts(_fetch_one_host, self.staging_root, self.pattern, failures)
