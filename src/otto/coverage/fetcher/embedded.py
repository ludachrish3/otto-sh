"""Collect ``.gcda`` coverage from embedded (Zephyr LLEXT) targets over the console.

Unix hosts write ``.gcda`` to a filesystem that otto fetches with
:class:`~otto.coverage.fetcher.remote.GcdaFetcher`.  Embedded RTOS targets have
no filesystem: a coverage-instrumented LLEXT extension built against NASA's
embedded-gcov dumps its counters as an ASCII hexdump over the serial console
(``call_fn cov_dump`` → ``__gcov_exit``).  This module reconstructs the binary
``.gcda`` files from that capture, mirroring embedded-gcov's ``serial_split.awk``
+ ``xxd -r``, so everything downstream (lcov merge → report) is reused unchanged.

Each instrumented product on a board is dumped on its own — via its loader's
call command — and staged under ``<staging_root>/<host_id>/<product>/``,
the same tree :class:`~otto.coverage.fetcher.remote.GcdaFetcher` writes.

The on-wire format, per source file::

    Emitting <N> bytes for <path>.gcda
    00000000: 61 64 63 67 2a 32 32 42 ...
    ...
    <path>.gcda
    Gcov End
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ... import layout
from ...config.fleet import do_for_all_hosts
from ...utils import Status

if TYPE_CHECKING:
    from ...host.embedded_host import EmbeddedHost

logger = logging.getLogger(__name__)

#: Console capture for a `cov_dump` can take several seconds (the hexdump is
#: emitted one printk-per-character); keep the timeout generous.
_DUMP_TIMEOUT = 120.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_EMIT_RE = re.compile(r"^Emitting\b")
_HEXDUMP_RE = re.compile(r"^[0-9a-fA-F]{8}:\s*(.*)$")
_HEXBYTE_RE = re.compile(r"^[0-9a-fA-F]{2}$")
_GCDA_LINE_RE = re.compile(r"\.gcda\s*$")


def decode_cov_dump(text: str) -> dict[str, bytes]:
    """Decode an embedded-gcov serial ``cov_dump`` capture.

    Args:
        text: Raw console output containing one or more emitted file blocks.
            ANSI colour codes and unrelated lines are tolerated.

    Returns:
        Mapping of ``.gcda`` basename → reconstructed binary contents.
    """
    result: dict[str, bytes] = {}
    collecting = False
    buf = bytearray()

    for raw in text.splitlines():
        line = _ANSI_RE.sub("", raw)

        # `Emitting ... <path>.gcda` opens a block; it also ends in `.gcda`, so
        # it must be matched before the closing-filename rule below.
        if _EMIT_RE.match(line):
            collecting = True
            buf = bytearray()
            continue

        if not collecting:
            continue

        hexdump = _HEXDUMP_RE.match(line)
        if hexdump:
            buf.extend(int(tok, 16) for tok in hexdump.group(1).split() if _HEXBYTE_RE.match(tok))
            continue

        # A bare `<path>.gcda` line closes the block and names the file.
        if _GCDA_LINE_RE.search(line):
            basename = line.strip().rsplit("/", 1)[-1]
            result[basename] = bytes(buf)
            collecting = False
            buf = bytearray()

    return result


async def _collect_one_embedded_host(
    host: "EmbeddedHost",
    staging_root: Path,
) -> dict[str, Path] | None:
    """Dump, decode and stage each instrumented ``llext`` product of one board.

    Each product is dumped with its loader's call command
    (``llext call_fn <product> <dump_fn>`` for the hex loader), decoded from
    the console hexdump, and written to ``<staging_root>/<host.id>/<product>/``
    (:func:`otto.layout.cov_product_dir_in`).

    Non-:class:`EmbeddedHost` hosts (Unix, Docker) carry no console dumper and
    are skipped, mirroring ``_fetch_one_host`` — ``do_for_all_hosts`` runs this
    over *every* configured host.

    Returns ``{product: staging_dir}``, or ``None`` when the host is not
    embedded, carries no instrumented product, or every dump produced nothing.
    """
    from ...host.embedded_host import EmbeddedHost
    from ..instrumentation import instrumented_products

    if not isinstance(host, EmbeddedHost):
        return None

    products = instrumented_products(host)
    if not products:
        logger.info("No instrumented products on %s — no embedded coverage from it", host.id)
        return None

    loader = host.loader
    if loader is None:
        logger.error("%s has no binary loader; cannot dump coverage", host.id)
        return None

    staged: dict[str, Path] = {}
    for product in products:
        # Only an llext product exports a dump function; the default lives on
        # LlextProduct, so anything without one is a product this collector
        # cannot dump rather than one to guess a function name for.
        dump_fn = getattr(product, "dump_fn", None)
        if dump_fn is None:
            logger.warning(
                "%s on %s has no dump_fn; not an llext product — skipped", product.name, host.id
            )
            continue

        dump_command = loader.call_command(product.name, dump_fn)
        logger.info(
            "Dumping embedded coverage for %s from %s via %r", product.name, host.id, dump_command
        )
        result = await host.exec(dump_command, timeout=_DUMP_TIMEOUT)
        if result.status != Status.Success:
            logger.error("%s failed on %s: %s", dump_command, host.id, result.value)
            continue

        blocks = decode_cov_dump(result.value)
        if not blocks:
            logger.warning("No coverage data decoded for %s from %s", product.name, host.id)
            continue

        # Only create the per-product dir once we have files, matching
        # GcdaFetcher and keeping the staging tree free of empty subdirs that
        # lcov chokes on.
        dest = layout.cov_product_dir_in(staging_root, host.id, product.name)
        dest.mkdir(parents=True, exist_ok=True)
        for name, data in blocks.items():
            (dest / name).write_bytes(data)

        logger.info(
            "Decoded %d .gcda file(s) for %s from %s into %s",
            len(blocks),
            product.name,
            host.id,
            dest,
        )
        staged[product.name] = dest

    return staged or None


class EmbeddedGcdaCollector:
    """Collect coverage from embedded (Zephyr LLEXT) hosts over the console.

    The embedded analogue of :class:`~otto.coverage.fetcher.remote.GcdaFetcher`:
    each instrumented product gets its own
    ``staging_root/<host.id>/<product>/`` subdirectory of decoded ``.gcda``
    files, so the downstream merge/report layer treats embedded and Unix hosts
    identically.
    """

    def __init__(
        self,
        staging_root: Path,
        pattern: re.Pattern[str] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.pattern = pattern

    async def collect_all(self) -> dict[tuple[str, str], Path]:
        """Dump, decode and stage coverage from every matching embedded host.

        Returns ``{(host_id, product): staging_dir}``. Non-embedded hosts,
        uninstrumented products, and failed dumps are omitted.
        """
        self.staging_root.mkdir(parents=True, exist_ok=True)

        collect_results = await do_for_all_hosts(
            _collect_one_embedded_host,
            self.staging_root,
            pattern=self.pattern,
        )

        results: dict[tuple[str, str], Path] = {}
        for host_id, value in collect_results.items():
            if isinstance(value, BaseException):
                logger.error("Failed to collect coverage from %s: %s", host_id, value)
                continue
            if value:
                for product, dest in value.items():
                    results[(host_id, product)] = dest
        return results


async def collect_embedded_coverage(
    staging_root: Path,
    pattern: re.Pattern[str] | None = None,
) -> dict[tuple[str, str], Path]:
    """Collect embedded coverage from every matching board's instrumented products.

    Args:
        staging_root: Directory under which per-host, per-product ``.gcda`` is
            staged (see :func:`otto.layout.cov_product_dir_in`).
        pattern: Optional compiled regex (the repo-declared ``[coverage].hosts``
            selector) matched against each host's id; ``None`` collects from
            every embedded host in the lab.

    Returns ``{(host_id, product): staging_dir}``, empty when no board carries
    an instrumented product (so the Unix-only path is unaffected).
    """
    return await EmbeddedGcdaCollector(staging_root, pattern=pattern).collect_all()
