"""Merge gcda files from multiple hosts using lcov.

Wraps ``lcov --capture`` and ``lcov --add-tracefile`` invocations,
executing them through :class:`~otto.host.local_host.LocalHost` so
they are fully async with proper logging and timeout handling.
"""

import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from ...host.local_host import LocalHost
from ...host.toolchain_discovery import ensure_gcov_tool
from ...utils import Status

if TYPE_CHECKING:
    from ...host.toolchain import Toolchain

logger = logging.getLogger(__name__)


_GCOV_HEADER_LEN = 12
"""Magic word, format-version word, build stamp — 4 bytes each.

GNU gcov and clang's gcov-compatible mode agree on this layout for
``.gcno`` and ``.gcda`` files alike.
"""


def _gcov_header(path: Path) -> bytes | None:
    """Read the gcov file header, or ``None`` for files too short to carry it."""
    with path.open("rb") as f:
        header = f.read(_GCOV_HEADER_LEN)
    return header if len(header) == _GCOV_HEADER_LEN else None


_GCOV_TAG_FUNCTION = 0x01000000
_GCOV_FUNCTION_ANNOUNCE_WORDS = 3  # ident, lineno_checksum, cfg_checksum


def _clang_function_records(path: Path) -> set[tuple[int, int, int]] | None:
    """Parse (ident, lineno_checksum, cfg_checksum) triplets from a clang-dialect gcov file.

    clang emits the GCC 4.8-era layout: the 12-byte header followed by
    tag/length records with the length counted in 32-bit words. These
    per-function triplets are exactly what llvm-cov verifies at load — and
    silently zeroes on disagreement — so they are the only place a
    shifted-lines stale deploy is visible (clang's file stamp is a
    structure hash and does not move for such edits).

    Returns ``None`` when the file does not walk cleanly as this dialect.
    GNU gcc-12+ files never do (16-byte headers, byte-unit lengths), which
    is deliberate: GCC restamps every compile, so the header check above
    already catches its stale deploys, and GNU gcov refuses loudly as the
    backstop — no deep check needed, no false-positive risk taken.
    """
    data = path.read_bytes()
    pos = _GCOV_HEADER_LEN
    functions: set[tuple[int, int, int]] = set()
    while pos + 8 <= len(data):
        tag, length = struct.unpack_from("<II", data, pos)
        pos += 8
        if tag == 0:  # zero padding at EOF
            break
        nbytes = length * 4
        if pos + nbytes > len(data):
            return None
        if tag == _GCOV_TAG_FUNCTION:
            if length < _GCOV_FUNCTION_ANNOUNCE_WORDS:
                return None
            functions.add(struct.unpack_from("<III", data, pos))
        pos += nbytes
    return functions


def _stamp_mismatches(gcda_dir: Path, build_dirs: list[Path]) -> list[str]:
    """Verify each ``.gcda`` pairs with a same-stem ``.gcno`` in *build_dirs*.

    A ``.gcda`` decodes only against the notes of the exact compilation
    that produced its binary. GNU gcov refuses a bad pairing loudly, but
    llvm-cov (clang builds) prints its complaint and exits 0 — lcov then
    "succeeds" with the file recorded at all-zero hits and nothing in its
    output to detect. So the pairing is verified structurally, before lcov
    runs: bytes 4:12 of both headers (format version + build stamp) must
    agree, as raw bytes so endianness never enters into it. A ``.gcda``
    with several same-stem candidates passes if any one matches, mirroring
    lcov's own resolution across ``--build-directory`` args; one with no
    candidate at all is left for lcov's own diagnostics.

    Returns one human-readable line per mismatched ``.gcda``.
    """
    mismatches: list[str] = []
    for gcda in sorted(gcda_dir.glob("*.gcda")):
        data_header = _gcov_header(gcda)
        if data_header is None:
            continue
        candidates = [
            (gcno, header)
            for d in build_dirs
            for gcno in [d / f"{gcda.stem}.gcno"]
            if gcno.is_file() and (header := _gcov_header(gcno)) is not None
        ]
        if not candidates:
            continue
        matching = [(g, h) for g, h in candidates if h[4:12] == data_header[4:12]]
        if matching:
            line = _function_checksum_mismatch(gcda, [g for g, _ in matching])
            if line:
                mismatches.append(line)
            continue
        gcno, notes_header = candidates[0]
        if notes_header[4:8] != data_header[4:8]:
            mismatches.append(
                f"{gcda}: format version {data_header[4:8]!r} does not match "
                f"notes file {gcno} ({notes_header[4:8]!r}) — written by "
                f"different compiler generations"
            )
        else:
            data_stamp, notes_stamp = (
                int.from_bytes(h[8:12], "little") for h in (data_header, notes_header)
            )
            mismatches.append(
                f"{gcda}: stamp mismatch with notes file {gcno} "
                f"(data {data_stamp:#010x} vs notes {notes_stamp:#010x})"
            )
    return mismatches


def _function_checksum_mismatch(gcda: Path, matching_gcnos: list[Path]) -> str | None:
    """Run the deep, clang-dialect half of the pairing check.

    Called for a ``.gcda`` whose header already agrees with *matching_gcnos*
    — which for clang proves nothing about line positions, since its stamp
    is a structure hash. The pairing passes if any candidate's notes carry
    every function triplet the data references, or if either side is not
    verifiable as the clang dialect (GNU files, empty record sets) — the
    check never guesses. Returns the mismatch line, or ``None`` when the
    pairing stands.
    """
    data_funcs = _clang_function_records(gcda)
    if not data_funcs:
        return None
    first_missing: tuple[Path, set[tuple[int, int, int]]] | None = None
    for gcno in matching_gcnos:
        notes_funcs = _clang_function_records(gcno)
        if not notes_funcs:
            return None
        missing = data_funcs - notes_funcs
        if not missing:
            return None
        first_missing = first_missing or (gcno, missing)
    if first_missing is None:  # unreachable with a non-empty candidate list
        return None
    gcno, missing = first_missing
    return (
        f"{gcda}: function checksums do not match notes file {gcno} "
        f"({len(missing)} of {len(data_funcs)} functions differ) — source "
        f"lines changed since the shipped binary was built; llvm-cov would "
        f"silently record zero hits for this file"
    )


def _find_gcno_dirs(gcda_dir: Path, search_root: Path) -> list[Path]:
    """Find ``.gcno``-containing directories under *search_root* that match ``.gcda`` basenames.

    ``lcov --build-directory`` does not search recursively, so we need
    to locate the exact directories and pass each one.

    Returns:
        De-duplicated list of directories containing matching ``.gcno``
        files, or ``[search_root]`` as fallback.
    """
    gcda_stems = {p.stem for p in gcda_dir.glob("*.gcda")}
    if not gcda_stems:
        return [search_root]

    dirs: set[Path] = set()
    for gcno in search_root.rglob("*.gcno"):
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
        toolchain: "Toolchain | None" = None,
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
        # An llvm-cov gcov tool (clang-instrumented build) needs a one-word
        # wrapper for lcov; anything else passes through unchanged.
        gcov = ensure_gcov_tool(gcov, output.parent)

        build_dirs = _find_gcno_dirs(gcda_dir, gcno_dir)

        # Structural pre-check, not output parsing: for clang builds a stamp
        # mismatch never surfaces in lcov's output (llvm-cov exits 0 and the
        # file is recorded at all-zero hits), so this is the only reliable
        # detection point. The post-run substring checks below stay as the
        # backstop for whatever this cannot see.
        mismatches = _stamp_mismatches(gcda_dir, build_dirs)
        if mismatches:
            from ..errors import CoverageDataMismatchError

            raise CoverageDataMismatchError("\n".join(mismatches))

        build_args = " ".join(f"--build-directory {d}" for d in build_dirs)

        cmd = (
            f"{lcov} --capture"
            f" --directory {gcda_dir}"
            f" {build_args}"
            f" --gcov-tool {gcov}"
            f" --rc branch_coverage=1"
            f" --output-file {output}"
        )
        logger.info("lcov capture: %s -> %s", gcda_dir, output)
        result = await self.localhost.exec(cmd, timeout=300)
        if result.status != Status.Success:
            if "stamp mismatch" in (result.value or ""):
                # gcov's marker for .gcda produced by a DIFFERENT build than
                # the .gcno notes files — the polluted-tree / partial-rebuild
                # error mode. Raise the typed error so the CLI can explain
                # the cause instead of dumping raw lcov output.
                from ..errors import CoverageDataMismatchError

                raise CoverageDataMismatchError(result.value)
            if "Incompatible GCC/GCOV version" in (result.value or ""):
                # geninfo's marker for a gcov tool that cannot read this
                # build's file format — the wrong-compiler-family error mode
                # (classically: a clang build captured with GNU gcov).
                from ..errors import CoverageToolVersionError

                raise CoverageToolVersionError(result.value)
            raise RuntimeError(f"lcov --capture failed:\n{result.value}")
        return output

    async def merge_info_files(
        self,
        info_files: list[Path],
        output: Path,
        toolchain: "Toolchain | None" = None,
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
        result = await self.localhost.exec(cmd, timeout=300)
        if result.status != Status.Success:
            raise RuntimeError(f"lcov --add-tracefile failed:\n{result.value}")
        return output

    async def capture_and_merge(
        self,
        host_gcda_dirs: list[Path],
        gcno_dir: Path,
        work_dir: Path,
        toolchains: "list[Toolchain | None] | None" = None,
        gcno_dirs: list[Path] | None = None,
    ) -> Path:
        """Capture each host dir to ``.info``, then merge all.

        Args:
            host_gcda_dirs: Per-host directories containing ``.gcda`` files.
            gcno_dir: Directory containing ``.gcno`` files (from the build).
                Used as the fallback for all hosts when *gcno_dirs* is not
                provided.
            work_dir: Scratch directory for intermediate ``.info`` files.
            toolchains: Per-host toolchains, parallel to *host_gcda_dirs*.
                Each entry can be ``None`` to use instance defaults.
                If the entire list is ``None``, defaults are used for all.
            gcno_dirs: Per-host ``.gcno`` directories, parallel to
                *host_gcda_dirs*.  When provided, host ``i`` is captured
                against ``gcno_dirs[i]`` instead of the shared *gcno_dir*
                fallback.  Must have the same length as *host_gcda_dirs*.

        Returns:
            Path to the merged ``.info`` file.

        Raises:
            ValueError: If *gcno_dirs* is provided but its length differs
                from *host_gcda_dirs*.
        """
        if gcno_dirs is not None and len(gcno_dirs) != len(host_gcda_dirs):
            raise ValueError(
                f"gcno_dirs length ({len(gcno_dirs)}) must match "
                f"host_gcda_dirs length ({len(host_gcda_dirs)})"
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        info_files: list[Path] = []

        for i, gcda_dir in enumerate(host_gcda_dirs):
            tc = toolchains[i] if toolchains else None
            g = gcno_dirs[i] if gcno_dirs else gcno_dir
            info_out = work_dir / f"host_{i}.info"
            await self.capture(gcda_dir, g, info_out, toolchain=tc)
            info_files.append(info_out)

        if len(info_files) == 1:
            return info_files[0]

        merged = work_dir / "merged.info"
        return await self.merge_info_files(info_files, merged)
