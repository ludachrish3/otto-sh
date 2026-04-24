"""Merge gcda files from multiple hosts using lcov.

Wraps ``lcov --capture`` and ``lcov --add-tracefile`` invocations,
executing them through :class:`~otto.host.localHost.LocalHost` so
they are fully async with proper logging and timeout handling.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ...host.localHost import LocalHost
from ...utils import Status

if TYPE_CHECKING:
    from ...host.toolchain import Toolchain

logger = logging.getLogger(__name__)


def _find_gcno_dirs(gcda_dir: Path, search_root: Path) -> list[Path]:
    """Find directories under *search_root* that contain ``.gcno`` files
    matching the ``.gcda`` basenames in *gcda_dir*.

    ``lcov --build-directory`` does not search recursively, so we need
    to locate the exact directories and pass each one.

    Returns:
        De-duplicated list of directories containing matching ``.gcno``
        files, or ``[search_root]`` as fallback.
    """
    gcda_stems = {p.stem for p in gcda_dir.glob('*.gcda')}
    if not gcda_stems:
        return [search_root]

    dirs: set[Path] = set()
    for gcno in search_root.rglob('*.gcno'):
        if gcno.stem in gcda_stems:
            dirs.add(gcno.parent)

    return sorted(dirs) if dirs else [search_root]


class LcovMerger:
    """Merge coverage using ``lcov --capture`` + ``lcov --add-tracefile``.

    Works with any GCC version.  Each host's gcda directory is captured
    into a ``.info`` file, then all ``.info`` files are merged.

    The *lcov* and *gcov* constructor arguments serve as **defaults**.
    Individual :meth:`capture` calls can override them via the
    *toolchain* parameter to support per-host toolchains.
    """

    def __init__(
        self,
        localhost: LocalHost,
        lcov: str = "lcov",
        gcov: str = "gcov",
    ) -> None:
        self.localhost = localhost
        self.lcov = lcov
        self.gcov = gcov

    async def capture(
        self,
        gcda_dir: Path,
        gcno_dir: Path,
        output: Path,
        toolchain: Toolchain | None = None,
    ) -> Path:
        """Run ``lcov --capture`` on a single host's gcda directory.

        Args:
            gcda_dir: Directory containing ``.gcda`` files (fetched from remote).
            gcno_dir: Directory containing ``.gcno`` files (from the build).
            output: Path for the output ``.info`` file.
            toolchain: Per-host toolchain override.  When provided, its
                ``lcov_bin`` and ``gcov_bin`` are used instead of the
                instance defaults.

        Returns:
            The *output* path on success.

        Raises:
            RuntimeError: If ``lcov --capture`` fails.
        """
        lcov = toolchain.lcov_bin if toolchain else self.lcov
        gcov = toolchain.gcov_bin if toolchain else self.gcov

        build_dirs = _find_gcno_dirs(gcda_dir, gcno_dir)
        build_args = " ".join(
            f"--build-directory {d}" for d in build_dirs
        )

        cmd = (
            f"{lcov} --capture"
            f" --directory {gcda_dir}"
            f" {build_args}"
            f" --gcov-tool {gcov}"
            f" --rc branch_coverage=1"
            f" --output-file {output}"
        )
        logger.info("lcov capture: %s -> %s", gcda_dir, output)
        result = await self.localhost.oneshot(cmd, timeout=300)
        if result.status != Status.Success:
            raise RuntimeError(f"lcov --capture failed:\n{result.output}")
        return output

    async def merge_info_files(
        self,
        info_files: list[Path],
        output: Path,
        toolchain: Toolchain | None = None,
    ) -> Path:
        """Merge pre-captured ``.info`` files using ``lcov --add-tracefile``.

        Args:
            info_files: List of ``.info`` files to merge.
            output: Path for the merged output ``.info`` file.
            toolchain: Optional toolchain override for the ``lcov`` binary.

        Returns:
            The *output* path on success.

        Raises:
            RuntimeError: If merging fails.
        """
        if not info_files:
            raise ValueError("No .info files to merge")

        lcov = toolchain.lcov_bin if toolchain else self.lcov

        add_args = " ".join(f"--add-tracefile {f}" for f in info_files)
        cmd = f"{lcov} {add_args} --rc branch_coverage=1 --output-file {output}"

        logger.info("lcov merge: %d files -> %s", len(info_files), output)
        result = await self.localhost.oneshot(cmd, timeout=300)
        if result.status != Status.Success:
            raise RuntimeError(f"lcov --add-tracefile failed:\n{result.output}")
        return output

    async def capture_and_merge(
        self,
        host_gcda_dirs: list[Path],
        gcno_dir: Path,
        work_dir: Path,
        toolchains: list[Toolchain] | None = None,
    ) -> Path:
        """Capture each host dir to ``.info``, then merge all.

        Args:
            host_gcda_dirs: Per-host directories containing ``.gcda`` files.
            gcno_dir: Directory containing ``.gcno`` files (from the build).
            work_dir: Scratch directory for intermediate ``.info`` files.
            toolchains: Per-host toolchains, parallel to *host_gcda_dirs*.
                Each entry can be ``None`` to use instance defaults.
                If the entire list is ``None``, defaults are used for all.

        Returns:
            Path to the merged ``.info`` file.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        info_files: list[Path] = []

        for i, gcda_dir in enumerate(host_gcda_dirs):
            tc = toolchains[i] if toolchains else None
            info_out = work_dir / f"host_{i}.info"
            await self.capture(gcda_dir, gcno_dir, info_out, toolchain=tc)
            info_files.append(info_out)

        if len(info_files) == 1:
            return info_files[0]

        merged = work_dir / "merged.info"
        return await self.merge_info_files(info_files, merged)
