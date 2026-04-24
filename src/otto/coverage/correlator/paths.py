"""Path correlator: normalise source paths across build host, remote hosts,
and gcda metadata.

gcda files embed the absolute paths used at compile time.  When run on a
remote host those paths rarely match the local source tree — especially in
cross-compilation scenarios.  This module handles the remapping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ...host.localHost import LocalHost

logger = logging.getLogger(__name__)


@dataclass
class PathMapping:
    """A single path-prefix substitution rule.

    ``from_prefix`` is matched against embedded ``.info`` paths.
    ``to_prefix`` is the replacement on the local build host.
    """

    from_prefix: str
    to_prefix: str

    def apply(self, path: str) -> str | None:
        """Return the remapped path if this rule matches, else ``None``."""
        if path.startswith(self.from_prefix):
            return self.to_prefix + path[len(self.from_prefix) :]
        return None


class PathCorrelator:
    """Apply a sequence of :class:`PathMapping` rules to normalise file paths.

    Mappings are tried in order; first match wins.  This lets you layer
    rules for different build environments (local dev, CI, release).
    """

    def __init__(self, mappings: list[PathMapping]) -> None:
        self.mappings = mappings

    def resolve(self, raw_path: str) -> Path | None:
        for mapping in self.mappings:
            result = mapping.apply(raw_path)
            if result is not None:
                p = Path(result)
                if p.exists():
                    return p
                logger.debug("Rule matched but path not found: %s -> %s", raw_path, result)
        logger.warning("No mapping resolved existing file for: %s", raw_path)
        return None

    def resolve_strict(self, raw_path: str) -> Path:
        result = self.resolve(raw_path)
        if result is None:
            raise FileNotFoundError(
                f"Could not resolve path to an existing file: {raw_path}\n"
                f"Configured mappings: {self.mappings}"
            )
        return result


async def discover_path_mappings(
    info_path: Path,
    source_root: Path,
    localhost: LocalHost,
) -> list[PathMapping]:
    """Auto-discover path mappings from a sample ``.info`` file.

    Parses ``SF:`` lines from the ``.info`` file, extracts the common
    prefix of the embedded paths, and builds a mapping to *source_root*.

    This eliminates the need for users to manually reverse-engineer
    compiler ``-fdebug-prefix-map`` settings.

    Args:
        info_path: Path to a captured ``.info`` file (from ``lcov --capture``).
        source_root: Local source tree root to map into.
        localhost: LocalHost instance for running commands.

    Returns:
        A list containing a single :class:`PathMapping` from the
        discovered common prefix to *source_root*, or an empty list
        if no ``SF:`` lines were found.
    """
    result = await localhost.oneshot(f"grep '^SF:' {info_path}")
    if result.retcode != 0 or not result.output.strip():
        logger.warning("No SF: lines found in %s", info_path)
        return []

    sf_paths = [line[3:] for line in result.output.strip().splitlines() if line.startswith("SF:")]
    if not sf_paths:
        return []

    # Find the common prefix of all embedded source paths
    from os.path import commonpath

    try:
        common = commonpath(sf_paths)
    except ValueError:
        logger.warning("SF: paths have no common prefix (mixed drives?)")
        return []

    # commonpath returns the longest common path component boundary.
    # If the result looks like a file (has an extension), walk up to its parent.
    common_p = Path(common)
    if common_p.suffix:
        common_dir = str(common_p.parent)
    else:
        common_dir = common

    source_str = str(source_root.resolve())
    if common_dir == source_str:
        logger.info("Embedded paths already match source root, no mapping needed")
        return []

    mapping = PathMapping(from_prefix=common_dir, to_prefix=source_str)
    logger.info("Auto-discovered path mapping: %s -> %s", common_dir, source_str)
    return [mapping]


async def discover_from_gcno(
    gcno_dir: Path,
    source_root: Path,
    localhost: LocalHost,
) -> list[PathMapping]:
    """Auto-discover path mappings by inspecting ``.gcno`` file contents.

    Uses ``strings`` to extract embedded paths from ``.gcno`` files,
    then finds the common prefix and maps it to *source_root*.

    This is useful when no ``.info`` file is available yet (before the
    first ``lcov --capture``).

    Args:
        gcno_dir: Directory containing ``.gcno`` files from the build.
        source_root: Local source tree root to map into.
        localhost: LocalHost instance for running commands.

    Returns:
        A list with a single :class:`PathMapping`, or empty if
        discovery fails.
    """
    # Find a sample .gcno file
    result = await localhost.oneshot(f"find {gcno_dir} -name '*.gcno' -type f | head -20")
    if result.retcode != 0 or not result.output.strip():
        logger.warning("No .gcno files found in %s", gcno_dir)
        return []

    gcno_files = result.output.strip().splitlines()

    # Extract source paths embedded in .gcno files
    source_paths: list[str] = []
    for gcno in gcno_files[:5]:  # sample a few files
        strings_result = await localhost.oneshot(f"strings {gcno}")
        if strings_result.retcode != 0:
            continue
        for line in strings_result.output.splitlines():
            stripped = line.strip()
            if stripped.endswith((".c", ".cpp", ".cc", ".h", ".hpp")) and "/" in stripped:
                source_paths.append(stripped)

    if not source_paths:
        logger.warning("Could not extract source paths from .gcno files")
        return []

    from os.path import commonpath

    try:
        common = commonpath(source_paths)
    except ValueError:
        return []

    common_dir = common if Path(common).is_dir() or common.endswith("/") else str(Path(common).parent)
    source_str = str(source_root.resolve())

    if common_dir == source_str:
        return []

    mapping = PathMapping(from_prefix=common_dir, to_prefix=source_str)
    logger.info("Auto-discovered path mapping from .gcno: %s -> %s", common_dir, source_str)
    return [mapping]
