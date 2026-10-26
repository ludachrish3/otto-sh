"""Resolve the gcov that reads a build's counters from the data's own stamp.

A ``.gcno``/``.gcda`` embeds no compiler path, but its header carries the
gcov *format version stamp* its writer chose: GCC stamps its own release
(``B33*`` for 13.3); ``clang --coverage`` stamps the GCC 4.8-era ``408*``
(``402*`` on old LLVM) up to clang 20 and gcc 11.1.0's ``B11*`` from clang
21. The stamp alone chooses the reader
(:func:`~otto.host.gcovformat.is_llvm_stamp`):

* **llvm-cov's stamps** (``402*``, ``408*``, ``B11*``) are read by
  ``llvm-cov gcov``, resolved from ``PATH`` here. GNU gcov refuses or
  crashes on ``402*``/``408*``, and a gcov 12 or later reads ``B11*`` as
  empty. A gcc 11.1.0 build, which stamps ``B11*`` too, names its
  ``gcov-11`` by hand;
* **every other stamp** names a GCC major, and gcov reads only its own
  major's record format, so a stamp from another major than the system
  gcov's is read by that major's ``gcov-<major>`` from ``PATH``.

A cross toolchain cannot be *located* from a stamp (it names a compiler,
never a target) and must be named by hand: in the host's ``toolchain`` for a
product, in the tier's own ``toolchain`` for a unit capture. The rule
(:func:`gcov_for_stamp`) names whichever subject its caller gives it.

``lcov --gcov-tool`` takes a single command, but ``llvm-cov`` reads only
as the two-word ``llvm-cov gcov`` — :func:`ensure_gcov_tool` bridges that
with a generated one-word wrapper script at capture time.
"""

import hashlib
import itertools
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..host.toolchain import Toolchain
from .errors import CoverageToolMissingError
from .gcovformat import LLVM_COV_STAMPS, gcov_stamp_major, is_llvm_stamp, read_gcov_version

logger = logging.getLogger(__name__)

# llvm-cov binary names: plain, or Debian/Ubuntu's versioned `llvm-cov-18`.
_LLVM_COV_NAME = re.compile(r"^llvm-cov(-\d+)?$")

# Files sampled for a stamp per directory; one readable header decides.
_GCNO_SAMPLE_LIMIT = 5

# llvm-cov's stamps as a message names them: ``402*/408*/B11*``.
_LLVM_COV_STAMPS = "/".join(sorted(LLVM_COV_STAMPS))

_GCOV_VERSION_RE = re.compile(r"\b(\d+)\.\d+(?:\.\d+)?\b")


def read_gcno_version(gcno: Path) -> str | None:
    """:func:`~otto.host.gcovformat.read_gcov_version` under the name the ``.gcno`` callers use."""
    return read_gcov_version(gcno)


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


@dataclass(frozen=True)
class StampSubject:
    """Whose coverage data a stamp came from, as the stamp rule's diagnostics name it."""

    name: str
    """The subject in a sentence, such as ``product 'app' on host 'test1'``."""
    log: str
    """The subject's short label at the head of a log line, such as ``test1/app``."""
    remedy: str
    """How to name the reader by hand, as an imperative clause."""
    where: Path
    """The directory whose data it is, as a warning about its stamp names it."""


def _product_subject(gcda_dir: Path) -> StampSubject:
    """Return the subject of one ``<cov>/<host>/<product>`` directory."""
    host, product = gcda_dir.parent.name, gcda_dir.name
    return StampSubject(
        name=f"product '{product}' on host '{host}'",
        log=f"{host}/{product}",
        remedy="name the gcov to use in the host's `toolchain.gcov`",
        where=gcda_dir,
    )


def _build_subject(gcno_dir: Path) -> StampSubject:
    """Return the subject of one embedded build directory, read at collection."""
    return StampSubject(
        name=f"the build in {gcno_dir}",
        log=str(gcno_dir),
        remedy="name the gcov to use in the host's `toolchain.gcov`",
        where=gcno_dir,
    )


def _missing_tool(
    subject: StampSubject, stamp: str, major: int | None, tool: str, package: str
) -> CoverageToolMissingError:
    """Name the stamp and the reader it needs: a GCC *major*'s ``gcov-<major>``, or llvm-cov."""
    if major is None:
        needs = f"has gcov stamp {stamp!r}, read by `{tool}`,"
    else:
        needs = f"was written by gcc {major} (gcov stamp {stamp!r}) and needs `{tool}` to read it,"
    return CoverageToolMissingError(
        f"Coverage data for {subject.name} {needs} but no `{tool}` is on PATH. Install it "
        f"(`apt install {package}`), or {subject.remedy}."
    )


def gcov_for_stamp(
    stamp: str, sample: Path, *, system_major: int | None, subject: StampSubject
) -> Path | None:
    """Return the reader a gcov *stamp* needs, or ``None`` for the default ``gcov``.

    One of llvm-cov's stamps (``402*``, ``408*``, ``B11*``) resolves
    ``llvm-cov`` from ``PATH``; a GCC stamp whose major is *system_major*
    (the system gcov's, or ``None`` when it could not be probed) returns
    ``None``; any other GCC major resolves ``gcov-<major>`` from ``PATH``; a
    stamp that decodes to no major returns ``None`` with one warning naming
    the subject's directory and *sample*, the file it came from. Every
    diagnostic names *subject*.

    Raises:
        CoverageToolMissingError: the tool the data needs is not on ``PATH``.
    """
    if is_llvm_stamp(stamp):
        llvm_cov = _find_llvm_cov()
        if llvm_cov is None:
            raise _missing_tool(subject, stamp, None, "llvm-cov", "llvm")
        logger.info(
            "%s: gcov stamp %r is llvm-cov's (stamps %s); reading counters with %s",
            subject.log,
            stamp,
            _LLVM_COV_STAMPS,
            llvm_cov,
        )
        return llvm_cov
    major = gcov_stamp_major(stamp)
    if major is None:
        logger.warning(
            "%s: gcov stamp %r (from %s) decodes to no GCC major; using the default gcov",
            subject.where,
            stamp,
            sample.name,
        )
        return None
    if major == system_major:
        return None
    tool = f"gcov-{major}"
    found = shutil.which(tool)
    if found is None:
        raise _missing_tool(subject, stamp, major, tool, f"gcc-{major}")
    logger.info(
        "%s: gcov stamp %r is gcc %d's; reading counters with %s",
        subject.log,
        stamp,
        major,
        found,
    )
    return Path(found)


def discover_toolchain_from_gcda(gcda_dir: Path, *, system_major: int | None) -> Toolchain | None:
    """Return the gcov the ``.gcda`` files under *gcda_dir* need, or ``None`` for the default.

    *gcda_dir* is one ``<cov>/<host>/<product>`` directory; the stamp of its
    first readable ``.gcda`` decides, by :func:`gcov_for_stamp`. A directory
    with no readable ``.gcda`` returns ``None`` and is left to lcov.

    Raises:
        CoverageToolMissingError: the tool the data needs is not on ``PATH``.
    """
    stamp, sample = _first_stamp(sorted(gcda_dir.rglob("*.gcda")))
    if stamp is None or sample is None:
        logger.debug("No readable .gcda under %s; leaving the gcov choice to lcov", gcda_dir)
        return None
    gcov = gcov_for_stamp(
        stamp, sample, system_major=system_major, subject=_product_subject(gcda_dir)
    )
    return None if gcov is None else _toolchain_for(gcov)


def discover_toolchain_from_gcno(gcno_dir: Path) -> Toolchain | None:
    """Return a toolchain reading with the ``llvm-cov`` the ``.gcno`` files under *gcno_dir* need.

    ``None`` when they need none.

    The collect-time rule for an embedded build directory: one of llvm-cov's
    stamps resolves an ``llvm-cov`` from ``PATH``, by :func:`gcov_for_stamp`;
    a GCC stamp returns ``None`` (the host's configured or default cross gcov
    applies, and a mismatch fails loudly at capture with
    :class:`~otto.coverage.errors.CoverageToolVersionError`), as does a
    directory with no readable ``.gcno``.

    Args:
        gcno_dir: Directory tree containing the build's ``.gcno`` files.

    Raises:
        CoverageToolMissingError: the stamp is llvm-cov's and no ``llvm-cov``
            is on ``PATH``.
    """
    stamp, sample = _first_stamp(gcno_dir.rglob("*.gcno"))
    if stamp is None or sample is None:
        logger.debug("No readable .gcno files under %s", gcno_dir)
        return None
    if not is_llvm_stamp(stamp):
        logger.debug(
            "%s carries GCC-family gcov stamp %r; leaving the default gcov in place",
            sample,
            stamp,
        )
        return None
    llvm_cov = gcov_for_stamp(stamp, sample, system_major=None, subject=_build_subject(gcno_dir))
    return None if llvm_cov is None else _toolchain_for(llvm_cov)


def is_llvm_cov(tool: str) -> bool:
    """Return whether *tool*'s basename is an ``llvm-cov`` (``llvm-cov`` or ``llvm-cov-<N>``)."""
    return _LLVM_COV_NAME.match(Path(tool).name) is not None


def ensure_gcov_tool(gcov: str, work_dir: Path) -> str:
    """Return *gcov* as a single-command tool that ``lcov --gcov-tool`` can exec.

    ``llvm-cov`` is gcov-compatible only through its ``gcov`` subcommand,
    and lcov rejects a two-word tool ("cannot access gcov tool"). When
    *gcov* names an ``llvm-cov`` binary, return a one-word wrapper script in
    *work_dir* that execs ``<llvm-cov> gcov``; every other tool passes
    through unchanged. The wrapper is named after the executable it wraps,
    so two readers share no wrapper, and it is rewritten whenever its
    content is not the one *gcov* needs.
    """
    if not is_llvm_cov(gcov):
        return gcov
    work_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(gcov.encode()).hexdigest()[:16]
    wrapper = work_dir / f"llvm-gcov-{key}.sh"
    content = f'#!/bin/sh\nexec {shlex.quote(gcov)} gcov "$@"\n'
    if not wrapper.is_file() or wrapper.read_text() != content:
        wrapper.write_text(content)
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
