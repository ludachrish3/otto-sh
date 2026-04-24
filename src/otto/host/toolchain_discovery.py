"""Auto-discover the toolchain from ``.gcno`` files.

When a host has no explicit :class:`~otto.host.toolchain.Toolchain`
configured, the coverage pipeline can attempt to infer the correct
``gcov`` (and by extension ``lcov``) from compiler paths embedded in
``.gcno`` files produced by ``gcc --coverage`` or ``clang --coverage``.

Both GCC and Clang families are supported:

* **GCC**: ``arm-linux-gnueabihf-gcc`` → ``arm-linux-gnueabihf-gcov``
* **Clang**: ``clang`` → generates a wrapper script that invokes
  ``llvm-cov gcov`` (required because ``lcov --gcov-tool`` takes a
  single command).
"""

from __future__ import annotations

import logging
import re
import stat
from pathlib import Path

from ..host.localHost import LocalHost
from ..host.toolchain import Toolchain

logger = logging.getLogger(__name__)

# Matches absolute paths to gcc/g++ or clang/clang++ inside a bin/ directory.
_COMPILER_RE = re.compile(
    r'(/\S+/bin/\S*(?:gcc|g\+\+|clang\+\+|clang))\b'
)


async def discover_toolchain_from_gcno(
    gcno_dir: Path,
    localhost: LocalHost,
    work_dir: Path | None = None,
) -> Toolchain | None:
    """Inspect ``.gcno`` files to discover the compiler toolchain.

    Runs ``strings`` on a sample of ``.gcno`` files and looks for
    absolute paths to ``gcc``, ``g++``, ``clang``, or ``clang++``.
    From that path the matching ``gcov`` binary and sysroot are
    derived.

    Args:
        gcno_dir: Directory containing ``.gcno`` files from the build.
        localhost: :class:`LocalHost` for running shell commands.
        work_dir: Directory for writing wrapper scripts (Clang only).
            If ``None``, defaults to ``gcno_dir / '_toolchain_work'``.

    Returns:
        A :class:`Toolchain` if discovery succeeds, ``None`` otherwise.
    """
    result = await localhost.oneshot(
        f"find {gcno_dir} -name '*.gcno' -type f | head -5",
        timeout=30,
    )
    if result.retcode != 0 or not result.output.strip():
        logger.debug("No .gcno files found in %s", gcno_dir)
        return None

    gcno_files = result.output.strip().splitlines()

    for gcno in gcno_files:
        strings_result = await localhost.oneshot(f"strings {gcno}", timeout=30)
        if strings_result.retcode != 0:
            continue

        for line in strings_result.output.splitlines():
            match = _COMPILER_RE.search(line.strip())
            if match:
                compiler_path = Path(match.group(1))
                toolchain = _toolchain_from_compiler(
                    compiler_path, work_dir or gcno_dir / '_toolchain_work',
                )
                if toolchain is not None:
                    logger.info(
                        "Auto-discovered toolchain from %s: sysroot=%s gcov=%s",
                        gcno, toolchain.sysroot, toolchain.gcov,
                    )
                    return toolchain

    logger.debug("Could not discover toolchain from .gcno files in %s", gcno_dir)
    return None


def _toolchain_from_compiler(compiler_path: Path, work_dir: Path) -> Toolchain | None:
    """Derive a :class:`Toolchain` from a discovered compiler path.

    Args:
        compiler_path: Absolute path to the compiler binary
            (e.g. ``/opt/arm/bin/arm-linux-gnueabihf-gcc``).
        work_dir: Directory for writing wrapper scripts (Clang only).

    Returns:
        A :class:`Toolchain` or ``None`` if the compiler family cannot
        be identified.
    """
    name = compiler_path.name
    bin_dir = compiler_path.parent          # e.g. /opt/arm/bin

    # Walk up from bin/ to find sysroot.
    # Typical layout: <sysroot>/usr/bin/gcc  or  <sysroot>/bin/gcc
    sysroot = _derive_sysroot(bin_dir)

    if _is_clang(name):
        return _clang_toolchain(bin_dir, sysroot, work_dir)

    if _is_gcc(name):
        return _gcc_toolchain(name, bin_dir, sysroot)

    return None


def _is_gcc(name: str) -> bool:
    return bool(re.search(r'(?:^|-)gcc$|(?:^|-)g\+\+$', name))


def _is_clang(name: str) -> bool:
    return bool(re.search(r'(?:^|-)clang(?:\+\+)?$', name))


def _derive_sysroot(bin_dir: Path) -> Path:
    """Walk up from the ``bin/`` directory to find the sysroot.

    Convention: if ``bin_dir`` ends in ``usr/bin``, sysroot is two
    levels up. If it ends in just ``bin``, sysroot is one level up.
    """
    if bin_dir.parent.name == 'usr':
        return bin_dir.parent.parent  # /opt/arm/usr/bin → /opt/arm
    return bin_dir.parent              # /opt/arm/bin → /opt/arm


def _gcc_toolchain(compiler_name: str, bin_dir: Path, sysroot: Path) -> Toolchain:
    """Build a :class:`Toolchain` for a GCC-family compiler."""
    gcov_name = re.sub(r'g\+\+', 'gcov', re.sub(r'gcc', 'gcov', compiler_name))
    gcov_abs = bin_dir / gcov_name

    try:
        gcov_rel = gcov_abs.relative_to(sysroot)
    except ValueError:
        gcov_rel = Path('usr/bin/gcov')

    return Toolchain(sysroot=sysroot, gcov=gcov_rel)


def _clang_toolchain(bin_dir: Path, sysroot: Path, work_dir: Path) -> Toolchain:
    """Build a :class:`Toolchain` for a Clang/LLVM compiler.

    ``lcov --gcov-tool`` requires a single command, but Clang's
    equivalent is ``llvm-cov gcov`` (two words).  This function
    generates a small wrapper script in *work_dir*.
    """
    llvm_cov = bin_dir / 'llvm-cov'
    wrapper = _create_llvm_cov_wrapper(llvm_cov, work_dir)

    try:
        gcov_rel = wrapper.relative_to(sysroot)
    except ValueError:
        # Wrapper lives outside sysroot — store as absolute in gcov field
        # and use '/' as sysroot so gcov_bin resolves correctly.
        return Toolchain(sysroot=Path('/'), gcov=wrapper)

    return Toolchain(sysroot=sysroot, gcov=gcov_rel)


def _create_llvm_cov_wrapper(llvm_cov: Path, work_dir: Path) -> Path:
    """Write a wrapper script that invokes ``llvm-cov gcov``.

    Returns the path to the wrapper.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper = work_dir / 'llvm-gcov-wrapper.sh'

    if not wrapper.exists():
        wrapper.write_text(
            f'#!/bin/sh\nexec {llvm_cov} gcov "$@"\n'
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return wrapper
