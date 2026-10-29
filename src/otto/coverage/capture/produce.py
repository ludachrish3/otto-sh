"""Per-product ``capture.json`` production from fetched ``.gcda`` counters.

Turns the raw ``.gcda`` counters collected by ``otto test --cov`` into a
:class:`~otto.coverage.capture.model.Capture` per (host, product),
anchored to ``base_commit``.  For each ``<cov_dir>/<host>/<product>/``
directory this:

1. Resolves the product's toolchain via
   :func:`~otto.coverage.toolchains.resolve_toolchains`, and its gcno
   (build) source root from the ``.otto_cov_meta.json`` sidecar via the
   :mod:`otto.coverage.reporter` helpers (per host, not per product).
2. Reads the product's object list from its local artifact
   (:class:`~otto.coverage.objects.ObjectListCache`) and pairs every
   ``.gcda`` with its own ``.gcno``
   (:func:`~otto.coverage.pairing.pair_product`).
3. Stages the pairs under ``<cov_dir>/.otto-stage/<host>/<product>/`` and
   runs one ``lcov --capture`` over them
   (:meth:`~otto.coverage.merge.merger.LcovMerger.capture_pairs`),
   producing ``<host>/<product>/board.info``.
4. Resolves each ``SF:`` path to its project file
   (:class:`~otto.coverage.sources.SourceResolver`), producing
   ``<host>/<product>/board.resolved.info``.
5. Builds and saves ``<host>/<product>/capture.json`` via
   :func:`~otto.coverage.capture.model.build_capture`.

A product that fails any step gets a ``capture.failed`` holding the short
reason instead, and the loop moves on to the next product; one
:class:`~otto.coverage.errors.CoverageCaptureError` naming every failure is
raised after the loop. The raw ``.gcda``, ``board.info`` and
``board.resolved.info`` stay on disk as debug artifacts; the staging leaf is
scratch, removed once its product is done (captured or failed), and
``.otto-stage`` with it once empty, so a finished cov dir holds only
``<host>/<product>/`` dirs and the metadata sidecar.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import (
    CaptureCommitError,
    CaptureFailedError,
    CoverageCaptureError,
    CoverageDataMismatchError,
    CoverageToolVersionError,
    PairingError,
)
from ..merge.merger import LcovMerger
from ..objects import ObjectListCache, Unavailable
from ..pairing import GcnoIndex, pair_product
from ..reporter import read_cov_source_root, read_cov_source_roots, read_cov_toolchains
from ..sources import SourceResolver, write_resolved_info
from ..toolchains import resolve_toolchains
from ..tree import ProductKey, iter_product_dirs
from . import gitio
from .model import build_capture

if TYPE_CHECKING:
    from ...host.product import Product

logger = logging.getLogger(__name__)

STAGE_DIR = ".otto-stage"
"""The dot-directory under a cov dir that stages each product's pairs for lcov."""


@dataclass(frozen=True)
class ProductCapture:
    """One product's collection outcome: exactly one of *capture* and *failure* is set."""

    key: ProductKey
    capture: Path | None
    """The ``capture.json`` written, when the product captured."""
    failure: str | None
    """The short failure written to ``capture.failed``, when it did not; for a
    product whose counters were never fetched, why the fetch failed."""
    fetched: bool = True
    """``False`` when the product's counters could not be fetched at all, so
    it never reached capture (and has no ``capture.failed``)."""
    remedy: str | None = None
    """What the user must change, when re-running ``otto cov get`` after
    fixing the cause is not it (a missing declaration, say); ``None`` for the
    usual remedy."""


@dataclass(frozen=True)
class ProduceResult:
    """Every product's outcome from one :func:`produce_captures` call, in walk order."""

    products: list[ProductCapture]

    @property
    def written(self) -> list[Path]:
        """List the ``capture.json`` files written."""
        return [p.capture for p in self.products if p.capture is not None]

    @property
    def captured(self) -> list[ProductKey]:
        """List the products that captured."""
        return [p.key for p in self.products if p.capture is not None]

    @property
    def failed(self) -> list[ProductCapture]:
        """List the products that failed to capture."""
        return [p for p in self.products if p.failure is not None]


def _product_dirs(cov_dir: Path) -> list[tuple[str, str, Path]]:
    """``(host_id, product, dir)`` for every ``cov/<host>/<product>/`` holding ``.gcda``.

    Product dirs with no ``.gcda`` files anywhere below them are skipped
    with a warning.  The walk itself — and its by-name refusal of a host
    dir holding counters or a capture directly, the pre-product one-level
    tree — lives in :func:`~otto.coverage.tree.iter_product_dirs`.
    """
    instrumented: list[tuple[str, str, Path]] = []
    for host_id, product, product_dir in iter_product_dirs(cov_dir):
        if next(product_dir.rglob("*.gcda"), None) is None:
            logger.warning("Skipping product dir with no .gcda files: %s", product_dir)
            continue
        instrumented.append((host_id, product, product_dir))
    return instrumented


async def produce_captures(  # noqa: PLR0913 — one keyword per capture annotation
    cov_dir: Path,
    *,
    tier: str,
    project_root: Path,
    labs: list[str],
    products: "dict[ProductKey, Product]",
    base_commit: str | None = None,
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    display_names: dict[str, str] | None = None,
    only: list[ProductKey] | None = None,
    objects: ObjectListCache | None = None,
) -> ProduceResult:
    """Produce a ``capture.json`` anchored to ``base_commit`` per product dir under *cov_dir*.

    A product dir is any ``<cov_dir>/<host>/<product>/`` containing at
    least one ``.gcda`` file (recursively).  Products with no ``.gcda``
    files are skipped with a warning. Every product is attempted: one that
    cannot be paired, has no usable gcov, or fails ``lcov --capture`` gets
    a ``capture.failed`` (its short reason) and the others still capture.
    A product's earlier ``capture.json`` / ``capture.failed`` is removed
    before it is attempted again.

    Args:
        cov_dir: Coverage directory written by ``otto test --cov``,
            containing ``<host>/<product>/`` subdirs and a
            ``.otto_cov_meta.json`` sidecar.
        tier: Coverage tier name to annotate onto each capture.
        project_root: the project's checkout root, used for base_commit/blob resolution
            and as the source-resolution target.
        labs: Lab identifiers to annotate onto each capture.
        products: (host, product) → that host's definition of the product,
            whose local artifact supplies the object list pairing prefers.
            Keyed by host too: two hosts may carry different builds under
            one product name. A product with no entry is paired by path
            alone.
        base_commit: The commit the counters were collected at, when known.
            Production is refused up front, before anything is touched,
            unless the tree is still at it.
        tester: Optional tester identity to annotate onto each capture.
        ticket: Optional ticket reference to annotate onto each capture.
        note: Optional free-text note to annotate onto each capture.
        display_names: Host-dir name (host id) → host display name; hosts
            without an entry are annotated ``None``.
        only: When given, produce only these (host, product) pairs; the
            others are left exactly as they are.
        objects: The object-list cache to read artifacts through (a caller
            producing several cov dirs shares one); a new one when omitted.

    Returns:
        Every attempted product's outcome, in host-then-product sort order.

    Raises:
        otto.coverage.errors.CoverageCaptureError: After the loop, when any
            product failed; its ``result`` holds every outcome.
        otto.coverage.errors.CaptureCommitError: *base_commit* is given and
            the tree is at a different commit.
        otto.coverage.capture.gitio.GitUnavailableError: If *project_root*
            is not a git repository.
        otto.config.coverage_settings.CoverageConfigError: If a host dir holds
            coverage data directly (the pre-product one-level tree).
    """
    from ...host.connections import teardown_step
    from ...host.errors import CoverageToolMissingError
    from ...host.local_host import LocalHost

    if base_commit is not None:
        head = gitio.head_commit(project_root)
        if head != base_commit:
            raise CaptureCommitError(capture=str(cov_dir), taken_at=base_commit, head=head)

    toolchains = read_cov_toolchains([cov_dir])
    source_roots = read_cov_source_roots([cov_dir])
    fallback_root = read_cov_source_root([cov_dir])
    product_dirs = _product_dirs(cov_dir)
    if objects is None:
        objects = ObjectListCache(project_root)
    indexes: dict[Path, GcnoIndex] = {}

    localhost = LocalHost()
    merger = LcovMerger(localhost)
    results: list[ProductCapture] = []
    try:
        for board, product, board_dir in product_dirs:
            key = ProductKey(board, product)
            if only is not None and key not in only:
                continue
            label = key.label
            for stale in ("capture.json", "capture.failed"):
                (board_dir / stale).unlink(missing_ok=True)
            logger.info("=== Capturing %s ===", label)
            # geninfo writes absolute SF: paths under the --directory it is
            # given; the resolver strips exactly that prefix, so both get
            # the one resolved path.
            staging = (cov_dir / STAGE_DIR / board / product).resolve()
            try:
                # One resolver with the reporter: a recorded gcov wins, else the
                # product's own .gcda stamp names the tool, before any lcov runs.
                (toolchain,) = resolve_toolchains([board_dir], toolchains)
                gcno_dir = source_roots.get(board, fallback_root)
                if gcno_dir not in indexes:
                    indexes[gcno_dir] = GcnoIndex(gcno_dir)
                if key in products:
                    # The cache logs its own INFO when the list is unavailable.
                    listed = objects.read(products[key])
                else:
                    reason = "no definition of this product in the active lab"
                    logger.info("%s: object list unavailable: %s — pairing by path", label, reason)
                    listed = Unavailable(product=product, reason=reason, kind="no-definition")
                pairs = pair_product(board_dir, listed, indexes[gcno_dir], label=label)
                raw_info = board_dir / "board.info"
                await merger.capture_pairs(pairs, staging, raw_info, toolchain=toolchain)
                resolved_info = board_dir / "board.resolved.info"
                write_resolved_info(
                    raw_info, resolved_info, SourceResolver(project_root, staging=staging)
                )
                capture = build_capture(
                    info_path=resolved_info,
                    tier=tier,
                    project_root=project_root,
                    board=board,
                    product=product,
                    labs=labs,
                    tester=tester,
                    ticket=ticket,
                    note=note,
                    display_name=(display_names or {}).get(board),
                    base_commit=base_commit,
                )
                capture_path = board_dir / "capture.json"
                capture.save(capture_path)
                results.append(ProductCapture(key, capture_path, None))
            except (
                PairingError,
                CoverageDataMismatchError,
                CoverageToolVersionError,
                CoverageToolMissingError,
                CaptureFailedError,
            ) as e:
                # The aggregate error is the one console rendering of this.
                (board_dir / "capture.failed").write_text(f"{e}\n")
                logger.debug("%s: capture failed: %s", label, e)
                results.append(ProductCapture(key, None, str(e)))
            finally:
                _drop_staging(cov_dir, staging)
    finally:
        merger.close()
        with teardown_step("coverage capture", "localhost close"):
            await localhost.close()

    result = ProduceResult(results)
    if result.failed:
        raise CoverageCaptureError(result)
    return result


def _drop_staging(cov_dir: Path, staging: Path) -> None:
    """Remove *staging* and then each parent it leaves empty, up to ``.otto-stage``.

    The staging tree is scratch for one ``lcov --capture``: the raw ``.gcda``
    stay in the product's own directory, and its notes are the build tree's.
    A sibling product's leaf (another host, or one *only* left out) keeps its
    parents.
    """
    shutil.rmtree(staging, ignore_errors=True)
    stage_root = (cov_dir / STAGE_DIR).resolve()
    for parent in (staging.parent, stage_root):
        if not parent.is_dir() or any(parent.iterdir()):
            return
        parent.rmdir()
