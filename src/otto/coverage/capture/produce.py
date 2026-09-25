"""Per-product ``capture.json`` production from fetched ``.gcda`` counters.

Turns the raw ``.gcda`` counters collected by ``otto test --cov`` into a
:class:`~otto.coverage.capture.model.Capture` per (host, product),
anchored to ``base_commit``.  For each ``<cov_dir>/<host>/<product>/``
directory this:

1. Resolves each product's gcno (build) source root from the
   ``.otto_cov_meta.json`` sidecar via the :mod:`otto.coverage.reporter`
   helpers (per host, not per product), and its toolchain via
   :func:`~otto.coverage.toolchains.resolve_toolchains`.
2. Runs :meth:`~otto.coverage.merge.merger.LcovMerger.capture` for
   that product alone, producing ``<host>/<product>/board.info``.
3. Auto-discovers path mappings and rewrites the embedded ``SF:`` paths
   to their local, ``repo_root``-relative form, producing
   ``<host>/<product>/board.resolved.info``.
4. Builds and saves ``<host>/<product>/capture.json`` via
   :func:`~otto.coverage.capture.model.build_capture`.

The raw ``.gcda``, ``board.info``, and ``board.resolved.info`` all stay
on disk as debug artifacts (spec decision 18).
"""

import logging
from pathlib import Path

from ..merge.merger import LcovMerger
from ..merge.paths import PathRemapper, discover_path_mappings
from ..reporter import read_cov_source_root, read_cov_source_roots, read_cov_toolchains
from ..toolchains import resolve_toolchains
from ..tree import iter_product_dirs
from .model import build_capture

logger = logging.getLogger(__name__)


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


def _write_resolved_info(raw_info: Path, resolved_info: Path, remapper: PathRemapper) -> None:
    """Rewrite ``SF:`` lines in *raw_info* to their remapped local paths.

    Lines whose path cannot be resolved are kept as-is (with a warning)
    so downstream parsing degrades the same way
    :class:`~otto.coverage.merge.lcov_loader.LCOVLoader` does.
    """
    out_lines: list[str] = []
    with raw_info.open() as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if line.startswith("SF:"):
                raw_path = line[3:]
                resolved = remapper.resolve(raw_path)
                if resolved is None:
                    logger.warning("Unmapped path in %s, keeping raw: %s", raw_info, raw_path)
                    out_lines.append(line)
                else:
                    out_lines.append(f"SF:{resolved}")
            else:
                out_lines.append(line)
    resolved_info.write_text("\n".join(out_lines) + "\n")


async def produce_captures(
    cov_dir: Path,
    *,
    tier: str,
    repo_root: Path,
    labs: list[str],
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    display_names: dict[str, str] | None = None,
) -> list[Path]:
    """Produce a ``capture.json`` anchored to ``base_commit`` per product dir under *cov_dir*.

    A product dir is any ``<cov_dir>/<host>/<product>/`` containing at
    least one ``.gcda`` file (recursively).  Products with no ``.gcda``
    files are skipped with a warning.

    Args:
        cov_dir: Coverage directory written by ``otto test --cov``,
            containing ``<host>/<product>/`` subdirs and a
            ``.otto_cov_meta.json`` sidecar.
        tier: Coverage tier name to annotate onto each capture.
        repo_root: SUT git repo root, used for base_commit/blob resolution
            and as the path-remapping target.
        labs: Lab identifiers to annotate onto each capture.
        tester: Optional tester identity to annotate onto each capture.
        ticket: Optional ticket reference to annotate onto each capture.
        note: Optional free-text note to annotate onto each capture.
        display_names: Host-dir name (host id) → host display name; hosts
            without an entry are annotated ``None``.

    Returns:
        Paths of the ``capture.json`` files written, one per (host,
        product), in host-then-product sort order.

    Raises:
        otto.coverage.capture.gitio.GitUnavailableError: If *repo_root*
            is not a git repository.
        otto.config.coverage_settings.CoverageConfigError: If a host dir holds
            coverage data directly (the pre-product one-level tree).
        otto.host.errors.CoverageToolMissingError: If a product's stamp
            names a gcov that is not on PATH.
    """
    from ...host.connections import teardown_step
    from ...host.local_host import LocalHost

    toolchains = read_cov_toolchains([cov_dir])
    source_roots = read_cov_source_roots([cov_dir])
    fallback_root = read_cov_source_root([cov_dir])
    product_dirs = _product_dirs(cov_dir)
    # One resolver with the reporter: a recorded gcov wins, else the
    # product's own .gcda stamp names the tool, before any lcov runs.
    resolved = resolve_toolchains([d for _, _, d in product_dirs], toolchains)

    localhost = LocalHost()
    written: list[Path] = []
    try:
        merger = LcovMerger(localhost)
        for (board, product, board_dir), toolchain in zip(product_dirs, resolved, strict=True):
            gcno_dir = source_roots.get(board, fallback_root)

            raw_info = board_dir / "board.info"
            logger.info("=== Capturing %s / %s ===", board, product)
            await merger.capture(board_dir, gcno_dir, raw_info, toolchain=toolchain)

            mappings = await discover_path_mappings(raw_info, repo_root, localhost)
            remapper = PathRemapper(mappings)
            resolved_info = board_dir / "board.resolved.info"
            _write_resolved_info(raw_info, resolved_info, remapper)

            capture = build_capture(
                info_path=resolved_info,
                tier=tier,
                repo_root=repo_root,
                board=board,
                product=product,
                labs=labs,
                tester=tester,
                ticket=ticket,
                note=note,
                display_name=(display_names or {}).get(board),
            )
            capture_path = board_dir / "capture.json"
            capture.save(capture_path)
            written.append(capture_path)
    finally:
        with teardown_step("coverage capture", "localhost close"):
            await localhost.close()

    return written
