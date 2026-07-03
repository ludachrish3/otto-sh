"""Per-board ``capture.json`` production from fetched ``.gcda`` counters.

Turns the raw ``.gcda`` counters collected by ``otto test --cov`` into a
pinned :class:`~otto.coverage.capture.model.Capture` per board.  For each
board directory under ``cov_dir`` this:

1. Resolves the board's toolchain and gcno (build) source root from the
   ``.otto_cov_meta.json`` sidecar via the :mod:`otto.coverage.reporter`
   helpers.
2. Runs :meth:`~otto.coverage.correlator.merger.LcovMerger.capture` for
   that board alone, producing ``<board>/board.info``.
3. Auto-discovers path mappings and rewrites the embedded ``SF:`` paths
   to their local, ``repo_root``-relative form, producing
   ``<board>/board.resolved.info``.
4. Builds and saves ``<board>/capture.json`` via
   :func:`~otto.coverage.capture.model.build_capture`.

The raw ``.gcda``, ``board.info``, and ``board.resolved.info`` all stay
on disk as debug artifacts (spec decision 18).
"""

import logging
from pathlib import Path

from ..correlator.merger import LcovMerger
from ..correlator.paths import PathCorrelator, discover_path_mappings
from ..reporter import read_cov_source_root, read_cov_source_roots, read_cov_toolchains
from .model import build_capture

logger = logging.getLogger(__name__)


def _board_dirs(cov_dir: Path) -> list[Path]:
    """Direct subdirectories of *cov_dir* containing at least one ``.gcda`` file (recursive).

    Non-directory entries (e.g. the ``.otto_cov_meta.json`` sidecar) are
    skipped silently.  Directories with no ``.gcda`` files anywhere below
    them are skipped with a warning.
    """
    boards: list[Path] = []
    for entry in sorted(cov_dir.iterdir()):
        if not entry.is_dir():
            continue
        if next(entry.rglob("*.gcda"), None) is None:
            logger.warning("Skipping board dir with no .gcda files: %s", entry)
            continue
        boards.append(entry)
    return boards


def _write_resolved_info(raw_info: Path, resolved_info: Path, correlator: PathCorrelator) -> None:
    """Rewrite ``SF:`` lines in *raw_info* to their correlated local paths.

    Lines whose path cannot be resolved are kept as-is (with a warning)
    so downstream parsing degrades the same way
    :class:`~otto.coverage.correlator.lcov_loader.LCOVLoader` does.
    """
    out_lines: list[str] = []
    with raw_info.open() as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if line.startswith("SF:"):
                raw_path = line[3:]
                resolved = correlator.resolve(raw_path)
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
) -> list[Path]:
    """Produce a pinned ``capture.json`` for each board dir under *cov_dir*.

    A board dir is any direct subdirectory of *cov_dir* containing at
    least one ``.gcda`` file (recursively).  Boards with no ``.gcda``
    files are skipped with a warning.

    Args:
        cov_dir: Coverage directory written by ``otto test --cov``,
            containing per-board subdirs and a ``.otto_cov_meta.json``
            sidecar.
        tier: Coverage tier name to stamp onto each capture.
        repo_root: SUT git repo root, used for pin/blob resolution and
            as the path-correlation target.
        labs: Lab identifiers to stamp onto each capture.
        tester: Optional tester identity to stamp onto each capture.
        ticket: Optional ticket reference to stamp onto each capture.
        note: Optional free-text note to stamp onto each capture.

    Returns:
        Paths of the ``capture.json`` files written, one per board, in
        board-name sort order.

    Raises:
        otto.coverage.capture.gitio.GitUnavailableError: If *repo_root*
            is not a git repository.
    """
    from ...host.local_host import LocalHost

    toolchains = read_cov_toolchains([cov_dir])
    source_roots = read_cov_source_roots([cov_dir])
    fallback_root = read_cov_source_root([cov_dir])

    localhost = LocalHost()
    written: list[Path] = []
    try:
        merger = LcovMerger(localhost)
        for board_dir in _board_dirs(cov_dir):
            board = board_dir.name
            gcno_dir = source_roots.get(board, fallback_root)
            toolchain = toolchains.get(board)

            raw_info = board_dir / "board.info"
            logger.info("=== Capturing board %r ===", board)
            await merger.capture(board_dir, gcno_dir, raw_info, toolchain=toolchain)

            mappings = await discover_path_mappings(raw_info, repo_root, localhost)
            correlator = PathCorrelator(mappings)
            resolved_info = board_dir / "board.resolved.info"
            _write_resolved_info(raw_info, resolved_info, correlator)

            capture = build_capture(
                info_path=resolved_info,
                tier=tier,
                repo_root=repo_root,
                board=board,
                labs=labs,
                tester=tester,
                ticket=ticket,
                note=note,
            )
            capture_path = board_dir / "capture.json"
            capture.save(capture_path)
            written.append(capture_path)
    finally:
        await localhost.close()

    return written
