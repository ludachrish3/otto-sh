"""CoverageReporter: merge, load, and render coverage from collected .gcda files.

This module replaces the old ``Pipeline`` class.  It does **not** fetch
``.gcda`` files (that is handled by ``otto test --cov`` via
:class:`~otto.coverage.fetcher.remote.GcdaFetcher`).  Instead it takes
directories of already-collected ``.gcda`` files, merges them with
``lcov``, loads the results into a :class:`CoverageStore`, and renders
an HTML report.

Coverage tiers are user-defined.  The reporter accepts an ordered list
of ``(tier_name, info_path)`` pairs where the order is the precedence
order — first entry has highest precedence in the renderer's
winner-take-all row coloring and column layout.  A ``None`` ``info_path``
marks the implicit *system* tier produced by merging the supplied
``.gcda`` directories with ``lcov``; only the tier named
:data:`~otto.coverage.store.model.TIER_SYSTEM` is allowed to omit a path.

Typical usage from the ``otto cov`` CLI command::

    reporter = CoverageReporter(
        gcda_dirs=[run1 / "cov" / "host1", run1 / "cov" / "host2"],
        source_root=Path("/home/me/myproduct"),
        output_dir=Path("./cov_report"),
        tiers=[("unit", Path("u.info")), ("system", None)],
    )
    store = await reporter.run()
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .correlator.lcov_loader import LCOVLoader
from .correlator.merger import LcovMerger
from .correlator.paths import (
    PathCorrelator,
    PathMapping,
    discover_from_gcno,
    discover_path_mappings,
)
from .renderer.html_renderer import HtmlRenderer
from .store.model import TIER_SYSTEM, CoverageStore

if TYPE_CHECKING:
    from ..host.toolchain import Toolchain

logger = logging.getLogger(__name__)


# An ordered tier specification: ``(tier_name, info_path_or_None)``.
# A ``None`` path is only valid for the implicit ``system`` tier, which
# is produced by merging the supplied ``.gcda`` directories with lcov.
TierSpec = tuple[str, Path | None]


def _read_cov_meta(cov_dirs: list[Path]) -> dict:
    """Read coverage metadata written by ``otto test --cov``.

    Looks for ``.otto_cov_meta.json`` directly inside each *cov_dir* and
    returns the parsed JSON from the first one found.

    Raises:
        FileNotFoundError: If no metadata file is found in any cov dir.
    """
    for cov_dir in cov_dirs:
        meta_path = cov_dir / '.otto_cov_meta.json'
        if meta_path.is_file():
            return json.loads(meta_path.read_text())
    raise FileNotFoundError(
        "No .otto_cov_meta.json found in any coverage directory. "
        "Re-run 'otto test --cov' to generate coverage metadata."
    )


def read_cov_source_root(cov_dirs: list[Path]) -> Path:
    """Read the source root from coverage metadata written by ``otto test --cov``.

    Looks for ``.otto_cov_meta.json`` in each *cov_dir*.  Returns the
    ``sut_dir`` from the first metadata file found.

    Args:
        cov_dirs: Coverage directories (each containing host subdirs and
            a ``.otto_cov_meta.json`` sidecar).

    Returns:
        The source root path.

    Raises:
        FileNotFoundError: If no metadata file is found in any cov dir.
    """
    meta = _read_cov_meta(cov_dirs)
    return Path(meta['sut_dir'])


def read_cov_toolchains(cov_dirs: list[Path]) -> dict[str, Toolchain]:
    """Read per-host toolchain info from coverage metadata.

    Returns a mapping of host directory name → :class:`Toolchain`.
    If no toolchain info is present in the metadata, returns an empty dict.
    """
    from ..host.toolchain import Toolchain

    try:
        meta = _read_cov_meta(cov_dirs)
    except FileNotFoundError:
        return {}

    raw_toolchains: dict = meta.get('toolchains', {})
    result: dict[str, Toolchain] = {}
    for host_id, tc_data in raw_toolchains.items():
        kwargs = {}
        for key in ('sysroot', 'lcov', 'gcov'):
            if key in tc_data:
                kwargs[key] = Path(tc_data[key])
        result[host_id] = Toolchain(**kwargs)
    return result


def discover_gcda_dirs(cov_dirs: list[Path]) -> list[Path]:
    """Collect all per-host .gcda directories from one or more cov/ directories.

    Each *cov_dir* is expected to contain per-host subdirectories::

        cov_dir/
          host_id_1/
          host_id_2/

    Returns:
        List of per-host directories containing ``.gcda`` files.
    """
    gcda_dirs: list[Path] = []
    for cov_dir in cov_dirs:
        if not cov_dir.is_dir():
            logger.warning("Coverage directory does not exist: %s", cov_dir)
            continue
        for host_dir in sorted(cov_dir.iterdir()):
            if host_dir.is_dir():
                gcda_dirs.append(host_dir)
    return gcda_dirs


class CoverageReporter:
    """Merge, load, and render coverage from pre-collected .gcda files.

    Args:
        gcda_dirs: Per-host directories containing ``.gcda`` files.
        source_root: Local source tree root (used for path mapping and
            .gcno discovery).
        output_dir: Directory for the HTML report output.
        project_name: Title shown in the HTML report.
        toolchains: Per-host toolchains keyed by host directory name.
            When a host has no entry the pipeline falls back to gcno-based
            auto-discovery, then to system defaults.
        tiers: Ordered list of ``(tier_name, info_path | None)`` pairs.
            Order is precedence order — first entry wins in the renderer's
            winner-take-all row coloring.  ``None`` paths are only valid
            for the ``system`` tier and indicate the implicit lcov-merged
            output of ``gcda_dirs``.  Defaults to ``[("system", None)]``
            when omitted.
    """

    def __init__(
        self,
        gcda_dirs: list[Path],
        source_root: Path,
        output_dir: Path,
        project_name: str = "Coverage Report",
        toolchains: dict[str, Toolchain] | None = None,
        tiers: list[TierSpec] | None = None,
    ) -> None:
        self.gcda_dirs = gcda_dirs
        self.source_root = source_root
        self.output_dir = output_dir
        self.project_name = project_name
        self.toolchains = toolchains or {}
        self.tiers: list[TierSpec] = list(tiers) if tiers else [(TIER_SYSTEM, None)]
        self._validate_tiers()

    def _validate_tiers(self) -> None:
        seen: set[str] = set()
        for name, path in self.tiers:
            if name in seen:
                raise ValueError(f"Duplicate tier name: {name!r}")
            seen.add(name)
            if path is None and name != TIER_SYSTEM:
                raise ValueError(
                    f"Tier {name!r} has no .info path; only the "
                    f"{TIER_SYSTEM!r} tier may omit a path."
                )

    async def _resolve_toolchains(self, work_dir: Path) -> list[Toolchain | None]:
        """Build a per-gcda-dir list of toolchains.

        Resolution order for each directory:
        1. Explicit toolchain from ``self.toolchains`` (matched by dir name)
        2. Auto-discovery from ``.gcno`` files in the source root
        3. ``None`` (merger will use its own defaults)
        """
        from ..host.localHost import LocalHost
        from ..host.toolchain_discovery import discover_toolchain_from_gcno

        result: list[Toolchain | None] = []
        discovered_fallback: Toolchain | None = None
        fallback_computed = False

        for gcda_dir in self.gcda_dirs:
            host_id = gcda_dir.name
            if host_id in self.toolchains:
                result.append(self.toolchains[host_id])
                continue

            # Lazy auto-discovery: run once and cache
            if not fallback_computed:
                localhost = LocalHost()
                try:
                    discovered_fallback = await discover_toolchain_from_gcno(
                        self.source_root, localhost, work_dir,
                    )
                finally:
                    await localhost.close()
                fallback_computed = True

            result.append(discovered_fallback)

        return result

    def _wants_system_tier(self) -> bool:
        return any(name == TIER_SYSTEM and path is None for name, path in self.tiers)

    async def run(self) -> CoverageStore:
        """Execute the coverage merge, load, and render pipeline.

        Returns:
            A populated :class:`CoverageStore` with all coverage data.
        """
        from ..host.localHost import LocalHost

        localhost = LocalHost()
        work_dir = self.output_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            tier_order = [name for name, _ in self.tiers]
            store = CoverageStore(tier_order=tier_order)

            wants_system = self._wants_system_tier()

            if wants_system and not self.gcda_dirs:
                logger.warning(
                    "No .gcda directories provided but 'system' tier requested; "
                    "system tier will be empty."
                )

            system_info: Path | None = None
            mappings: list[PathMapping] = []

            if wants_system and self.gcda_dirs:
                # 0. Resolve per-host toolchains
                toolchain_list = await self._resolve_toolchains(work_dir)

                # 1. Merge across hosts using lcov
                logger.info(
                    "=== Merging coverage across %d host(s) ===", len(self.gcda_dirs)
                )
                merger = LcovMerger(localhost)
                filtered_toolchains = [t for t in toolchain_list if t is not None]
                system_info = await merger.capture_and_merge(
                    host_gcda_dirs=self.gcda_dirs,
                    gcno_dir=self.source_root,
                    work_dir=work_dir,
                    toolchains=filtered_toolchains if filtered_toolchains else None,
                )

                # 2. Auto-discover path mappings
                logger.info("=== Auto-discovering path mappings ===")
                mappings = await discover_path_mappings(
                    system_info, self.source_root, localhost
                )

            if not mappings:
                mappings = await discover_from_gcno(
                    self.source_root, self.source_root, localhost
                )
            if not mappings:
                logger.warning(
                    "Could not auto-discover path mappings. "
                    "Embedded paths will be used as-is."
                )

            # 3. Load coverage into store
            logger.info("=== Loading coverage into store ===")
            correlator = PathCorrelator(mappings)
            loader = LCOVLoader(store, correlator)

            for tier_name, tier_path in self.tiers:
                if tier_path is None:
                    # Implicit system tier from the merged .gcda pipeline
                    if system_info is not None:
                        loader.load(system_info, tier_name)
                    else:
                        # Still register so the renderer shows the column
                        store.register_tier(tier_name)
                else:
                    if not tier_path.exists():
                        logger.warning(
                            "Tier %r .info file not found: %s — skipping",
                            tier_name, tier_path,
                        )
                        store.register_tier(tier_name)
                        continue
                    loader.load(tier_path, tier_name)

            # 4. Render HTML
            logger.info("=== Rendering HTML report ===")
            renderer = HtmlRenderer(self.output_dir, project_name=self.project_name)
            renderer.render(store)

            store.save(self.output_dir / "store.json")

            logger.info("Done. Report: %s", self.output_dir / "index.html")
            return store

        finally:
            await localhost.close()


async def run_coverage_report(
    cov_dirs: list[Path],
    report_dir: Path,
    project_name: str = "Coverage Report",
    tier_specs: list[TierSpec] | None = None,
) -> CoverageStore | None:
    """Render an HTML coverage report from one or more cov/ directories.

    Shared entry point used by both ``otto cov report`` (multiple cov dirs,
    one per run) and ``otto test --cov-report`` (single cov dir produced by
    the test run just completed).

    Reads the source root and per-host toolchains from
    ``.otto_cov_meta.json`` inside the cov dirs, discovers per-host gcda
    subdirs, and runs :class:`CoverageReporter`.

    Returns:
        The populated :class:`CoverageStore`, or ``None`` if no coverage
        data is available (no metadata file or no gcda subdirs found).
    """
    try:
        source_root = read_cov_source_root(cov_dirs)
    except FileNotFoundError as e:
        logger.warning("%s", e)
        return None

    gcda_dirs = discover_gcda_dirs(cov_dirs)
    if not gcda_dirs:
        logger.warning(
            "No per-host .gcda directories found in: %s",
            ", ".join(str(d) for d in cov_dirs),
        )
        return None

    toolchains = read_cov_toolchains(cov_dirs)

    reporter = CoverageReporter(
        gcda_dirs=gcda_dirs,
        source_root=source_root,
        output_dir=report_dir,
        project_name=project_name,
        toolchains=toolchains,
        tiers=tier_specs,
    )
    return await reporter.run()
