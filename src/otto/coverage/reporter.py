"""CoverageReporter: merge, load, and render coverage from collected .gcda files.

This module replaces the old ``Pipeline`` class.  It does **not** fetch
``.gcda`` files (that is handled by ``otto test --cov`` via
:class:`~otto.coverage.fetcher.remote.GcdaFetcher`).  Instead it takes
directories of already-collected ``.gcda`` files, merges them with
``lcov``, loads the results into a :class:`~otto.coverage.store.model.CoverageStore`, and renders
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

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .merge.lcov_loader import LCOVLoader
from .merge.merger import LcovMerger
from .merge.paths import (
    PathMapping,
    PathRemapper,
    discover_from_gcno,
    discover_path_mappings,
)
from .store.model import TIER_SYSTEM, CoverageStore, Thresholds, TicketRecord

if TYPE_CHECKING:
    from ..host.local_host import LocalHost
    from ..host.toolchain import Toolchain
    from .capture.model import Capture
    from .overrides import OverrideConfig
    from .tickets import TicketSpec
    from .tiers import TierConfig

logger = logging.getLogger(__name__)


TierSpec = tuple[str, Path | None]
"""An ordered coverage tier specification: ``(tier_name, info_path_or_None)``.

A ``None`` path is only valid for the implicit ``system`` tier, which is
produced by merging the supplied ``.gcda`` directories with lcov.
"""


@dataclass(frozen=True)
class CollectionInputs:
    """The collection-model inputs to :class:`CoverageReporter` (Task 10).

    All fields are optional; an all-default instance (the constructor
    default) selects the legacy, purely ``.gcda``-driven behavior — every
    collection-model step becomes a no-op.

    - ``repo_root``: SUT git repo root.  Enables e2e captures + the manual
      store; also the base_commit-guard reference and the exclusion source.
    - ``tier_configs``: Declared coverage tiers (precedence order).  Seeds
      ``tier_order`` / ``tier_colors`` and drives unit-harvest.
    - ``capture_paths``: ``capture.json`` files (one per board) to fold in
      under their own tier, subject to the HEAD base_commit guard.
    - ``extra_markers``: Extra source exclusion markers (spec §8).
    """

    repo_root: Path | None = None
    tier_configs: "list[TierConfig]" = field(default_factory=list)
    capture_paths: list[Path] = field(default_factory=list)
    extra_markers: list[str] = field(default_factory=list)


def _read_cov_meta(cov_dirs: list[Path]) -> dict[str, Any]:
    """Read coverage metadata written by ``otto test --cov``.

    Looks for ``.otto_cov_meta.json`` directly inside each *cov_dir* and
    returns the parsed JSON from the first one found.

    Raises:
        FileNotFoundError: If no metadata file is found in any cov dir.
    """
    for cov_dir in cov_dirs:
        meta_path = cov_dir / ".otto_cov_meta.json"
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
    return Path(meta["sut_dir"])


def read_cov_source_roots(cov_dirs: list[Path]) -> dict[str, Path]:
    """Read per-host source roots from coverage metadata.

    Returns a mapping of host directory name → source root :class:`Path`.
    If no source_roots info is present in the metadata, or if no metadata
    file is found, returns an empty dict.
    """
    try:
        meta = _read_cov_meta(cov_dirs)
    except FileNotFoundError:
        return {}

    raw: dict[str, str] = meta.get("source_roots", {})
    return {host_id: Path(v) for host_id, v in raw.items()}


def read_cov_toolchains(cov_dirs: list[Path]) -> "dict[str, Toolchain]":
    """Read per-host toolchain info from coverage metadata.

    Returns a mapping of host directory name → :class:`~otto.host.toolchain.Toolchain`.
    If no toolchain info is present in the metadata, returns an empty dict.
    """
    from ..host.toolchain import Toolchain

    try:
        meta = _read_cov_meta(cov_dirs)
    except FileNotFoundError:
        return {}

    raw_toolchains: dict[str, Any] = meta.get("toolchains", {})
    result: "dict[str, Toolchain]" = {}
    for host_id, tc_data in raw_toolchains.items():
        kwargs = {}
        for key in ("sysroot", "lcov", "gcov"):
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
        gcda_dirs.extend(host_dir for host_dir in sorted(cov_dir.iterdir()) if host_dir.is_dir())
    return gcda_dirs


class CoverageReporter:
    """Merge, load, and render coverage from pre-collected .gcda files.

    Args:
        gcda_dirs: Per-host directories containing ``.gcda`` files.
        source_root: Local source tree root (used for path mapping and
            .gcno discovery).  Acts as the fallback for hosts that have
            no entry in *source_roots*.
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
        source_roots: Per-host source roots keyed by gcda-dir name (i.e.
            the host id).  When provided, each host's ``.gcda`` files are
            captured against its own ``.gcno`` directory instead of the
            shared *source_root* fallback.  Hosts with no entry fall back
            to *source_root*.
        collection: The collection-model inputs (e2e captures, unit
            harvest, manual store, exclusion markers).  Omitted / an
            all-default :class:`CollectionInputs` selects the legacy,
            purely ``.gcda``-driven behavior — every new step is a no-op.
        prefix: Strip this leading directory from file paths *shown* in
            the report (display only, like ``genhtml --prefix``).  Files
            outside the prefix display unchanged; links and store keys
            always use the full path.
        thresholds: Render thresholds from ``[coverage.report]``; defaults
            to ``Thresholds()``.
        ticket_spec: Compiled ``[coverage.tickets]`` pattern (Task 1). ``None``
            (the default) is the "feature absent" signal: no git log walk
            runs, no line or ``store.tickets`` entry is annotated, and the
            coverage numbers themselves are unchanged. When set,
            ``_annotate_tickets`` attributes every stored line to the
            ticket(s) named by the commit that last touched it.
        overrides: Parsed ``.otto/coverage-overrides.toml``, as an
            :class:`~otto.coverage.overrides.OverrideConfig` (overrides spec
            §2/§3/§6). ``None`` (the default) is the "feature absent"
            signal: no asserted entries fold in and no reattribution
            reaches ``attribute_tickets``. When set, applied after
            ``_annotate_tickets`` in ``run()`` §3c/§3d, since asserted-entry
            resolution needs the final per-line sha map and ticket->commits
            map that attribution produces.
    """

    def __init__(  # noqa: PLR0913 — wide constructor: each field is an independently-optional report input
        self,
        gcda_dirs: list[Path],
        source_root: Path,
        output_dir: Path,
        project_name: str = "Coverage Report",
        toolchains: "dict[str, Toolchain] | None" = None,
        tiers: list[TierSpec] | None = None,
        source_roots: dict[str, Path] | None = None,
        *,
        collection: CollectionInputs | None = None,
        prefix: Path | None = None,
        thresholds: "Thresholds | None" = None,
        ticket_spec: "TicketSpec | None" = None,
        overrides: "OverrideConfig | None" = None,
    ) -> None:
        self.gcda_dirs = gcda_dirs
        self.source_root = source_root
        self.output_dir = output_dir
        self.project_name = project_name
        self.toolchains = toolchains or {}
        self.tiers: list[TierSpec] = list(tiers) if tiers else [(TIER_SYSTEM, None)]
        self.source_roots: dict[str, Path] = source_roots or {}
        coll = collection or CollectionInputs()
        self.repo_root: Path | None = coll.repo_root
        self.tier_configs: "list[TierConfig]" = list(coll.tier_configs)
        self.capture_paths: list[Path] = list(coll.capture_paths)
        self.extra_markers: list[str] = list(coll.extra_markers)
        self.prefix = prefix
        self.thresholds: Thresholds = thresholds or Thresholds()
        self.ticket_spec: "TicketSpec | None" = ticket_spec
        self.overrides: "OverrideConfig | None" = overrides
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

    def _per_host_gcno_dirs(self) -> list[Path]:
        """Per-gcda-dir source root: the host's own root (by dir name) or ``source_root`` fallback.

        Parallel to ``self.gcda_dirs``.
        """
        return [self.source_roots.get(d.name, self.source_root) for d in self.gcda_dirs]

    def _resolve_toolchains(self) -> "list[Toolchain | None]":
        """Build a per-gcda-dir list of toolchains.

        Resolution order for each directory:
        1. Explicit toolchain from ``self.toolchains`` (matched by dir name)
        2. Auto-discovery from ``.gcno`` files in the source root
        3. ``None`` (merger will use its own defaults)
        """
        from ..host.toolchain_discovery import discover_toolchain_from_gcno

        result: "list[Toolchain | None]" = []
        discovered_fallback: "Toolchain | None" = None
        fallback_computed = False

        for gcda_dir in self.gcda_dirs:
            host_id = gcda_dir.name
            if host_id in self.toolchains:
                result.append(self.toolchains[host_id])
                continue

            # Lazy auto-discovery: run once and cache, against the shared
            # source_root. Note: a host with its own ``source_roots`` entry
            # (a different build tree) but no explicit ``toolchains`` entry
            # would get a toolchain sniffed from this fallback root, which may
            # be wrong. The metadata writer (``collect_coverage``) emits a per-host
            # toolchain whenever it emits a per-host source root, so this path
            # is not reached for per-version embedded hosts.
            if not fallback_computed:
                discovered_fallback = discover_toolchain_from_gcno(self.source_root)
                fallback_computed = True

            result.append(discovered_fallback)

        return result

    def _wants_system_tier(self) -> bool:
        return any(name == TIER_SYSTEM and path is None for name, path in self.tiers)

    async def run(self) -> CoverageStore:
        """Execute the coverage merge, load, and render pipeline.

        Returns:
            A populated :class:`~otto.coverage.store.model.CoverageStore` with all coverage data.
        """
        from ..host.local_host import LocalHost

        localhost = LocalHost()
        work_dir = self.output_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Seed tier order from the declared tiers (precedence order)
            # first, so the renderer's winner-take-all row coloring follows
            # settings precedence even for a tier that has no data yet;
            # then fold in any legacy ``--tier`` specs not already present.
            tier_order = [t.name for t in self.tier_configs]
            for name, _ in self.tiers:
                if name not in tier_order:
                    tier_order.append(name)
            store = CoverageStore(tier_order=tier_order)
            store.thresholds = self.thresholds
            # Manual-overrides spec §7: "active" tracks the override FILE's
            # presence (parsed successfully into an OverrideConfig), not
            # whether it happens to carry any asserted entries — a
            # reattribute-only file must still export overrides_active=true
            # with an empty overrides list (F2, final review). Set up front
            # rather than inside the attribution/apply branch below so it
            # stays correct even if that branch never runs.
            store.overrides_file_active = self.overrides is not None

            wants_system = self._wants_system_tier()

            if wants_system and not self.gcda_dirs and not self.capture_paths:
                logger.warning(
                    "No .gcda directories provided but 'system' tier requested; "
                    "system tier will be empty."
                )

            system_info: Path | None = None
            mappings: list[PathMapping] = []

            if wants_system and self.gcda_dirs:
                # 0. Resolve per-host toolchains
                toolchain_list = self._resolve_toolchains()

                # 1. Merge across hosts using lcov
                logger.info("=== Merging coverage across %d host(s) ===", len(self.gcda_dirs))
                merger = LcovMerger(localhost)
                # The list is positional (parallel to gcda_dirs) — hosts
                # without a toolchain stay as None entries so a mixed bed
                # (e.g. one clang product, one default-gcov host) keeps each
                # host paired with its own toolchain.
                has_toolchains = any(t is not None for t in toolchain_list)
                system_info = await merger.capture_and_merge(
                    host_gcda_dirs=self.gcda_dirs,
                    gcno_dir=self.source_root,
                    work_dir=work_dir,
                    toolchains=toolchain_list if has_toolchains else None,
                    gcno_dirs=self._per_host_gcno_dirs() if self.source_roots else None,
                )

                # 2. Auto-discover path mappings
                logger.info("=== Auto-discovering path mappings ===")
                mappings = await discover_path_mappings(system_info, self.source_root, localhost)

            if not mappings:
                mappings = await discover_from_gcno(self.source_root, self.source_root, localhost)
            if not mappings:
                logger.warning(
                    "Could not auto-discover path mappings. Embedded paths will be used as-is."
                )

            # 3. Load coverage into store
            logger.info("=== Loading coverage into store ===")
            remapper = PathRemapper(mappings)
            loader = LCOVLoader(store, remapper)

            for tier_name, tier_path in self.tiers:
                if tier_path is None:
                    # Implicit system tier from the merged .gcda pipeline
                    if system_info is not None:
                        run_id = store.add_run(tier=tier_name)
                        loader.load(system_info, tier_name, run_id=run_id)
                    else:
                        # Still register so the renderer shows the column
                        store.register_tier(tier_name)
                else:
                    if not tier_path.exists():
                        logger.warning(
                            "Tier %r .info file not found: %s — skipping",
                            tier_name,
                            tier_path,
                        )
                        store.register_tier(tier_name)
                        continue
                    run_id = store.add_run(tier=tier_name)
                    loader.load(tier_path, tier_name, run_id=run_id)

            # 3b. Collection-model inputs (Task 10). No-ops for legacy
            #     callers: every step is gated on the new constructor
            #     arguments being present. Cross-source dedupe is a
            #     *key* hoist, not a load-order hoist: the manual store's
            #     run keys are computed up front and seeded into
            #     ``seen_runs`` before anything folds, so a verbatim
            #     cov-dir duplicate of an already-committed manual run is
            #     skipped before the base_commit guard gets a chance to raise —
            #     but the manual captures themselves still fold LAST.
            #     That preserves ``apply_manual_capture``'s stale guard
            #     (it reads all tiers' hits at fold time): a line the e2e
            #     capture or unit harvest already covers is not marked
            #     stale just because the manual anchor chain couldn't
            #     verify its own copy.
            #
            #     ``seen_runs`` is seeded from *every* committed manual
            #     capture, not just supersede's winners: a cov-dir copy of
            #     a since-superseded capture must still be recognized and
            #     skipped by key here, or it falls through to the e2e
            #     base_commit guard as an unrecognized run (crash once the
            #     tree has moved past its base_commit, silent double-count
            #     if base_commit still equals HEAD). Only the winners fold
            #     into the store below.
            from .capture.supersede import select_manual_captures

            all_manual_captures = self._manual_captures()
            manual_captures = select_manual_captures(all_manual_captures)
            seen_runs: set[tuple[str, str, str, str]] = set()
            for cap in all_manual_captures:
                seen_runs.add(self._run_key(cap))
            self._load_captures(store, seen_runs)
            await self._harvest_unit_tiers(localhost, work_dir, loader)
            self._load_manual_store(store, manual_captures)
            self._fill_tier_colors(store)

            # 3c. Per-ticket attribution (design §6): a no-op unless both
            # ticket_spec is configured and repo_root is known (only the
            # collection-model path resolves one — the git-less --tier
            # escape hatch never does). Must run after every fold above so
            # the store's final line set is what gets attributed, and
            # before rendering so the SPA data build sees populated
            # LineRecord.ticket / store.tickets.
            if self.repo_root is not None:
                attribution = self._annotate_tickets(store, self.repo_root)
                # 3d. Manual-testing overrides (overrides spec §3): asserted
                # entries fold in after attribution so the line->sha map and
                # ticket->commits map they resolve against are final.
                if self.overrides is not None and attribution is not None:
                    self._apply_overrides(store, self.repo_root, *attribution)

            # 4. Render the SPA report. Exclusion display is render-time (spec
            # §8/§9): a single-valued LineRecord.state can't express "excluded
            # always wins" over covered/stale/aging, so the reporter never
            # bakes state="excluded" into the store — it just forwards the
            # extra marker strings for the renderer's own per-file source scan.
            logger.info("=== Rendering coverage report ===")
            # Deferred so importing this module (pulled onto the CLI startup
            # path via cli.cov) does not drag in SpaRenderer's transitive
            # imports (spa_data, and through it colors/exclusions) —
            # enforced by the import-budget guard
            # (tests/unit/import_budget/test_import_budget.py).
            from .renderer.spa_renderer import SpaRenderer

            renderer = SpaRenderer(
                self.output_dir,
                project_name=self.project_name,
                extra_markers=self.extra_markers,
                prefix=self.prefix,
            )
            renderer.render(store)

            store.save(self.output_dir / "store.json")

            logger.info("Done. Report: %s", self.output_dir / "index.html")
            return store

        finally:
            await localhost.close()

    # ------------------------------------------------------------------
    # Collection-model steps (Task 10) — each is a no-op unless the
    # relevant constructor argument is supplied.
    # ------------------------------------------------------------------

    @staticmethod
    def _run_key(capture: "Capture") -> tuple[str, str, str, str]:
        """Dedupe key: one run-table entry per distinct capture across all capture sources."""
        return (capture.tier, capture.base_commit, capture.board, capture.captured_at)

    def _load_captures(
        self, store: CoverageStore, seen_runs: set[tuple[str, str, str, str]]
    ) -> None:
        """Fold e2e board ``capture.json`` files in under a strict HEAD base_commit guard.

        Each capture is anchored to the exact commit (``base_commit``)
        whose line numbering it means; unlike a manual capture it carries
        no anchor chain, so it is only valid against a matching tree.  A
        ``base_commit`` that differs from HEAD is a hard error (the
        product was rebuilt or the tree moved after the capture was
        taken).

        The guard proves ``capture.base_commit == HEAD`` but says nothing
        about the *working tree*: a dirty tree at report time means the
        renderer reads edited on-disk source while the capture holds HEAD
        coordinates, silently misaligning every hit past a local edit.
        When the tree is dirty each capture is remapped HEAD → working
        tree (hits on locally-modified lines dropped) so the annotation
        lines up.

        *seen_runs* is the cross-source dedupe set, pre-seeded by the
        caller (:meth:`run`) with every committed manual capture's run
        key before this method runs: a cov-dir capture that duplicates an
        already-committed manual run is skipped here, before the
        base_commit guard gets a chance to raise — even though the manual
        copy itself does not fold into the store until
        :meth:`_load_manual_store` runs later.
        """
        if not self.capture_paths or self.repo_root is None:
            return
        from .capture import gitio
        from .capture.model import Capture
        from .errors import CoverageDataMismatchError
        from .validity import (
            load_capture_into_store,
            load_dirty_capture_into_store,
            register_capture_run,
        )

        head = gitio.head_commit(self.repo_root)
        dirty = gitio.is_dirty(self.repo_root)
        if dirty:
            logger.warning(
                "SUT repo %s has uncommitted changes; e2e capture hits are being "
                "remapped from HEAD to the working tree, and hits on locally-modified "
                "lines are omitted.",
                self.repo_root,
            )
        for cap_path in self.capture_paths:
            capture = Capture.load(cap_path)
            key = self._run_key(capture)
            if key in seen_runs:
                logger.info("Skipping duplicate capture %s (already folded)", cap_path)
                continue
            seen_runs.add(key)
            run_id = register_capture_run(store, capture)
            if capture.base_commit != head:
                raise CoverageDataMismatchError(
                    f"e2e capture {cap_path} was taken at {capture.base_commit[:12]} "
                    f"but the tree is at {head[:12]}; re-run the test or "
                    f"report from the matching commit"
                )
            if dirty:
                load_dirty_capture_into_store(store, capture, self.repo_root, run_id=run_id)
            else:
                load_capture_into_store(store, capture, self.repo_root, run_id=run_id)

    async def _harvest_unit_tiers(
        self,
        localhost: "LocalHost",
        work_dir: Path,
        loader: LCOVLoader,
    ) -> None:
        """Capture + load each unit tier's ``harvest_dirs`` (its own gcda == gcno root).

        Per spec §4, ``harvest_dirs`` entries are repo-relative: a relative
        entry is resolved against :attr:`repo_root`, not the process CWD
        (``otto cov report`` may run from anywhere). Absolute entries pass
        through unchanged.
        """
        unit_tiers = [t for t in self.tier_configs if t.kind == "unit" and t.harvest_dirs]
        if not unit_tiers:
            return
        from .merge.merger import LcovMerger

        merger = LcovMerger(localhost)
        for tier in unit_tiers:
            tier_run_id: int | None = None
            for idx, raw_hdir in enumerate(tier.harvest_dirs):
                hdir = (
                    self.repo_root / raw_hdir
                    if self.repo_root is not None and not raw_hdir.is_absolute()
                    else raw_hdir
                )
                if not hdir.is_dir():
                    logger.warning(
                        "Unit tier %r: harvest dir does not exist: %s — skipping",
                        tier.name,
                        hdir,
                    )
                    continue
                gcda_files = list(hdir.rglob("*.gcda"))
                if not gcda_files:
                    logger.warning(
                        "Unit tier %r: harvest dir has no .gcda files: %s — skipping",
                        tier.name,
                        hdir,
                    )
                    continue
                self._warn_if_stale_counters(tier.name, hdir, gcda_files)
                info_out = work_dir / f"unit_{tier.name}_{idx}.info"
                if tier_run_id is None:
                    tier_run_id = loader.store.add_run(tier=tier.name)
                await merger.capture(hdir, hdir, info_out)
                loader.load(info_out, tier.name, run_id=tier_run_id)

    @staticmethod
    def _warn_if_stale_counters(tier_name: str, hdir: Path, gcda_files: list[Path]) -> None:
        """Warn (but still load) when counters look older than the build notes."""
        gcno_files = list(hdir.rglob("*.gcno"))
        if not gcno_files:
            return
        newest_gcda = max(p.stat().st_mtime for p in gcda_files)
        newest_gcno = max(p.stat().st_mtime for p in gcno_files)
        if newest_gcda < newest_gcno:
            logger.warning(
                "Unit tier %r: newest .gcda under %s predates newest .gcno — "
                "counters may be stale (loading anyway).",
                tier_name,
                hdir,
            )

    def _manual_captures(self) -> "list[Capture]":
        """Load every committed manual capture under ``repo_root``.

        Returns ``[]`` when ``repo_root`` is unset. Split out from
        :meth:`_load_manual_store` so :meth:`run` can seed the cross-source
        dedupe set with these captures' run keys *before* e2e captures and
        unit harvest fold in, while still folding the manual captures
        themselves last (see :meth:`run`'s step-3b comment).
        """
        if self.repo_root is None:
            return []
        from .capture.store_dir import load_manual_captures

        return list(load_manual_captures(self.repo_root))

    def _load_manual_store(self, store: CoverageStore, manual_captures: "list[Capture]") -> None:
        """Fold every committed manual capture in with report-time validity states.

        Folds LAST — after e2e captures and unit harvest — so
        ``apply_manual_capture``'s stale guard (which reads all tiers'
        hits already folded into the line at fold time) only marks a line
        stale when no other already-folded run covers it; an e2e capture
        or unit harvest that hits the same line suppresses the stale mark
        even though the manual anchor chain itself couldn't verify it.

        Cross-source dedupe against e2e/unit runs already happened before
        this method runs (:meth:`run` seeds ``seen_runs`` with these
        captures' keys ahead of :meth:`_load_captures`); this method only
        needs to dedupe *within* ``manual_captures`` itself, so two
        identical committed captures still fold once.
        """
        if self.repo_root is None or not manual_captures:
            return
        from .validity import apply_manual_capture, register_capture_run

        max_age_by_tier = {t.name: t.max_age_days for t in self.tier_configs}
        seen: set[tuple[str, str, str, str]] = set()
        for capture in manual_captures:
            key = self._run_key(capture)
            if key in seen:
                logger.info("Skipping duplicate manual capture %s (already folded)", key)
                continue
            seen.add(key)
            run_id = register_capture_run(store, capture)
            apply_manual_capture(
                store,
                capture,
                self.repo_root,
                max_age_days=max_age_by_tier.get(capture.tier),
                run_id=run_id,
            )

    def _fill_tier_colors(self, store: CoverageStore) -> None:
        """Seed ``store.tier_colors`` from the declared tiers (name → color)."""
        for tier in self.tier_configs:
            store.tier_colors[tier.name] = tier.color

    def _annotate_tickets(
        self, store: CoverageStore, repo_root: Path
    ) -> tuple[dict[str, dict[int, str]], dict[str, list[str]]] | None:
        """Attribute every stored line to the ticket(s) that last touched it.

        A no-op when :attr:`ticket_spec` is ``None`` (the feature-absent
        signal — no git log walk runs) or when the store holds no files
        under *repo_root* to attribute. Files outside *repo_root* have no
        history to attribute against it and are skipped rather than
        crashing the run.

        Every owned line stays represented (spec §4/§6.1/§7): a line whose
        commit named a real ticket gets those id(s); a line whose commit
        matched no configured pattern gets the synthetic
        :data:`~otto.coverage.attribution.NO_TICKET` id; a line that is a
        working-tree edit never committed gets
        :data:`~otto.coverage.attribution.UNCOMMITTED_TICKET`. Both
        sentinels get a :class:`TicketRecord` with ``url=None``, exactly
        like any other id, so they flow through the store, the export, and
        the SPA unchanged — the collision guard that keeps a real commit
        from silently merging into either bucket lives in
        :func:`~otto.coverage.attribution.attribute_tickets` (a matched id
        equal to a reserved sentinel is dropped there, not here).

        Returns:
            ``(per_line_sha, commits)`` on success — the raw per-line owning
            sha map and the raw ticket id -> commit shas map from
            :func:`~otto.coverage.attribution.attribute_tickets` (not the
            coverable-filtered ``store.tickets`` view; override as_of
            bounding needs every walked commit of a ticket) — for
            :meth:`_apply_overrides` to resolve asserted entries against.
            ``None`` on either early-out.
        """
        if self.ticket_spec is None:
            return None
        # Deferred: this is the only caller that needs a git log walk, and
        # keeping the import out of module scope means a report with no
        # ``[coverage.tickets]`` configured never pulls in attribution's
        # transitive imports (mirrors SpaRenderer's deferred import below).
        from .attribution import NO_TICKET, UNCOMMITTED, UNCOMMITTED_TICKET, attribute_tickets

        # The store keys files by resolved absolute path (get_or_create_file
        # resolves whenever the file exists on disk); resolve repo_root the
        # same way before every is_relative_to/relative_to comparison below —
        # otherwise a repo_root that reaches the tree through a symlink
        # component would wrongly read every file in it as "outside the
        # repo" (mirrors capture/model.py's identical repo_root.resolve()
        # ahead of its own is_relative_to check).
        repo_root = repo_root.resolve()
        line_counts = {
            path.relative_to(repo_root).as_posix(): max(rec.lines)
            for rec in store.files()
            for path in [rec.path]
            if rec.lines and rec.path.is_relative_to(repo_root)
        }
        if not line_counts:
            return None
        per_line, commits, per_line_sha = attribute_tickets(
            repo_root,
            line_counts,
            self.ticket_spec,
            reattributions=self.overrides.reattributions if self.overrides else None,
        )

        # Real shas that own at least one no-ticket-sentinel line, and
        # whether any line was assigned the uncommitted sentinel — tracked
        # here so the TicketRecord entries below are only created when at
        # least one line actually needs them.
        no_ticket_shas: set[str] = set()
        saw_uncommitted = False
        # `line_counts` above is keyed by each file's *highest* stored line
        # number, not by which lines are actually coverable — a covered
        # file with lines {1, 5} still walks (and attributes) lines 2-4, so
        # `commits` (built over every walked line) can name a ticket whose
        # only touched lines are these gaps. Track which ticket ids own at
        # least one line that survives the `lineno not in rec.lines` filter
        # below, so a ticket owning zero coverable lines gets no
        # :class:`TicketRecord` at all — one wouldn't appear anywhere in
        # the SPA or the export anyway, since both read per-line data.
        tickets_with_coverable_lines: set[str] = set()

        for rec in store.files():
            if not rec.path.is_relative_to(repo_root):
                continue
            relpath = rec.path.relative_to(repo_root).as_posix()
            line_shas = per_line_sha.get(relpath, {})
            for lineno, ids in per_line.get(relpath, {}).items():
                if lineno not in rec.lines:
                    continue
                if ids:
                    rec.lines[lineno].ticket = list(ids)
                    tickets_with_coverable_lines.update(ids)
                    continue
                # `per_line_sha` is built from the exact same (relpath,
                # lineno) pairs as `per_line` (attribute_tickets populates
                # both in one pass over its internal `by_sha`), so this
                # lookup always hits — either UNCOMMITTED or a real sha,
                # never absent.
                sha = line_shas[lineno]
                if sha == UNCOMMITTED:
                    rec.lines[lineno].ticket = [UNCOMMITTED_TICKET]
                    saw_uncommitted = True
                else:
                    rec.lines[lineno].ticket = [NO_TICKET]
                    no_ticket_shas.add(sha)

        for ticket_id, shas in commits.items():
            if ticket_id not in tickets_with_coverable_lines:
                continue
            store.tickets[ticket_id] = TicketRecord(
                id=ticket_id, url=self.ticket_spec.url_for(ticket_id), commits=shas
            )
        if no_ticket_shas:
            store.tickets[NO_TICKET] = TicketRecord(
                id=NO_TICKET, url=None, commits=sorted(no_ticket_shas)
            )
        if saw_uncommitted:
            store.tickets[UNCOMMITTED_TICKET] = TicketRecord(
                id=UNCOMMITTED_TICKET, url=None, commits=[]
            )

        return per_line_sha, commits

    def _apply_overrides(
        self,
        store: CoverageStore,
        repo_root: Path,
        per_line_sha: dict[str, dict[int, str]],
        ticket_commits: dict[str, list[str]],
    ) -> None:
        """Fold the override file's asserted entries into the store (spec §3).

        One extra constant-cost subprocess (`rev-list --first-parent`) for
        the as_of ordering — never per entry or per file, and skipped
        entirely (F4, final review) when there are no asserted entries to
        bound at all: a reattribute-only override file has nothing for
        `fp_index` to serve (`apply_asserted_entries` early-returns on an
        empty entry list regardless), so paying for the walk would be pure
        waste on the (common, break-glass-only) reattribute-only path.
        """
        from .overrides import apply_asserted_entries

        assert self.overrides is not None  # noqa: S101 — caller gates on it
        if not self.overrides.asserted:
            return
        from .capture.gitio import rev_list_first_parent

        fp_index = {sha: i for i, sha in enumerate(rev_list_first_parent(repo_root))}
        apply_asserted_entries(
            store,
            self.overrides.asserted,
            repo_root=repo_root,
            per_line_sha=per_line_sha,
            ticket_commits=ticket_commits,
            fp_index=fp_index,
            path=self.overrides.path,
        )


def _partition_board_dirs(cov_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    """Split each cov dir's board subdirs into (gcda dirs, capture.json paths).

    A board dir holding a ``capture.json`` is an e2e capture already
    anchored to base_commit and loads via the capture path; every other
    board dir keeps today's ``.gcda``-merge path (back-compat).  Mixed
    cov dirs are supported.
    """
    gcda_dirs: list[Path] = []
    capture_paths: list[Path] = []
    for cov_dir in cov_dirs:
        if not cov_dir.is_dir():
            logger.warning("Coverage directory does not exist: %s", cov_dir)
            continue
        for board_dir in sorted(cov_dir.iterdir()):
            if not board_dir.is_dir():
                continue
            capture_json = board_dir / "capture.json"
            if capture_json.is_file():
                capture_paths.append(capture_json)
            else:
                gcda_dirs.append(board_dir)
    return gcda_dirs, capture_paths


async def run_coverage_report(  # noqa: PLR0913 — wide entry point: each kwarg is an independently-optional report input
    cov_dirs: list[Path],
    output_dir: Path,
    project_name: str = "Coverage Report",
    tier_specs: list[TierSpec] | None = None,
    *,
    repo_root: Path | None = None,
    tier_configs: "list[TierConfig] | None" = None,
    extra_markers: list[str] | None = None,
    prefix: Path | None = None,
    thresholds: "Thresholds | None" = None,
    ticket_spec: "TicketSpec | None" = None,
    overrides: "OverrideConfig | None" = None,
) -> CoverageStore | None:
    """Render an HTML coverage report from one or more cov/ directories.

    Shared entry point used by both ``otto cov report`` (multiple cov dirs,
    one per run) and ``otto test --cov-report`` (single cov dir produced by
    the test run just completed).

    **Two modes, one precedence rule.**  When neither *repo_root* nor
    *tier_configs* is given the function runs the **legacy** path unchanged:
    it reads the source root + per-host toolchains from
    ``.otto_cov_meta.json``, discovers per-host gcda subdirs, and merges
    them with lcov — returning ``None`` (as before) when there is no
    metadata sidecar or no gcda subdirs.

    When *repo_root* or *tier_configs* is given the function runs the
    **collection-model** path, which additionally consumes, in order:

    1. **e2e captures** — board dirs holding a ``capture.json`` load under a
       strict HEAD base_commit guard (:class:`~otto.coverage.errors.CoverageDataMismatchError`
       on mismatch); board dirs without one keep the legacy gcda-merge.
    2. **unit harvest** — each ``kind == "unit"`` tier's ``harvest_dirs``.
    3. **manual store** — every committed capture under *repo_root*'s
       ``.otto/coverage/manual/`` (with report-time validity states).

    Crucially the legacy "no metadata → return ``None``" early-outs do
    **not** fire in this mode: an empty *cov_dirs* plus a non-empty manual
    store still yields a report.  The legacy gcda-merge only targets the
    conventional ``system`` tier via *tier_specs* (default ``[("system",
    None)]``); explicit ``--tier`` specs, being a git-less escape hatch,
    never reach this mode (the CLI routes them through the legacy path).

    *thresholds* is the render thresholds from ``[coverage.report]``;
    ``None`` (the default) selects :class:`~otto.coverage.store.model.Thresholds`'s
    own 80.0/70.0 defaults. Forwarded unchanged to both the legacy and
    collection-model paths, which each pass it straight through to
    :class:`CoverageReporter`.

    *ticket_spec* is the compiled ``[coverage.tickets]`` pattern (Task 1);
    ``None`` (the default) is the feature-absent signal — no git log walk
    runs. Ticket attribution needs a git *repo_root* to walk, which only the
    collection-model path resolves, so *ticket_spec* is forwarded there only;
    a caller that supplies it without *repo_root*/*tier_configs* falls into
    the legacy branch and it is silently ignored (mirroring that branch's
    "byte-for-byte the historical behavior" contract).

    *overrides* is the parsed ``.otto/coverage-overrides.toml``, as an
    :class:`~otto.coverage.overrides.OverrideConfig` (overrides spec §2);
    ``None`` (the default) is the feature-absent signal. Like
    *ticket_spec*, it only takes effect on the collection-model path — a
    caller supplying it without *repo_root*/*tier_configs* falls into the
    legacy branch and it is silently ignored.

    Returns:
        The populated :class:`~otto.coverage.store.model.CoverageStore`, or
        ``None`` when the legacy path found no coverage data.
    """
    if repo_root is None and tier_configs is None:
        return await _run_legacy_report(
            cov_dirs, output_dir, project_name, tier_specs, prefix=prefix, thresholds=thresholds
        )

    return await _run_collection_report(
        cov_dirs,
        output_dir,
        project_name=project_name,
        tier_specs=tier_specs,
        repo_root=repo_root,
        tier_configs=tier_configs,
        extra_markers=extra_markers,
        prefix=prefix,
        thresholds=thresholds,
        ticket_spec=ticket_spec,
        overrides=overrides,
    )


async def _run_legacy_report(
    cov_dirs: list[Path],
    output_dir: Path,
    project_name: str,
    tier_specs: list[TierSpec] | None,
    *,
    prefix: Path | None = None,
    thresholds: "Thresholds | None" = None,
) -> CoverageStore | None:
    """Run the pre-collection-model path — byte-for-byte the historical behavior."""
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
    source_roots = read_cov_source_roots(cov_dirs)

    reporter = CoverageReporter(
        gcda_dirs=gcda_dirs,
        source_root=source_root,
        output_dir=output_dir,
        project_name=project_name,
        toolchains=toolchains,
        tiers=tier_specs,
        source_roots=source_roots,
        prefix=prefix,
        thresholds=thresholds,
    )
    return await reporter.run()


async def _run_collection_report(  # noqa: PLR0913 — wide internal helper: mirrors run_coverage_report's kwargs
    cov_dirs: list[Path],
    output_dir: Path,
    *,
    project_name: str,
    tier_specs: list[TierSpec] | None,
    repo_root: Path | None,
    tier_configs: "list[TierConfig] | None",
    extra_markers: list[str] | None,
    prefix: Path | None = None,
    thresholds: "Thresholds | None" = None,
    ticket_spec: "TicketSpec | None" = None,
    overrides: "OverrideConfig | None" = None,
) -> CoverageStore:
    """Run the collection-model path: captures + unit harvest + manual store.

    Always returns a store (never ``None``): even with no gcda dirs and no
    captures, the manual store and the declared tiers still produce a
    report.
    """
    gcda_dirs, capture_paths = _partition_board_dirs(cov_dirs)

    # The gcda-merge fallback needs a source root only when there are
    # legacy (non-capture) board dirs to merge.  When the meta sidecar is
    # absent — e.g. a captures-only or manual-only run — fall back to the
    # repo root (or cwd) rather than bailing out the way the legacy path
    # would.
    try:
        source_root = read_cov_source_root(cov_dirs)
    except FileNotFoundError:
        source_root = repo_root or Path.cwd()

    toolchains = read_cov_toolchains(cov_dirs)
    source_roots = read_cov_source_roots(cov_dirs)

    reporter = CoverageReporter(
        gcda_dirs=gcda_dirs,
        source_root=source_root,
        output_dir=output_dir,
        project_name=project_name,
        toolchains=toolchains,
        tiers=tier_specs,
        source_roots=source_roots,
        collection=CollectionInputs(
            repo_root=repo_root,
            tier_configs=list(tier_configs) if tier_configs else [],
            capture_paths=capture_paths,
            extra_markers=list(extra_markers) if extra_markers else [],
        ),
        prefix=prefix,
        thresholds=thresholds,
        ticket_spec=ticket_spec,
        overrides=overrides,
    )
    return await reporter.run()
