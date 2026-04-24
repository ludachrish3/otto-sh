"""Fetch ``.gcda`` files from remote hosts using otto's file transfer.

Uses :meth:`RemoteHost.get() <otto.host.remoteHost.RemoteHost.get>`
which supports SCP, SFTP, FTP, and netcat with progress tracking and
multi-hop SSH chains.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ...configmodule.configmodule import do_for_all_hosts
from ...utils import Status

if TYPE_CHECKING:
    from ...host.remoteHost import RemoteHost

logger = logging.getLogger(__name__)


async def _clean_one_host(
    host: RemoteHost,
    gcda_remote_dir: str,
) -> None:
    """Delete .gcda files on a single host, logging the outcome."""
    label = host.id
    result = await host.oneshot(
        f"find {gcda_remote_dir} -name '*.gcda' -type f -delete",
        timeout=60,
    )
    if result.status != Status.Success:
        logger.warning(
            "Failed to clean .gcda files on %s: %s", label, result.output,
        )
    else:
        logger.info("Cleaned .gcda files on %s", label)


async def _fetch_one_host(
    host: RemoteHost,
    gcda_remote_dir: str,
    staging_root: Path,
) -> Path | None:
    """Discover and download .gcda files for a single host.

    Returns the per-host staging directory on success, or ``None`` if
    no files were found or the transfer failed.
    """
    label = host.id
    dest = staging_root / label
    dest.mkdir(parents=True, exist_ok=True)

    logger.info("Discovering .gcda files on %s:%s", label, gcda_remote_dir)
    find_result = await host.oneshot(
        f"find {gcda_remote_dir} -name '*.gcda' -type f",
        timeout=60,
    )
    if find_result.status != Status.Success or not find_result.output.strip():
        logger.warning("No .gcda files found on %s at %s", label, gcda_remote_dir)
        return None

    gcda_files = [
        Path(line.strip())
        for line in find_result.output.strip().splitlines()
        if line.strip().endswith(".gcda")
    ]
    if not gcda_files:
        logger.warning("No .gcda files found on %s", label)
        return None

    logger.info("Fetching .gcda files from %s", label)
    status, msg = await host.get(gcda_files, dest, show_progress=False)
    if status != Status.Success:
        logger.error("Failed to fetch .gcda files from %s: %s", label, msg)
        return None

    return dest


class GcdaFetcher:
    """Fetch ``.gcda`` files from the configured lab hosts into a local staging area.

    Each host gets its own subdirectory under *staging_root* so files
    from different hosts never collide before the merge step::

        staging_root/
            host1_ne/
                foo.gcda
                subdir/bar.gcda
            host2_ne/
                foo.gcda

    Hosts are selected via :func:`all_hosts`, optionally filtered by a
    compiled regex *pattern* matched against each host's ``id``.
    """

    def __init__(
        self,
        staging_root: Path,
        pattern: re.Pattern[str] | None = None,
    ) -> None:
        self.staging_root = staging_root
        self.pattern = pattern

    async def fetch_all(self, gcda_remote_dir: str) -> dict[str, Path]:
        """Fetch ``.gcda`` files from every matching host concurrently.

        Args:
            gcda_remote_dir: Absolute path on each remote host where
                ``.gcda`` files are located (e.g. ``/var/coverage/myproduct``).

        Returns:
            Mapping of host id → local staging directory containing its
            fetched ``.gcda`` files.  Hosts with no files or failed
            transfers are omitted from the result.
        """
        self.staging_root.mkdir(parents=True, exist_ok=True)

        fetch_results = await do_for_all_hosts(
            _fetch_one_host,
            gcda_remote_dir,
            self.staging_root,
            pattern=self.pattern,
        )

        results: dict[str, Path] = {}
        for host_id, value in fetch_results.items():
            if isinstance(value, BaseException):
                logger.error("Failed to fetch from %s: %s", host_id, value)
                continue
            if value is not None:
                results[host_id] = value
        return results

    async def clean_remote(self, gcda_remote_dir: str) -> None:
        """Delete ``.gcda`` files from every matching remote host concurrently.

        Should be called **before** a test run to ensure clean coverage
        data, and optionally **after** collection to save disk space.
        """
        clean_results = await do_for_all_hosts(
            _clean_one_host,
            gcda_remote_dir,
            pattern=self.pattern,
        )
        for host_id, result in clean_results.items():
            if isinstance(result, BaseException):
                logger.warning(
                    "Failed to clean .gcda files on %s: %s", host_id, result,
                )
