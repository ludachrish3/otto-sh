"""Collect ``.gcda`` coverage from embedded (Zephyr LLEXT) targets over the console.

Unix hosts write ``.gcda`` to a filesystem that otto fetches with
:class:`~otto.coverage.fetcher.remote.GcdaFetcher`.  Embedded RTOS targets have
no filesystem: a coverage-instrumented LLEXT extension built against NASA's
embedded-gcov dumps its counters as an ASCII hexdump over the serial console
(``call_fn cov_dump`` → ``__gcov_exit``).  This module reconstructs the binary
``.gcda`` files from that capture, mirroring embedded-gcov's ``serial_split.awk``
+ ``xxd -r``, so everything downstream (lcov merge → report) is reused unchanged.

The on-wire format, per source file::

    Emitting <N> bytes for <path>.gcda
    00000000: 61 64 63 67 2a 32 32 42 ...
    ...
    <path>.gcda
    Gcov End
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...configmodule.configmodule import do_for_all_hosts
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
    host: EmbeddedHost,
    dump_command: str,
    staging_root: Path,
) -> Path | None:
    """Dump, decode and stage coverage for a single embedded host.

    Issues *dump_command* (e.g. ``llext call_fn <ext> cov_dump``) on the host's
    console, decodes the resulting hexdump, and writes the reconstructed
    ``.gcda`` files to ``staging_root/<host.id>/``.

    Non-:class:`EmbeddedHost` hosts (Unix, Docker) carry no console dumper and
    are skipped, mirroring ``_fetch_one_host`` — ``do_for_all_hosts`` runs this
    over *every* configured host.

    Returns the per-host staging directory, or ``None`` if the host was skipped,
    the dump failed, or it produced no coverage data.
    """
    from ...host.embedded_host import EmbeddedHost

    if not isinstance(host, EmbeddedHost):
        return None

    label = host.id

    logger.info("Dumping embedded coverage from %s via %r", label, dump_command)
    result = await host.oneshot(dump_command, timeout=_DUMP_TIMEOUT)
    if result.status != Status.Success:
        logger.error("cov_dump failed on %s: %s", label, result.output)
        return None

    blocks = decode_cov_dump(result.output)
    if not blocks:
        logger.warning("No coverage data decoded from %s", label)
        return None

    # Only create the per-host dir once we have files, matching GcdaFetcher and
    # keeping the staging tree free of empty subdirs that lcov chokes on.
    dest = staging_root / label
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in blocks.items():
        (dest / name).write_bytes(data)

    logger.info("Decoded %d .gcda file(s) from %s into %s", len(blocks), label, dest)
    return dest


class EmbeddedGcdaCollector:
    """Collect coverage from embedded (Zephyr LLEXT) hosts over the console.

    The embedded analogue of :class:`~otto.coverage.fetcher.remote.GcdaFetcher`:
    each host gets its own ``staging_root/<host.id>/`` subdirectory of decoded
    ``.gcda`` files, so the downstream merge/report layer treats embedded and
    Unix hosts identically.

    *dump_command* is the console command that triggers the product's coverage
    dump (e.g. ``llext call_fn <ext> cov_dump`` → ``__gcov_exit``).
    """

    def __init__(
        self,
        staging_root: Path,
        dump_command: str,
        pattern: re.Pattern[str] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.dump_command = dump_command
        self.pattern = pattern

    async def collect_all(self) -> dict[str, Path]:
        """Dump, decode and stage coverage from every matching embedded host.

        Returns a mapping of host id → per-host staging directory.  Non-embedded
        hosts, hosts with no coverage data, and failed dumps are omitted.
        """
        self.staging_root.mkdir(parents=True, exist_ok=True)

        collect_results = await do_for_all_hosts(
            _collect_one_embedded_host,
            self.dump_command,
            self.staging_root,
            pattern=self.pattern,
        )

        results: dict[str, Path] = {}
        for host_id, value in collect_results.items():
            if isinstance(value, BaseException):
                logger.error("Failed to collect coverage from %s: %s", host_id, value)
                continue
            if value is not None:
                results[host_id] = value
        return results


async def collect_embedded_coverage(
    cov_config: dict[str, Any],
    staging_root: Path,
    pattern: re.Pattern[str] | None = None,
) -> dict[str, Path]:
    """Collect embedded coverage per the ``[coverage.embedded]`` config section.

    Reads the product's extension name from ``cov_config['embedded']['extension']``
    and dumps it via ``llext call_fn <ext> cov_dump`` (the conventional
    embedded-gcov ``__gcov_exit`` trigger) on every embedded host whose id
    matches ``pattern``.

    Args:
        cov_config: The repo's ``[coverage]`` table.
        staging_root: Directory under which per-host ``.gcda`` is staged.
        pattern: Optional compiled regex (the repo-declared ``[coverage].hosts``
            selector) matched against each host's id; ``None`` collects from
            every embedded host in the lab.

    Returns ``{host_id: staging_dir}``, or ``{}`` when no embedded coverage is
    configured (so the Unix-only path is unaffected).
    """
    embedded_cfg = cov_config.get("embedded") or {}
    extension = embedded_cfg.get("extension")
    if not extension:
        return {}

    dump_command = f"llext call_fn {extension} cov_dump"
    return await EmbeddedGcdaCollector(
        staging_root,
        dump_command,
        pattern=pattern,
    ).collect_all()
