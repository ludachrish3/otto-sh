"""The otto_kgcov kernel-module library, shipped in the wheel.

This directory holds the library's C sources beside this module so a wheel
carries them: ``otto cov kgcov export`` copies :data:`SHIPPED_FILES` into a
user's repo (vendored and committed there; the user's build system builds
the ``.ko``), and ``check`` compares such a copy with what is shipped here.

The library and otto agree on an INTERFACE NUMBER: the debugfs layout
(``/sys/kernel/debug/otto_kgcov/<module>/{dump,reset}``), the ``gcov_dir``
module parameter and the consumer macros. It is ``KGCOV_INTERFACE`` in
``kgcov.h`` and :data:`INTERFACE` here (a guard holds the two equal), and a
built module reports it through ``MODULE_VERSION`` as
``<otto version>+kgcov<interface>``, which :func:`modinfo_version` reads
back off the ``.ko`` without any host tool.

The version header (:data:`VERSION_HEADER`) is the ONLY generated file: an
export writes the exporting otto's version into it, and the in-tree copy
says ``"source"``. :data:`LOCAL_HEADER` is the user's override for
kernel-facing names and is never exported nor compared.
"""

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

INTERFACE = 1
"""The library interface this otto drives; equals ``KGCOV_INTERFACE`` in ``kgcov.h``."""

SHIPPED_FILES = (
    "Kbuild",
    "Makefile",
    "build.sh",
    "consumer.mk",
    "kgcov.c",
    "kgcov.h",
    "kgcov_gcov.h",
    "kgcov_gcc.c",
    "kgcov_gcc_abi.c",
    "kgcov_clang.c",
    "README.md",
)
"""Every file an export writes verbatim, and every file a check compares."""

VERSION_HEADER = "kgcov_version.h"
"""The generated version header, written at export as ``#define KGCOV_OTTO_VERSION "<version>"``."""

LOCAL_HEADER = "kgcov_local.h"
"""The user's override header, force-included by ``Kbuild`` when present and never shipped."""

LIBRARY_DIR = Path(str(files(__name__)))
"""Where the shipped sources live (this package's directory)."""

_VERSION_LINE = re.compile(r'^#define KGCOV_OTTO_VERSION "([^"]*)"$', re.MULTILINE)
_INTERFACE_SUFFIX = re.compile(r"\+kgcov(\d+)$")
_MODINFO_VERSION = re.compile(rb"(?:^|\x00)version=([^\x00]*)")


def version_header(otto_version: str) -> str:
    """Render :data:`VERSION_HEADER`'s exact text for *otto_version*."""
    return (
        "/* Written by otto cov kgcov export; the otto that exported these sources. */\n"
        f'#define KGCOV_OTTO_VERSION "{otto_version}"\n'
    )


def exported_version(directory: Path) -> str | None:
    """Read the otto version *directory*'s version header names, or ``None``."""
    header = directory / VERSION_HEADER
    if not header.is_file():
        return None
    m = _VERSION_LINE.search(header.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def modinfo_version(ko: Path) -> str | None:
    """Read the ``version=`` string a ``.ko``'s ``.modinfo`` carries, or ``None``.

    ``.modinfo`` is NUL-separated ``key=value`` text, so the key is matched only
    at a NUL boundary (``srcversion=`` never matches) and the value runs to the
    next NUL. A byte scan, not ``modinfo(8)``: no host tool, and a foreign-ISA
    ``.ko`` reads the same on the dev VM.
    """
    m = _MODINFO_VERSION.search(ko.read_bytes())
    return m.group(1).decode("ascii", "replace") if m else None


def interface_of(version: str) -> int | None:
    """Read the interface number a ``MODULE_VERSION`` string ends with."""
    m = _INTERFACE_SUFFIX.search(version)
    return int(m.group(1)) if m else None


@dataclass
class ExportResult:
    """What :func:`export_tree` did: the files it wrote differently from what was there."""

    directory: Path
    version: str
    changed: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    """What :func:`check_tree` found; ``state`` is ``current``, ``differs`` or ``absent``."""

    directory: Path
    state: str
    differing: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    exported_by: str | None = None
    local_override: bool = False

    @property
    def exit_code(self) -> int:
        """Map :attr:`state` to the exit status ``check`` leaves behind."""
        return {"current": 0, "differs": 1, "absent": 2}[self.state]


def export_tree(dest: Path) -> ExportResult:
    """Write the shipped library into *dest*, overwriting; the version header names this otto.

    Never refuses: the copy is committed in the user's repo, so their own diff
    is the review of what a re-export changed. :data:`LOCAL_HEADER` is never
    touched.
    """
    from ..version import get_version

    version = get_version()
    dest.mkdir(parents=True, exist_ok=True)
    result = ExportResult(directory=dest, version=version)
    for name in SHIPPED_FILES:
        source = LIBRARY_DIR / name
        data = source.read_bytes()
        # Only the EXECUTE bits travel with the copy, because they are the
        # only mode bits version control tracks: an installed wheel's 0644
        # against a umask-002 checkout's 0664 is not a difference, and
        # reporting it would name every file as written over a clean tree.
        x_bits = source.stat().st_mode & 0o111
        target = dest / name
        content_changed = not target.is_file() or target.read_bytes() != data
        if content_changed:
            target.write_bytes(data)
        mode = target.stat().st_mode & 0o777
        if content_changed or (mode & 0o111) != x_bits:
            target.chmod((mode & ~0o111) | x_bits)
            result.changed.append(name)
    header = version_header(version)
    target = dest / VERSION_HEADER
    if not target.is_file() or target.read_text(encoding="utf-8") != header:
        target.write_text(header, encoding="utf-8")
        result.changed.append(VERSION_HEADER)
    return result


def check_tree(directory: Path) -> CheckResult:
    """Compare *directory* with the shipped library byte for byte.

    ``absent`` when there is no ``kgcov.h`` there at all; otherwise every
    shipped file must be present and identical for ``current``. The version
    header is read for the message, not compared (an export names its otto;
    the tree says ``"source"``), and :data:`LOCAL_HEADER` is reported, never
    compared.
    """
    if not (directory / "kgcov.h").is_file():
        return CheckResult(directory=directory, state="absent")
    result = CheckResult(
        directory=directory,
        state="current",
        exported_by=exported_version(directory),
        local_override=(directory / LOCAL_HEADER).is_file(),
    )
    for name in SHIPPED_FILES:
        target = directory / name
        if not target.is_file():
            result.missing.append(name)
        elif target.read_bytes() != (LIBRARY_DIR / name).read_bytes():
            result.differing.append(name)
    if result.missing or result.differing:
        result.state = "differs"
    return result
