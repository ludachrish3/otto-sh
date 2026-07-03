"""Resolve the gcov tool family from ``.gcno`` files.

A ``.gcno`` embeds no compiler path, but its 8-byte header carries the gcov
*format version stamp* the producing compiler wrote: GCC stamps its own
release (``B33*`` for 13.3), while ``clang --coverage`` always stamps the
GCC 4.8-era ``408*`` (``402*`` on old LLVM) it emulates. That stamp is the
one reliable family signal:

* **GCC stamp** — the default (or host-configured) ``gcov`` applies; a cross
  toolchain cannot be *located* from the ``.gcno`` alone and must be named in
  the host's ``toolchain`` configuration.
* **LLVM stamp** — GNU gcov refuses (or worse, crashes on) clang's files;
  the counters must be read by ``llvm-cov gcov``, which is resolved from
  ``PATH`` here.

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
from pathlib import Path

from ..host.toolchain import Toolchain

logger = logging.getLogger(__name__)

# Version stamps LLVM's gcov-compatible writer emits by default: clang
# emulated GCC 4.2 historically and emulates 4.8 on every current release.
# Real GCC builds of that vintage predate every supported bed; in practice a
# .gcno carrying one of these stamps came from ``clang --coverage``.
_LLVM_STAMPS = frozenset({"402*", "408*"})

# llvm-cov binary names: plain, or Debian/Ubuntu's versioned `llvm-cov-18`.
_LLVM_COV_NAME = re.compile(r"^llvm-cov(-\d+)?$")

_GCNO_SAMPLE_LIMIT = 5

# .gcno header: 4-byte magic + 4-byte version stamp.
_GCNO_HEADER_LEN = 8


def read_gcno_version(gcno: Path) -> str | None:
    """Return a ``.gcno``'s 4-char version stamp (e.g. ``'B33*'``, ``'408*'``).

    Handles both byte orders: a little-endian target stores the magic as
    ``oncg`` and the stamp characters reversed; a big-endian target stores
    ``gcno`` and the stamp as-is. Returns ``None`` for anything unreadable
    or not a ``.gcno``.
    """
    try:
        with gcno.open("rb") as f:
            header = f.read(_GCNO_HEADER_LEN)
    except OSError:
        return None
    if len(header) < _GCNO_HEADER_LEN:
        return None
    magic, version = header[:4], header[4:8]
    if magic == b"oncg":
        return version[::-1].decode(errors="replace")
    if magic == b"gcno":
        return version.decode(errors="replace")
    return None


def discover_toolchain_from_gcno(gcno_dir: Path) -> Toolchain | None:
    """Infer the gcov tool family from the ``.gcno`` files under *gcno_dir*.

    Reads the version stamp of a small sample of ``.gcno`` files. A clang
    stamp resolves to an ``llvm-cov`` from ``PATH``; a GCC stamp returns
    ``None`` (the caller's default gcov already matches same-host GCC
    builds, and a mismatched cross build fails loudly at capture with
    :class:`~otto.coverage.errors.CoverageToolVersionError`).

    Args:
        gcno_dir: Directory tree containing the build's ``.gcno`` files.

    Returns:
        A :class:`~otto.host.toolchain.Toolchain` pointing at ``llvm-cov``
        for clang builds, ``None`` otherwise.
    """
    stamp: str | None = None
    sample: Path | None = None
    for gcno in itertools.islice(gcno_dir.rglob("*.gcno"), _GCNO_SAMPLE_LIMIT):
        stamp = read_gcno_version(gcno)
        if stamp is not None:
            sample = gcno
            break

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

    host_lcov = shutil.which("lcov")
    lcov = Path(host_lcov) if host_lcov else Path("usr/bin/lcov")
    logger.info(
        "Auto-discovered clang toolchain from %s (stamp %r): gcov=%s", sample, stamp, llvm_cov
    )
    return Toolchain(sysroot=Path("/"), gcov=llvm_cov, lcov=lcov)


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
