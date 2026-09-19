"""Resolve the gcov that reads a build's counters from the data's own stamp.

A ``.gcno``/``.gcda`` embeds no compiler path, but its 8-byte header carries
the gcov *format version stamp* the producing compiler wrote: GCC stamps its
own release (``B33*`` for 13.3), while ``clang --coverage`` always stamps
the GCC 4.8-era ``408*`` (``402*`` on old LLVM) it emulates. That stamp is
the one reliable signal, and it names two things:

* **the family** — GNU gcov refuses (or worse, crashes on) clang's files; an
  LLVM stamp means the counters must be read by ``llvm-cov gcov``, resolved
  from ``PATH`` here;
* **the GCC major** — gcov reads only its own major's record format, so a
  GCC stamp from another major than the system gcov's is read by that
  major's ``gcov-<major>`` from ``PATH``.

A cross toolchain cannot be *located* from a stamp (it names a compiler,
never a target) and must be named in the host's ``toolchain`` configuration.

``lcov --gcov-tool`` takes a single command, but Clang's reader is the
two-word ``llvm-cov gcov`` — :func:`ensure_gcov_tool` bridges that with a
generated one-word wrapper script at capture time.
"""

import itertools
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

from ..host.toolchain import Toolchain
from .errors import CoverageToolMissingError

logger = logging.getLogger(__name__)

# Version stamps LLVM's gcov-compatible writer emits by default: clang
# emulated GCC 4.2 historically and emulates 4.8 on every current release.
# Real GCC builds of that vintage predate every supported bed; in practice a
# .gcno carrying one of these stamps came from ``clang --coverage``.
_LLVM_STAMPS = frozenset({"402*", "408*"})

# llvm-cov binary names: plain, or Debian/Ubuntu's versioned `llvm-cov-18`.
_LLVM_COV_NAME = re.compile(r"^llvm-cov(-\d+)?$")

# Files sampled for a stamp per directory; one readable header decides.
_GCNO_SAMPLE_LIMIT = 5

# .gcno header: 4-byte magic + 4-byte version stamp.
_GCNO_HEADER_LEN = 8


# gcov file magics. A little-endian target stores the word reversed
# (``oncg``/``adcg``) and the stamp characters with it; a big-endian one
# stores both as written.
_GCOV_MAGIC_REVERSED = {b"oncg", b"adcg"}
_GCOV_MAGIC_AS_IS = {b"gcno", b"gcda"}

_GCOV_VERSION_RE = re.compile(r"\b(\d+)\.\d+(?:\.\d+)?\b")

# A version stamp is always 4 characters: <lead><major digit><minor digit>*.
_GCOV_STAMP_LEN = 4


def read_gcov_version(path: Path) -> str | None:
    """Return a ``.gcno``/``.gcda`` file's 4-char version stamp (``'B33*'``, ``'408*'``).

    Both file kinds carry the stamp at bytes 4:8 behind their magic. Handles
    both byte orders. Returns ``None`` for anything unreadable or not a gcov
    file.
    """
    try:
        with path.open("rb") as f:
            header = f.read(_GCNO_HEADER_LEN)
    except OSError:
        return None
    if len(header) < _GCNO_HEADER_LEN:
        return None
    magic, version = header[:4], header[4:8]
    if magic in _GCOV_MAGIC_REVERSED:
        return version[::-1].decode(errors="replace")
    if magic in _GCOV_MAGIC_AS_IS:
        return version.decode(errors="replace")
    return None


def read_gcno_version(gcno: Path) -> str | None:
    """:func:`read_gcov_version` under the name the ``.gcno`` callers use."""
    return read_gcov_version(gcno)


def gcov_stamp_major(stamp: str) -> int | None:
    """Decode the GCC major a gcov version word names, or ``None``.

    gcc writes the word as ``<lead><major digit><minor digit>*``: the lead is
    ``'A'`` for majors 5-9 and ``'B'`` for 10-19 (``'A95*'`` is 9.5, ``'B24*'``
    12.4), each later letter adding ten. gcc 4 wrote a digit lead (``'407*'``),
    the form clang still emulates; that, and anything else that is not a
    capital letter followed by a digit, decodes to ``None``.
    """
    if len(stamp) != _GCOV_STAMP_LEN or not ("A" <= stamp[0] <= "Z") or not stamp[1].isdigit():
        return None
    return (ord(stamp[0]) - ord("A")) * 10 + int(stamp[1])


def system_gcov_major(gcov: str = "gcov") -> int | None:
    """Return the major *gcov* reports for ``--version``, or ``None``.

    ``gcov (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0`` is 13. The LAST
    version-like token on the line is the one that matters — a packager's
    parenthesized build string can carry one of its own, and it comes first:
    ``gcov (crosstool-NG 1.25.0.196_227d99d) 12.2.0`` is 12, not 1. ``None``
    when the tool is missing, exits non-zero, or prints no version on its
    first line; the caller then treats every GCC stamp as another major.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [gcov, "--version"], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    matches = list(_GCOV_VERSION_RE.finditer(first))
    return int(matches[-1].group(1)) if matches else None


def _first_stamp(files: Iterable[Path]) -> tuple[str, Path] | tuple[None, None]:
    """Return the stamp of the first readable gcov file among the first few of *files*."""
    for path in itertools.islice(files, _GCNO_SAMPLE_LIMIT):
        stamp = read_gcov_version(path)
        if stamp is not None:
            return stamp, path
    return None, None


def _toolchain_for(gcov: Path) -> Toolchain:
    """Build a host toolchain reading counters with *gcov* and the host's own ``lcov``."""
    host_lcov = shutil.which("lcov")
    lcov = Path(host_lcov) if host_lcov else Path("usr/bin/lcov")
    return Toolchain(sysroot=Path("/"), gcov=gcov, lcov=lcov)


def _missing_tool(
    gcda_dir: Path, stamp: str, major: int | None, tool: str, package: str
) -> CoverageToolMissingError:
    host, product = gcda_dir.parent.name, gcda_dir.name
    written_by = f"gcc {major}" if major is not None else "clang --coverage"
    return CoverageToolMissingError(
        f"Coverage data for product '{product}' on host '{host}' was written by "
        f"{written_by} (gcov stamp {stamp!r}) and needs `{tool}` to read it, but no "
        f"`{tool}` is on PATH. Install it (`apt install {package}`), or name the gcov "
        "to use in the host's `toolchain.gcov`."
    )


def discover_toolchain_from_gcda(gcda_dir: Path, *, system_major: int | None) -> Toolchain | None:
    """Return the gcov the ``.gcda`` files under *gcda_dir* need, or ``None`` for the default.

    *gcda_dir* is one ``<cov>/<host>/<product>`` directory; the stamp of its
    first readable ``.gcda`` decides. An LLVM stamp resolves ``llvm-cov`` from
    ``PATH``; a GCC stamp whose major is *system_major* (the system gcov's, or
    ``None`` when it could not be probed) returns ``None``; any other GCC major
    resolves ``gcov-<major>`` from ``PATH``; a stamp that decodes to no major
    returns ``None`` with one warning; a directory with no readable ``.gcda``
    returns ``None`` and is left to lcov.

    Raises:
        CoverageToolMissingError: the tool the data needs is not on ``PATH``.
    """
    stamp, sample = _first_stamp(sorted(gcda_dir.rglob("*.gcda")))
    if stamp is None or sample is None:
        logger.debug("No readable .gcda under %s; leaving the gcov choice to lcov", gcda_dir)
        return None
    host, product = gcda_dir.parent.name, gcda_dir.name
    if stamp in _LLVM_STAMPS:
        llvm_cov = _find_llvm_cov()
        if llvm_cov is None:
            raise _missing_tool(gcda_dir, stamp, None, "llvm-cov", "llvm")
        logger.info(
            "%s/%s: gcov stamp %r is clang's; reading counters with %s",
            host,
            product,
            stamp,
            llvm_cov,
        )
        return _toolchain_for(llvm_cov)
    major = gcov_stamp_major(stamp)
    if major is None:
        logger.warning(
            "%s: gcov stamp %r (from %s) decodes to no GCC major; using the default gcov",
            gcda_dir,
            stamp,
            sample.name,
        )
        return None
    if major == system_major:
        return None
    tool = f"gcov-{major}"
    found = shutil.which(tool)
    if found is None:
        raise _missing_tool(gcda_dir, stamp, major, tool, f"gcc-{major}")
    logger.info(
        "%s/%s: gcov stamp %r is gcc %d's; reading counters with %s",
        host,
        product,
        stamp,
        major,
        found,
    )
    return _toolchain_for(Path(found))


def discover_toolchain_from_gcno(gcno_dir: Path) -> Toolchain | None:
    """Infer the gcov tool family from the ``.gcno`` files under *gcno_dir*.

    The collect-time rule for an embedded build directory: a clang stamp
    resolves to an ``llvm-cov`` from ``PATH``; a GCC stamp returns ``None``
    (the host's configured or default cross gcov applies, and a mismatch
    fails loudly at capture with
    :class:`~otto.coverage.errors.CoverageToolVersionError`). A clang build
    without ``llvm-cov`` on ``PATH`` returns ``None`` with a warning.

    Args:
        gcno_dir: Directory tree containing the build's ``.gcno`` files.
    """
    stamp, sample = _first_stamp(gcno_dir.rglob("*.gcno"))
    if stamp is None or sample is None:
        logger.debug("No readable .gcno files under %s", gcno_dir)
        return None

    if stamp not in _LLVM_STAMPS:
        logger.debug(
            "%s carries GCC-family gcov stamp %r; leaving the default gcov in place",
            sample,
            stamp,
        )
        return None

    llvm_cov = _find_llvm_cov()
    if llvm_cov is None:
        logger.warning(
            "%s was produced by clang --coverage (gcov stamp %r), but no "
            "llvm-cov executable is on PATH; install llvm or set the host "
            "toolchain's gcov to an llvm-cov path.",
            sample,
            stamp,
        )
        return None

    logger.info(
        "Auto-discovered clang toolchain from %s (stamp %r): gcov=%s", sample, stamp, llvm_cov
    )
    return _toolchain_for(llvm_cov)


def ensure_gcov_tool(gcov: str, work_dir: Path) -> str:
    """Return *gcov* as a single-command tool that ``lcov --gcov-tool`` can exec.

    ``llvm-cov`` is gcov-compatible only through its ``gcov`` subcommand,
    and lcov rejects a two-word tool ("cannot access gcov tool"). When
    *gcov* names an ``llvm-cov`` binary, write (idempotently) and return a
    one-word wrapper script in *work_dir* that execs ``<llvm-cov> gcov``;
    every other tool passes through unchanged.
    """
    if not _LLVM_COV_NAME.match(Path(gcov).name):
        return gcov
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper = work_dir / "llvm-gcov-wrapper.sh"
    if not wrapper.exists():
        wrapper.write_text(f'#!/bin/sh\nexec {gcov} gcov "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(wrapper)


def _find_llvm_cov() -> Path | None:
    """Find ``llvm-cov`` on ``PATH``: the plain name, else the highest ``llvm-cov-<N>``."""
    plain = shutil.which("llvm-cov")
    if plain:
        return Path(plain)

    candidates: list[tuple[int, Path]] = []
    for path_dir in os.get_exec_path():
        for exe in Path(path_dir).glob("llvm-cov-*"):
            match = re.fullmatch(r"llvm-cov-(\d+)", exe.name)
            if match and os.access(exe, os.X_OK):
                candidates.append((int(match.group(1)), exe))
    if not candidates:
        return None
    return max(candidates)[1]
