"""Per-host toolchain configuration for coverage and build tools.

Every host carries a :class:`Toolchain` that describes where ``lcov``,
``gcov``, and the compiler live.  Tool paths are stored **relative to the
sysroot** so that a single ``sysroot`` change is enough to switch an entire
cross-toolchain.

Sensible defaults (``sysroot='/'``, tools under ``usr/bin/``) mean
hosts with system-installed toolchains need no configuration at all.

A toolchain may additionally declare :class:`ToolchainTool` entries — the
artifacts otto *installs onto* the host (a cross-built ``gdbserver``, a runtime
``.so``) as opposed to the binaries above, which otto only *reads from* the
toolchain to process coverage.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ToolchainTool:
    """One installable toolchain tool (gdb, strace, a runtime .so, …).

    Declared per host in ``lab.json``'s ``toolchain.tools``; installed by
    :meth:`~otto.host.host.BaseHost.install_toolchain_tools`. These often land
    in root-owned directories, hence *user* and *mode* per tool.
    """

    name: str
    """Tool name, and the filename it is installed as under *dest*.

    This is a rename target, not a label. ``put`` lands every file under its
    SOURCE basename — no transfer backend renames (they all write
    ``dest_dir / src.name``) — so when *name* differs from ``source.name``,
    :meth:`~otto.host.host.BaseHost.install_toolchain_tools` issues an explicit
    ``mv`` before taking ownership. That is what makes a cross-built
    ``arm-linux-gnueabihf-gdb`` installable as plain ``gdb``, which is the
    common case this field exists for."""

    source: Path
    """Local path to the artifact to transfer."""

    dest: Path
    """Destination directory on the host."""

    user: str = "root"
    """Owner applied after transfer (``chown``), once the tool is at *name*."""

    mode: str = "755"
    """Octal permission string applied on transfer."""


@dataclass(slots=True)
class Toolchain:
    """Describes the toolchain associated with a host's products.

    Every field is documented by its own attribute docstring below. There is
    deliberately no ``Attributes:`` block duplicating them: with
    ``napoleon_use_ivar``, such a block becomes ``:ivar:`` fields that Sphinx
    matches against the rendered ``__init__`` signature, and a
    ``default_factory`` field renders as ``<factory>``, which the matcher
    cannot parse — every name in the block then reports as unmatched and
    ``-W`` fails the docs build.
    """

    sysroot: Path = Path("/")
    """Root directory of the toolchain installation."""

    lcov: Path = Path("usr/bin/lcov")
    """Path to ``lcov``, relative to *sysroot*."""

    gcov: Path = Path("usr/bin/gcov")
    """Path to ``gcov`` (or ``llvm-cov`` wrapper), relative to *sysroot*."""

    tools: list[ToolchainTool] = field(default_factory=list)
    """Tools this toolchain installs onto the host — none by default.

    NO COLON in the summary line above: napoleon reads an attribute
    docstring's ``prefix: rest`` as ``type: description``, so a colon here
    makes Sphinx look the prefix up as a class and ``-W -n`` fails on the
    missing target (same trap as ``Status.NotRun``).

    Unlike the coverage binaries above — which otto *reads* out of the
    toolchain — these are artifacts otto *places* on the host, so they carry
    absolute source/destination paths rather than sysroot-relative ones."""

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
        if "llvm-cov" in name:
            return self.sysroot / self.gcov.parent / "clang"

        # GCC: *gcov* → *gcc*
        gcc_name = re.sub(r"gcov", "gcc", name)
        if gcc_name != name:
            return self.sysroot / self.gcov.parent / gcc_name

        return None
