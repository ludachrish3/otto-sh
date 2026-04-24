"""Per-host toolchain configuration for coverage and build tools.

Each :class:`~otto.host.remoteHost.RemoteHost` carries a :class:`Toolchain`
that describes where ``lcov``, ``gcov``, and the compiler live.  Tool
paths are stored **relative to the sysroot** so that a single
``sysroot`` change is enough to switch an entire cross-toolchain.

Sensible defaults (``sysroot='/'``, tools under ``usr/bin/``) mean
hosts with system-installed toolchains need no configuration at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Toolchain:
    """Describes the toolchain associated with a host's products.

    Attributes:
        sysroot: Root directory of the toolchain installation.
            Defaults to ``/`` (system toolchain).
        lcov: Path to the ``lcov`` binary, relative to *sysroot*.
        gcov: Path to the ``gcov`` binary (or an ``llvm-cov`` wrapper),
            relative to *sysroot*.
    """

    sysroot: Path = Path('/')
    """Root directory of the toolchain installation."""

    lcov: Path = Path('usr/bin/lcov')
    """Path to ``lcov``, relative to *sysroot*."""

    gcov: Path = Path('usr/bin/gcov')
    """Path to ``gcov`` (or ``llvm-cov`` wrapper), relative to *sysroot*."""

    @property
    def lcov_bin(self) -> str:
        """Absolute path to the ``lcov`` binary."""
        return str(self.sysroot / self.lcov)

    @property
    def gcov_bin(self) -> str:
        """Absolute path to the ``gcov`` binary."""
        return str(self.sysroot / self.gcov)

    @property
    def compiler(self) -> Path | None:
        """Derive the compiler path from the gcov path.

        For GCC toolchains the gcov binary name mirrors the compiler
        (e.g. ``arm-linux-gnueabihf-gcov`` → ``arm-linux-gnueabihf-gcc``).

        For Clang/LLVM toolchains where the gcov path contains
        ``llvm-cov``, the compiler is assumed to be ``clang`` in the
        same directory.

        Returns ``None`` when the compiler cannot be inferred.
        """
        name = self.gcov.name

        # Clang: llvm-cov → clang (sibling in same directory)
        if 'llvm-cov' in name:
            return self.sysroot / self.gcov.parent / 'clang'

        # GCC: *gcov* → *gcc*
        gcc_name = re.sub(r'gcov', 'gcc', name)
        if gcc_name != name:
            return self.sysroot / self.gcov.parent / gcc_name

        return None
