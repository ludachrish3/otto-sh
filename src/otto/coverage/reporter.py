"""CoverageReporter: load and render coverage from captures, harvested tiers and tracefiles.

This module does **not** fetch ``.gcda`` files (``otto test --cov`` and ``otto
cov get`` do, via :func:`~otto.coverage.collect.collect_coverage`). It reads
each run's ``cov/<host>/<product>/`` directories, where every product is in
one of three states:

- **captured**: a ``capture.json`` anchored to the commit the counters were
  collected at — loaded as one run carrying its host and product;
- **failed**: a ``capture.failed`` holding the short reason — listed once in a
  warning and never retried here (``otto cov get`` fetches it afresh);
- **not yet captured**: neither file — captured now by the same code as
  collection (:func:`~otto.coverage.capture.produce.produce_captures`), with
  the tier and commit collection recorded in ``.otto_cov_meta.json``.

Beside the run directories it folds in each unit tier's harvested build
directories, the committed manual-capture store, and any ``--tier NAME=PATH``
tracefile, then renders an HTML report.

Coverage tiers are user-defined. The reporter accepts an ordered list of
``(tier_name, info_path)`` pairs where the order is the precedence order —
first entry has highest precedence in the renderer's winner-take-all row
coloring and column layout.

Typical usage::

    reporter = CoverageReporter(
        source_root=Path("/home/me/myproduct"),
        output_dir=Path("report"),
        tiers=[("unit", Path("u.info"))],
    )
    store = await reporter.run()
"""

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .merge.lcov_loader import LCOVLoader
from .merge.merger import LcovMerger
from .report_inputs import ReportInputs
from .store.model import CoverageStore, Thresholds, TicketRecord
from .tree import ProductKey, iter_product_dirs

if TYPE_CHECKING:
    from ..host.local_host import LocalHost
    from ..host.product import Product
    from ..host.toolchain import Toolchain
    from .capture.model import Capture
    from .exclusions.rules import ExclusionRule
    from .objects import ObjectListCache
    from .overrides import OverrideConfig
    from .sources import SourceResolver
    from .tickets import TicketSpec
    from .tiers import TierConfig
    from .unit_filter import UnitPlan, UnitView

logger = logging.getLogger(__name__)


TierSpec = tuple[str, Path | None]
"""An ordered coverage tier specification: ``(tier_name, info_path)``.

The path is a user-supplied lcov tracefile; its run records the tier only. A
``None`` path is refused (``--tier NAME`` with no ``=PATH``): every tier
needs its tracefile.
"""


@dataclass(frozen=True)
class CollectionInputs:
    """The collection-model inputs to :class:`CoverageReporter`.

    All fields are optional; an all-default instance (the constructor
    default) folds in only the reporter's ``tiers`` tracefiles — every
    collection-model step becomes a no-op.

    - ``project_root``: the project's git checkout root.  Enables e2e captures + the manual
      store; also the base_commit-guard reference and the exclusion source.
    - ``tier_configs``: Declared coverage tiers (precedence order).  Seeds
      ``tier_order`` / ``tier_colors`` and drives unit-harvest.
    - ``capture_paths``: ``capture.json`` files (one per host and product) to
      fold in under their own tier, subject to the HEAD base_commit guard.
    - ``exclusion_rules``: Compiled ``[coverage.exclusions]`` rules. The
      built-in ``LCOV_EXCL_*`` families always apply and are not listed here.
    - ``project_name``: The project whose products a unit tier reports.
    - ``objects``: The report's object-list cache, so every artifact is read
      once; a new one when omitted.
    - ``unit_plan``: The unit views, validated before anything was captured
      (:func:`~otto.coverage.unit_filter.plan_unit_views`); planned by
      :meth:`CoverageReporter.run` when omitted.
    """

    project_root: Path | None = None
    tier_configs: "list[TierConfig]" = field(default_factory=list)
    capture_paths: list[Path] = field(default_factory=list)
    exclusion_rules: "list[ExclusionRule]" = field(default_factory=list)
    project_name: str | None = None
    objects: "ObjectListCache | None" = None
    unit_plan: "UnitPlan | None" = None


_META_KEYS = ["project_name", "project_root", "tier", "base_commit"]
"""The keys ``otto test --cov`` writes into ``.otto_cov_meta.json`` and every reader needs."""
_OLD_META_KEYS = ["repo_name", "sut_dir"]
"""Keys an older otto wrote; metadata carrying one is refused, never translated.

The one place in ``src/`` that must spell the old keys. The vocabulary checks
exempt exactly this whole line, by ``REJECTION_INVENTORY`` in
``tests/_fixtures/vocabulary.py``; keep it byte for byte."""


def _read_cov_meta(cov_dirs: list[Path]) -> dict[str, Any]:
    """Read and validate every input's ``.otto_cov_meta.json``; return the first one.

    A directory without the file is skipped, as before. A file that carries an
    old key, or lacks a current one, is refused: reporting reads counters
    from every input, so one stale directory must not slip in behind a
    current first one.

    Raises:
        CoverageInputError: A metadata file is stale (``field="cov_dirs"``).
        FileNotFoundError: No input directory has a metadata file.
    """
    from .errors import CoverageInputError

    first: "dict[str, Any] | None" = None
    for cov_dir in cov_dirs:
        meta_path = cov_dir / ".otto_cov_meta.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text())
        old = [k for k in _OLD_META_KEYS if k in meta]
        missing = [k for k in _META_KEYS if k not in meta]
        if old or missing:
            what = f"has the old key {old[0]!r}" if old else f"lacks the key {missing[0]!r}"
            raise CoverageInputError(
                f"{cov_dir}: coverage metadata {meta_path.name} {what} (written by an "
                "older otto); re-collect with 'otto test --cov' or 'otto cov get'",
                field="cov_dirs",
            )
        if first is None:
            first = meta
    if first is None:
        raise FileNotFoundError(
            "No .otto_cov_meta.json found in any coverage directory. "
            "Re-run 'otto test --cov' to generate coverage metadata."
        )
    return first


def read_cov_source_root(cov_dirs: list[Path]) -> Path:
    """Read the source root from coverage metadata written by ``otto test --cov``.

    Looks for ``.otto_cov_meta.json`` in each *cov_dir*.  Returns the
    ``project_root`` from the first metadata file found.

    Args:
        cov_dirs: Coverage directories (each containing host subdirs and
            a ``.otto_cov_meta.json`` sidecar).

    Returns:
        The source root path.

    Raises:
        CoverageInputError: A metadata file is stale (``field="cov_dirs"``).
        FileNotFoundError: If no metadata file is found in any cov dir.
    """
    meta = _read_cov_meta(cov_dirs)
    return Path(meta["project_root"])


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

    # dict[str, Any], not dict[str, str]: the metadata is unvalidated JSON, and
    # Path() below is what pins each value.
    raw: dict[str, Any] = meta.get("source_roots", {})
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
        # Only the three coverage paths and ``lcov_args`` are serialized into
        # cov metadata — a toolchain's installable ``tools`` are not — so start
        # from the defaults and replace whichever of them the metadata carries.
        # (Unpacking a ``dict[str, Path]`` straight into the constructor no
        # longer type-checks now that not every field is a Path.)
        paths = {key: Path(tc_data[key]) for key in ("sysroot", "lcov", "gcov") if key in tc_data}
        # ``lcov_args`` travels too: it is what the capture of this host's
        # data has to carry, and a deferred report builds that command here.
        extra = {"lcov_args": list(tc_data["lcov_args"])} if "lcov_args" in tc_data else {}
        result[host_id] = replace(Toolchain(), **paths, **extra)
    return result


class CoverageReporter:
    """Load and render coverage from captures, harvested unit tiers and tracefiles.

    Args:
        source_root: The tree exclusion markers are read from when no
            ``collection.project_root`` is known (a tracefile-only report).
        output_dir: Directory for the HTML report output.
        report_title: Title shown in the HTML report.
        tiers: Ordered list of ``(tier_name, info_path)`` pairs, each a
            user-supplied lcov tracefile. Order is precedence order — first
            entry wins in the renderer's winner-take-all row coloring. Each
            run records the tier only.
        collection: The collection-model inputs (e2e captures, unit
            harvest, manual store, exclusion markers). Omitted / an
            all-default :class:`CollectionInputs` folds in only *tiers*.
        prefix: Strip this leading directory from file paths *shown* in
            the report (display only, like ``genhtml --prefix``).  Files
            outside the prefix display unchanged; links and store keys
            always use the full path.
        thresholds: Render thresholds from ``[coverage.report]``; defaults
            to ``Thresholds()``.
        ticket_spec: Compiled ``[coverage.tickets]`` pattern. ``None``
            (the default) is the "feature absent" signal: no git log walk
            runs, no line or ``store.tickets`` entry is annotated, and the
            coverage numbers themselves are unchanged. When set,
            ``_annotate_tickets`` attributes every stored line to the
            ticket(s) named by the commit that last touched it.
        overrides: Parsed ``.otto/coverage-overrides.toml``, as an
            :class:`~otto.coverage.overrides.OverrideConfig`. ``None`` (the
            default) is the "feature absent" signal: no asserted entries
            fold in and no reattribution reaches ``attribute_tickets``.
            When set, applied after ``_annotate_tickets`` in ``run()``,
            since asserted-entry resolution needs the final per-line sha map
            and ticket->commits map that attribution produces.

    Raises:
        ValueError: A tier is named twice or has no tracefile path.
    """

    def __init__(
        self,
        source_root: Path,
        output_dir: Path,
        report_title: str = "Coverage Report",
        tiers: list[TierSpec] | None = None,
        *,
        collection: CollectionInputs | None = None,
        prefix: Path | None = None,
        thresholds: "Thresholds | None" = None,
        ticket_spec: "TicketSpec | None" = None,
        overrides: "OverrideConfig | None" = None,
    ) -> None:
        self.source_root = source_root
        self.output_dir = output_dir
        self.report_title = report_title
        self.tiers: list[TierSpec] = list(tiers or [])
        coll = collection or CollectionInputs()
        self.project_root: Path | None = coll.project_root
        self.tier_configs: "list[TierConfig]" = list(coll.tier_configs)
        self.capture_paths: list[Path] = list(coll.capture_paths)
        self.exclusion_rules: "list[ExclusionRule]" = list(coll.exclusion_rules)
        self.project_name: str | None = coll.project_name
        self.objects: "ObjectListCache | None" = coll.objects
        self.unit_plan: "UnitPlan | None" = coll.unit_plan
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
            if path is None:
                raise ValueError(f"Tier {name!r} has no .info path; every tier needs one.")

    async def run(self) -> CoverageStore:
        """Load every input into one store, render the report, and return the store.

        Returns:
            A populated :class:`~otto.coverage.store.model.CoverageStore` with all coverage data.
        """
        from ..host.connections import teardown_step
        from ..host.local_host import LocalHost
        from .sources import SourceResolver

        localhost = LocalHost()
        work_dir = self.output_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Seed tier order from the declared tiers (precedence order)
            # first, so the renderer's winner-take-all row coloring follows
            # settings precedence even for a tier that has no data yet;
            # then fold in any ``--tier`` tracefiles not already present.
            tier_order = [t.name for t in self.tier_configs]
            for name, _ in self.tiers:
                if name not in tier_order:
                    tier_order.append(name)
            store = CoverageStore(tier_order=tier_order)
            store.thresholds = self.thresholds
            # "Active" tracks the override FILE's presence (parsed
            # successfully into an OverrideConfig), not whether it happens to
            # carry any asserted entries — a reattribute-only file must still
            # export overrides_active=true with an empty overrides list. Set
            # up front rather than inside the attribution/apply branch below
            # so it stays correct even if that branch never runs.
            store.overrides_file_active = self.overrides is not None

            # Tracefiles and harvested tiers name their sources as lcov wrote
            # them; each is resolved to its project file, and the resolver
            # alone logs one it cannot place.
            # It shares the unit plan's walk of the project tree, when there is one.
            resolver = (
                SourceResolver(
                    self.project_root,
                    index_from=self.unit_plan.resolver if self.unit_plan is not None else None,
                )
                if self.project_root is not None
                else None
            )
            loader = LCOVLoader(store, resolver)
            for tier_name, tier_path in self.tiers:
                if tier_path is None or not tier_path.exists():
                    logger.warning(
                        "Tier %r .info file not found: %s — skipping", tier_name, tier_path
                    )
                    store.register_tier(tier_name)
                    continue
                run_id = store.add_run(tier=tier_name)
                loader.load(tier_path, tier_name, run_id=run_id)

            # Collection-model inputs, each a no-op without its constructor
            # argument. Cross-source dedupe is a *key* hoist, not a
            # load-order hoist: the manual store's run keys are computed up
            # front and seeded into ``seen_runs`` before anything folds, so
            # a verbatim cov-dir duplicate of an already-committed manual run
            # is skipped before the base_commit guard gets a chance to raise
            # — but the manual captures themselves still fold LAST. That
            # preserves ``apply_manual_capture``'s stale guard (it reads all
            # tiers' hits at fold time): a line the e2e capture or unit
            # harvest already covers is not marked stale just because the
            # manual anchor chain couldn't verify its own copy.
            #
            # ``seen_runs`` is seeded from *every* committed manual capture,
            # not just supersede's winners: a cov-dir copy of a
            # since-superseded capture must still be recognized and skipped
            # by key here, or it falls through to the e2e base_commit guard
            # as an unrecognized run (crash once the tree has moved past its
            # base_commit, silent double-count if base_commit still equals
            # HEAD). Only the winners fold into the store below.
            from .capture.supersede import select_manual_captures

            all_manual_captures = self._manual_captures()
            manual_captures = select_manual_captures(all_manual_captures)
            seen_runs: set[tuple[str, str, str, str, str]] = set()
            for cap in all_manual_captures:
                seen_runs.add(self._run_key(cap))
            self._load_captures(store, seen_runs)
            await self._harvest_unit_tiers(localhost, work_dir, loader)
            self._load_manual_store(store, manual_captures)
            self._fill_tier_colors(store)

            # Exclusion filter. Position is load-bearing three ways: after
            # every fold (one pass covers captures, harvested tiers,
            # tracefiles and the manual store); after the dirty-tree remap in
            # _load_captures (so the line numbers deleted here share the
            # working-tree coordinate space of the source being scanned); and
            # before attribution (so an excluded line never reaches
            # per-ticket coverage or override resolution).
            #
            # Runs unconditionally: the built-in LCOV_EXCL_* families apply
            # even with no configured rules, which is how otto enforces the
            # standard markers on harvested tracefiles that geninfo never
            # touched. Deferred import keeps the package off the `otto cov
            # --help` path (import-budget guard), same as SpaRenderer below.
            from .exclusions.apply import apply_exclusions

            apply_exclusions(store, self.exclusion_rules, self.project_root or self.source_root)

            # Per-ticket attribution: a no-op unless both ticket_spec is
            # configured and project_root is known (a tracefile-only report
            # never resolves one). Must run after every fold above so the
            # store's final line set is what gets attributed, and before
            # rendering so the SPA data build sees populated
            # LineRecord.ticket / store.tickets.
            if self.project_root is not None:
                attribution = self._annotate_tickets(store, self.project_root)
                # Manual-testing overrides: asserted entries fold in after
                # attribution so the line->sha map and ticket->commits map
                # they resolve against are final.
                if self.overrides is not None and attribution is not None:
                    self._apply_overrides(store, self.project_root, *attribution)

            # Render the SPA report. Exclusion is not a render-time concern:
            # the filter stage above already deleted the excluded records and
            # annotated FileRecord.excluded_lines, so the renderer reads that
            # annotation instead of scanning source itself.
            logger.info("=== Rendering coverage report ===")
            # Deferred so importing this module does not drag in
            # SpaRenderer's transitive imports (spa_data, and through it
            # colors); a row in tests/unit/test_import_contracts.py pins it.
            from .renderer.spa_renderer import SpaRenderer

            renderer = SpaRenderer(
                self.output_dir,
                report_title=self.report_title,
                prefix=self.prefix,
            )
            renderer.render(store)

            store.save(self.output_dir / "store.json")

            logger.info("Done. Report: %s", self.output_dir / "index.html")
            return store

        finally:
            with teardown_step("coverage report", "localhost close"):
                await localhost.close()

    # ------------------------------------------------------------------
    # Collection-model steps — each is a no-op unless the
    # relevant constructor argument is supplied.
    # ------------------------------------------------------------------

    @staticmethod
    def _run_key(capture: "Capture") -> tuple[str, str, str, str, str]:
        """Dedupe key: one run-table entry per distinct capture across all capture sources."""
        return _capture_run_key(capture)

    def _load_captures(
        self, store: CoverageStore, seen_runs: set[tuple[str, str, str, str, str]]
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
        if not self.capture_paths or self.project_root is None:
            return
        from .capture import gitio
        from .capture.model import Capture
        from .errors import CaptureCommitError
        from .validity import (
            load_capture_into_store,
            load_dirty_capture_into_store,
            register_capture_run,
        )

        head = gitio.head_commit(self.project_root)
        dirty = gitio.is_dirty(self.project_root)
        if dirty:
            logger.warning(
                "Project root %s has uncommitted changes; e2e capture hits are being "
                "remapped from HEAD to the working tree, and hits on locally-modified "
                "lines are omitted.",
                self.project_root,
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
                raise CaptureCommitError(
                    capture=str(cap_path), taken_at=capture.base_commit, head=head
                )
            if dirty:
                load_dirty_capture_into_store(store, capture, self.project_root, run_id=run_id)
            else:
                load_capture_into_store(store, capture, self.project_root, run_id=run_id)

    async def _harvest_unit_tiers(
        self,
        localhost: "LocalHost",
        work_dir: Path,
        loader: LCOVLoader,
    ) -> None:
        """Capture + load each unit view's directories: its products' sources only.

        ``harvest_dirs`` fold into one unnamed run (``product=""``); each
        ``products`` entry gets its own run, named after the product. Each
        view's directories, their kept sources and their toolchains come from
        the unit plan (:class:`CollectionInputs`), planned here when none was
        given: the tier's own ``toolchain`` setting over what each
        directory's stamps choose.
        """
        if not any(t.kind == "unit" and (t.harvest_dirs or t.products) for t in self.tier_configs):
            return
        if self.project_root is None:
            logger.warning("Unit tiers need the project root to read its products; skipping them")
            return
        from .merge.merger import LcovMerger
        from .sources import SourceResolver

        plan = self.unit_plan
        if plan is None:
            from .objects import ObjectListCache
            from .pairing import GcnoIndex
            from .unit_filter import plan_unit_views

            plan = await plan_unit_views(
                self.tier_configs,
                self.project_root,
                self.project_name,
                self.objects or ObjectListCache(self.project_root),
                # The unit build's notes name system and gtest headers by the dozen.
                SourceResolver(self.project_root, quiet=True, index_from=loader.resolver),
                GcnoIndex(self.project_root),
            )
        with LcovMerger(localhost) as merger:
            for view in plan.views:
                await self._harvest_one(
                    self.project_root, view, work_dir, merger, loader, plan.resolver
                )

    async def _harvest_one(
        self,
        project_root: Path,
        view: "UnitView",
        work_dir: Path,
        merger: "LcovMerger",
        loader: LCOVLoader,
        resolver: "SourceResolver",
    ) -> None:
        """Capture + load one unit view's directories into one run, keeping product sources only.

        Each directory the plan kept is captured in place with its resolved
        toolchain and one tail-glob ``--include`` per kept file, so nothing
        else reaches ``geninfo``, then filtered exactly on the resolved
        ``SF:`` paths. The run id is created lazily, on the first directory
        captured — a view whose every directory is missing, empty or keeps
        nothing registers no run.

        Raises:
            otto.coverage.errors.CaptureFailedError: a capture failed, or one of
                a non-empty kept source set yielded no record (a reader that read
                nothing); either names the view and the tier's setting.
            otto.coverage.errors.CoverageToolVersionError: the reader cannot
                read the build's format; its remedy names the tier's setting.
        """
        from .errors import CaptureFailedError, CoverageToolVersionError
        from .unit_filter import filter_info, include_patterns
        from .unit_toolchain import setting_key

        key = setting_key(view.tier, "coverage_tool")
        run_id: int | None = None
        for unit_dir in view.captures:
            suffix = f"_{view.product}" if view.product else ""
            raw_info = work_dir / f"unit_{view.tier}{suffix}_{unit_dir.index}.raw.info"
            info_out = work_dir / f"unit_{view.tier}{suffix}_{unit_dir.index}.info"
            if run_id is None:
                run_id = loader.store.add_run(tier=view.tier, product=view.product)
            kept = list(unit_dir.selection.kept)
            try:
                await merger.capture_dir(
                    unit_dir.path,
                    raw_info,
                    toolchain=unit_dir.toolchain,
                    includes=include_patterns(kept),
                )
            except CoverageToolVersionError as e:
                raise CoverageToolVersionError(
                    e.detail, setting=f"`{key}`", subject="the tested code"
                ) from e
            except CaptureFailedError as e:
                raise CaptureFailedError(
                    f"unit tier {view.label}: capturing {unit_dir.path} failed; if the counter "
                    f"reader is the wrong one, set `{key}`.\n{e}"
                ) from e
            if not _has_records(raw_info):
                raise CaptureFailedError(
                    f"unit tier {view.label}: the capture of {unit_dir.path} with "
                    f"{unit_dir.toolchain.gcov_bin} recorded no source file, though {len(kept)} "
                    "product source file(s) are kept: the reader read nothing (a reader of "
                    f"another compiler's data does this). Check `{key}`."
                )
            filter_info(raw_info, info_out, set(kept), project_root, resolver)
            loader.load(info_out, view.tier, run_id=run_id)

    def _manual_captures(self) -> "list[Capture]":
        """Load every committed manual capture under ``project_root``.

        Returns ``[]`` when ``project_root`` is unset. Split out from
        :meth:`_load_manual_store` so :meth:`run` can seed the cross-source
        dedupe set with these captures' run keys *before* e2e captures and
        unit harvest fold in, while still folding the manual captures
        themselves last (see :meth:`run`'s step-3b comment).
        """
        if self.project_root is None:
            return []
        from .capture.store_dir import load_manual_captures

        return list(load_manual_captures(self.project_root))

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
        if self.project_root is None or not manual_captures:
            return
        from .validity import apply_manual_capture, register_capture_run

        max_age_by_tier = {t.name: t.max_age_days for t in self.tier_configs}
        seen: set[tuple[str, str, str, str, str]] = set()
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
                self.project_root,
                max_age_days=max_age_by_tier.get(capture.tier),
                run_id=run_id,
            )

    def _fill_tier_colors(self, store: CoverageStore) -> None:
        """Seed ``store.tier_colors`` from the declared tiers (name → color)."""
        for tier in self.tier_configs:
            store.tier_colors[tier.name] = tier.color

    def _annotate_tickets(
        self, store: CoverageStore, project_root: Path
    ) -> tuple[dict[str, dict[int, str]], dict[str, list[str]]] | None:
        """Attribute every stored line to the ticket(s) that last touched it.

        A no-op when :attr:`ticket_spec` is ``None`` (the feature-absent
        signal — no git log walk runs) or when the store holds no files
        under *project_root* to attribute. Files outside *project_root* have no
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
        # resolves whenever the file exists on disk); resolve project_root the
        # same way before every is_relative_to/relative_to comparison below —
        # otherwise a project_root that reaches the tree through a symlink
        # component would wrongly read every file in it as "outside the
        # project root" (mirrors capture/model.py's identical project_root.resolve()
        # ahead of its own is_relative_to check).
        project_root = project_root.resolve()
        line_counts = {
            path.relative_to(project_root).as_posix(): max(rec.lines)
            for rec in store.files()
            for path in [rec.path]
            if rec.lines and rec.path.is_relative_to(project_root)
        }
        if not line_counts:
            return None
        per_line, commits, per_line_sha = attribute_tickets(
            project_root,
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
            if not rec.path.is_relative_to(project_root):
                continue
            relpath = rec.path.relative_to(project_root).as_posix()
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
        project_root: Path,
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

        fp_index = {sha: i for i, sha in enumerate(rev_list_first_parent(project_root))}
        apply_asserted_entries(
            store,
            self.overrides.asserted,
            project_root=project_root,
            per_line_sha=per_line_sha,
            ticket_commits=ticket_commits,
            fp_index=fp_index,
            path=self.overrides.path,
        )


@dataclass(frozen=True)
class _ProductStates:
    """One cov dir's product dirs, by capture state, each in walk order."""

    captured: "list[ProductKey]"
    """Holding a ``capture.json``."""
    failed: "list[ProductKey]"
    """Holding a ``capture.failed``: listed once, never retried here."""
    pending: "list[ProductKey]"
    """Holding neither: not yet captured."""


def _classify_product_dirs(cov_dir: Path) -> _ProductStates:
    """Sort *cov_dir*'s ``<host>/<product>`` dirs into captured, failed and pending.

    A dir holding no ``.gcda`` and no capture marker has nothing to capture
    and is in no list. A host dir holding data directly (the pre-product
    one-level tree) is refused by name, not misread as a product.
    """
    states = _ProductStates(captured=[], failed=[], pending=[])
    for host_id, product, product_dir in iter_product_dirs(cov_dir):
        key = ProductKey(host_id, product)
        if (product_dir / "capture.json").is_file():
            states.captured.append(key)
        elif (product_dir / "capture.failed").is_file():
            states.failed.append(key)
        elif next(product_dir.rglob("*.gcda"), None) is not None:
            states.pending.append(key)
        else:
            logger.debug("%s: no .gcda and no capture marker; nothing to capture", product_dir)
    return states


def _has_records(info: Path) -> bool:
    """Return whether the tracefile *info* records any source file."""
    try:
        with info.open() as f:
            return any(line.startswith("SF:") for line in f)
    except FileNotFoundError:
        return False


def _declared_products(keys: "list[ProductKey]") -> "dict[ProductKey, Product]":
    """Map each of *keys* to its product's definition on that key's host in the active lab.

    Read off the hosts as declared: no artifact is scanned for
    instrumentation. Each key gets its own host's definition, since two
    hosts may carry different builds under one product name. A product with
    no definition (no active lab, or a host the lab no longer carries) gets
    no entry and is paired by path alone.
    """
    from ..run_context import try_get_context

    ctx = try_get_context()
    if ctx is None:
        return {}
    products: "dict[ProductKey, Product]" = {}
    for key in keys:
        host = ctx.lab.hosts.get(key.host)
        for product in host.products if host is not None else []:
            if product.name == key.product:
                products.setdefault(key, product)
    return products


def _pending_meta(cov_dir: Path) -> dict[str, Any]:
    """Return the metadata a not-yet-captured cov dir is captured with.

    Raises:
        CoverageInputError: *cov_dir* has no metadata, or one without the tier
            or commit collection recorded (``field="cov_dirs"``).
    """
    from .errors import CoverageInputError

    try:
        return _read_cov_meta([cov_dir])
    except FileNotFoundError:
        raise CoverageInputError(
            f"{cov_dir}: products not yet captured, and no .otto_cov_meta.json records the "
            "tier and commit they were collected at; re-collect with 'otto test --cov' or "
            "'otto cov get'",
            field="cov_dirs",
        ) from None


@dataclass(frozen=True)
class _RunInput:
    """One run directory's ``cov/``, read and refused before the report writes anything."""

    cov_dir: Path
    states: _ProductStates
    meta: "dict[str, Any] | None"
    """The metadata its pending products are captured with; ``None`` when none are pending."""


def _scan_run_inputs(cov_dirs: list[Path]) -> list[_RunInput]:
    """Read every cov dir's metadata and capture states, refusing a bad one.

    Raises:
        CoverageInputError: A cov dir's metadata is stale, or one with pending
            products records no tier or commit (``field="cov_dirs"``).
    """
    runs: list[_RunInput] = []
    for cov_dir in cov_dirs:
        if (cov_dir / ".otto_cov_meta.json").is_file():
            # Refuse a stale input up front, whatever state its products are in.
            _read_cov_meta([cov_dir])
        if not cov_dir.is_dir():
            logger.warning("Coverage directory does not exist: %s", cov_dir)
            continue
        states = _classify_product_dirs(cov_dir)
        meta = _pending_meta(cov_dir) if states.pending else None
        runs.append(_RunInput(cov_dir=cov_dir, states=states, meta=meta))
    return runs


def _capture_run_key(capture: "Capture") -> tuple[str, str, str, str, str]:
    """Return the dedupe key: one run-table entry per distinct capture across all sources."""
    return (
        capture.tier,
        capture.base_commit,
        capture.board,
        capture.product,
        capture.captured_at,
    )


def _refuse_moved_commits(runs: list[_RunInput], project_root: Path) -> None:
    """Refuse, before the destination is touched, any run input from another commit.

    The same two guards the report applies later — a pending product is
    captured at the commit its metadata records, and a ``capture.json``
    loads only at its own ``base_commit`` — checked up front so a refusal
    never follows ``--overwrite-dir`` clearing the previous report. A
    ``capture.json`` that duplicates a committed manual capture is exempt
    here as it is at load time: it is skipped there, never guarded.

    Raises:
        otto.coverage.errors.CaptureCommitError: A pending cov dir, or a
            capture, belongs to another commit than the tree's.
    """
    from .capture import gitio
    from .capture.model import Capture
    from .capture.store_dir import load_manual_captures
    from .errors import CaptureCommitError

    if not any(run.meta is not None or run.states.captured for run in runs):
        return
    head = gitio.head_commit(project_root)
    manual: set[tuple[str, str, str, str, str]] | None = None
    for run in runs:
        if run.meta is not None and run.meta["base_commit"] != head:
            raise CaptureCommitError(
                capture=str(run.cov_dir), taken_at=run.meta["base_commit"], head=head
            )
        for key in run.states.captured:
            path = run.cov_dir / key.host / key.product / "capture.json"
            capture = Capture.load(path)
            if capture.base_commit == head:
                continue
            if manual is None:
                manual = {_capture_run_key(c) for c in load_manual_captures(project_root)}
            if _capture_run_key(capture) not in manual:
                raise CaptureCommitError(capture=str(path), taken_at=capture.base_commit, head=head)


async def _capture_states(
    runs: list[_RunInput], project_root: Path, objects: "ObjectListCache"
) -> list[Path]:
    """Capture every not-yet-captured product; return every ``capture.json`` to load.

    Each cov dir's pending products are captured with the tier and commit
    its metadata records, through *objects*, the report's one object-list
    cache. A product that fails here, like one that failed at collection,
    is named in the one warning listing failed products, which gives the
    path of each ``capture.failed`` holding the detail. A failure new in
    this report also appends its detail there, since this warning is the
    only place it is shown; one from an earlier run was shown by that run.

    Raises:
        otto.coverage.errors.CaptureCommitError: A pending cov dir was
            collected at another commit than the tree's.
    """
    from .capture.produce import produce_captures
    from .errors import CoverageCaptureError

    captures: list[Path] = []
    failed: list[Path] = []
    details: list[str] = []
    for run in runs:
        cov_dir, states = run.cov_dir, run.states
        failed.extend(cov_dir / k.host / k.product for k in states.failed)
        if run.meta is not None:
            try:
                await produce_captures(
                    cov_dir,
                    tier=run.meta["tier"],
                    base_commit=run.meta["base_commit"],
                    project_root=project_root,
                    labs=[run.meta["project_name"]],
                    products=_declared_products(states.pending),
                    only=states.pending,
                    objects=objects,
                )
            except CoverageCaptureError as e:
                failed.extend(cov_dir / p.key.host / p.key.product for p in e.result.failed)
                details.extend(e.details)
            states = _classify_product_dirs(cov_dir)
        captures.extend(cov_dir / k.host / k.product / "capture.json" for k in states.captured)
    if failed:
        names = ", ".join(dict.fromkeys(ProductKey(d.parent.name, d.name).label for d in failed))
        reasons = ", ".join(str(d / "capture.failed") for d in failed)
        message = "\n\n".join([f"{names}: failed to capture; see {reasons}", *details])
        logger.warning(message)
    return captures


def _validate_report_inputs(
    cov_dirs: list[Path], tier_specs: list[TierSpec] | None, project_root: Path | None
) -> None:
    """Refuse unusable report inputs, by field, before anything is read.

    *cov_dirs* entries name a ``<run dir>/cov`` layout; the check is against
    the run directory (``cov_dir.parent``), not ``cov/`` itself, so a run
    without coverage data but with a committed manual store can still
    produce a report. Run directories need *project_root*: their captures
    are anchored to its commits. *tier_specs* mirrors the CLI's own
    ``--tier`` validation minus the flag text, since a library caller has no
    flag; tracefiles are reported alone, never together with run
    directories.
    """
    from .errors import CoverageInputError

    for cov_dir in cov_dirs:
        run_dir = cov_dir.parent
        if not run_dir.is_dir():
            raise CoverageInputError(
                f"output directory does not exist: {run_dir}", field="cov_dirs"
            )
    seen: set[str] = set()
    for name, path in tier_specs or []:
        if path is None:
            raise CoverageInputError(
                f"Tier {name!r} has no tracefile: every --tier needs NAME=PATH",
                field="tier_specs",
            )
        if name in seen:
            raise CoverageInputError(f"Duplicate tier name: {name!r}", field="tier_specs")
        seen.add(name)
    if cov_dirs and tier_specs:
        raise CoverageInputError(
            "--tier NAME=PATH reports tracefiles alone and cannot be combined with run "
            "directories; report the run directories without --tier",
            field="tier_specs",
        )
    if cov_dirs and project_root is None:
        raise CoverageInputError(
            "run directories need the project's git root, which a [coverage] section in the "
            "project's .otto/settings.toml names; tracefiles alone need none",
            field="cov_dirs",
        )


async def run_coverage_report(
    cov_dirs: list[Path],
    output_dir: Path,
    inputs: ReportInputs,
    *,
    report_title: str = "Coverage Report",
    tier_specs: list[TierSpec] | None = None,
    prefix: Path | None = None,
    overwrite: bool = False,
) -> CoverageStore:
    """Render an HTML coverage report from one or more cov/ directories.

    Shared entry point used by both ``otto cov report`` (multiple cov dirs,
    one per run) and ``otto test --cov-report`` (single cov dir produced by
    the test run just completed).

    *overwrite* clears a non-empty *output_dir*; without it a non-empty one
    raises :class:`~otto.coverage.config.DestinationError` before anything is
    read.

    The report consumes, in order:

    1. **tracefiles** — each *tier_specs* ``(name, path)``, a user-supplied
       lcov ``.info`` whose run records the tier only.
    2. **e2e captures** — each run directory's ``<host>/<product>/`` dirs.
       A ``capture.json`` loads under a strict HEAD base_commit guard
       (:class:`~otto.coverage.errors.CaptureCommitError` on mismatch); a
       ``capture.failed`` is named in one warning and not retried; a dir with
       neither is captured now with the tier and commit its
       ``.otto_cov_meta.json`` records. Every run carries its host and
       product.
    3. **unit harvest** — each ``kind == "unit"`` tier's ``harvest_dirs`` and
       ``products``, keeping only the sources of ``inputs.project_name``'s
       products in the active lab, each directory captured with the tier's
       own ``toolchain`` setting over what its stamps choose. Every unit view
       and every directory's toolchain is validated first — before the
       destination is touched and before any capture.
    4. **manual store** — every committed capture under
       ``inputs.project_root``'s ``.otto/coverage/manual/`` (with report-time
       validity states).

    Run directories need ``inputs.project_root``; a report from tracefiles
    alone does not. An empty *cov_dirs* plus a non-empty manual store still
    yields a report.

    Every other field of *inputs* — ``exclusion_rules``, ``thresholds``,
    ``ticket_spec``, ``overrides`` — is documented on
    :class:`~otto.coverage.report_inputs.ReportInputs` itself: what config
    table it comes from, and what its feature-absent default means. Ticket
    attribution and overrides need ``project_root`` to walk. The exclusion
    filter always runs, because the built-in ``LCOV_EXCL_*`` families always
    apply — so the standard markers are honored on harvested tracefiles that
    ``geninfo`` never saw, not just on captures otto ran ``geninfo`` for.

    Returns:
        The populated :class:`~otto.coverage.store.model.CoverageStore`.

    Raises:
        CoverageInputError: a *cov_dirs* entry's run directory does not
            exist, run directories are given without ``inputs.project_root``,
            a cov dir's metadata is stale, or a cov dir with products not yet
            captured records no tier or commit (``field="cov_dirs"``);
            *tier_specs* names a tier with no path, repeats a tier name, or is
            given together with run directories (``field="tier_specs"``); a
            unit tier's configured tool cannot be resolved or contradicts its
            directory's stamps, or a unit directory holds two compilers' notes
            (``field=None``). Every one is raised before *output_dir* is
            cleared or created.
        otto.coverage.errors.CaptureCommitError: a capture, or a cov dir not
            yet captured, belongs to another commit than the tree's; also
            raised before *output_dir* is cleared or created.
        otto.coverage.errors.CoverageNotInstrumentedError: a unit view selects
            no product, or a selected product is not instrumented.
        otto.coverage.errors.CaptureFailedError: a unit view's product's
            object list could not be read, or a unit capture recorded nothing.
        otto.host.errors.CoverageToolMissingError: a tool a unit directory's
            data needs by default is not on ``PATH``; raised before
            *output_dir* is cleared or created.
    """
    _validate_report_inputs(cov_dirs, tier_specs, inputs.project_root)
    # Every input refusal runs before the destination is cleared or created.
    runs = _scan_run_inputs(cov_dirs)
    objects: "ObjectListCache | None" = None
    unit_plan: "UnitPlan | None" = None
    if inputs.project_root is not None:
        _refuse_moved_commits(runs, inputs.project_root)
        from .objects import ObjectListCache
        from .pairing import GcnoIndex
        from .sources import SourceResolver
        from .unit_filter import plan_unit_views

        # One cache for the report: the unit preflight and every capture share its reads.
        objects = ObjectListCache(inputs.project_root)
        unit_plan = await plan_unit_views(
            list(inputs.tier_configs or []),
            inputs.project_root,
            inputs.project_name,
            objects,
            SourceResolver(inputs.project_root, quiet=True),
            GcnoIndex(inputs.project_root),
        )

    from .config import prepare_destination

    prepare_destination(
        output_dir, overwrite=overwrite, field="output_dir", remedy_field="overwrite"
    )
    capture_paths: list[Path] = []
    if objects is not None:
        capture_paths = await _capture_states(runs, objects.project_root, objects)

    reporter = CoverageReporter(
        source_root=inputs.project_root or Path.cwd(),
        output_dir=output_dir,
        report_title=report_title,
        tiers=tier_specs,
        collection=CollectionInputs(
            project_root=inputs.project_root,
            tier_configs=list(inputs.tier_configs or []),
            capture_paths=capture_paths,
            exclusion_rules=list(inputs.exclusion_rules),
            project_name=inputs.project_name,
            objects=objects,
            unit_plan=unit_plan,
        ),
        prefix=prefix,
        thresholds=inputs.thresholds,
        ticket_spec=inputs.ticket_spec,
        overrides=inputs.overrides,
    )
    return await reporter.run()
