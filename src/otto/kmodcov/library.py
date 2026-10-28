"""What otto knows about the otto_kmodcov library it ships, and how it hands it over.

The library's C sources sit beside this module in the ``otto.kmodcov`` package,
so a wheel carries them: ``otto cov kmodcov export`` copies
:data:`SHIPPED_FILES` into a user's project (vendored and committed there; the
user's build system builds the ``.ko``), and ``check`` compares such a copy
with what is shipped here.

The library and otto agree on an INTERFACE NUMBER: the sysfs layout
(``/sys/module/<module>/kmodcov/{dump,reset}``), the ``cov_dir``
module parameter and the consumer macros. It is ``KMODCOV_INTERFACE`` in
``kmodcov.h`` and :data:`INTERFACE` here (a guard holds the two equal), and a
built module reports it through ``MODULE_VERSION`` as
``<otto version>+kmodcov<interface>``, which :func:`modinfo_version` reads
back off the ``.ko`` without any host tool.

The version header (:data:`VERSION_HEADER`) is the ONLY generated file: an
export writes the exporting otto's version into it, and the in-tree copy
says ``"source"``. :data:`LOCAL_HEADER` is the user's override for
kernel-facing names and is never exported nor compared.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..vendoring import CheckResult as BaseCheckResult
from ..vendoring import check_result, compare_files, export_files
from .formats import KMODCOV_INTERFACE_READ_VERSIONS

[INTERFACE] = KMODCOV_INTERFACE_READ_VERSIONS
"""The library interface this otto drives and ships; equals ``KMODCOV_INTERFACE`` in ``kmodcov.h``.

The one interface otto declares it reads (``otto.kmodcov.formats``),
unpacked so a second fails at import until the shipped library chooses."""

SHIPPED_FILES = (
    "Kbuild",
    "Makefile",
    "build.sh",
    "consumer.mk",
    "kmodcov.lds",
    "kmodcov.c",
    "kmodcov.h",
    "kmodcov_compat.h",
    "kmodcov_gcov.h",
    "kmodcov_gcc.c",
    "kmodcov_gcc_abi.c",
    "kmodcov_clang.c",
    "README.md",
)
"""Every file an export writes verbatim, and every file a check compares."""

VERSION_HEADER = "kmodcov_version.h"
"""The generated version header; an export writes ``#define KMODCOV_OTTO_VERSION "<version>"``."""

LOCAL_HEADER = "kmodcov_local.h"
"""The user's override header, force-included by ``Kbuild`` when present and never shipped."""

LIBRARY_DIR = Path(__file__).parent
"""Where the shipped sources live (the ``otto.kmodcov`` package directory, beside this module)."""

_VERSION_LINE = re.compile(r'^#define KMODCOV_OTTO_VERSION "([^"]*)"$', re.MULTILINE)
_INTERFACE_SUFFIX = re.compile(r"\+kmodcov(\d+)$")
_MODINFO_VERSION = re.compile(rb"(?:^|\x00)version=([^\x00]*)")


def version_header(otto_version: str) -> str:
    """Render :data:`VERSION_HEADER`'s exact text for *otto_version*."""
    return (
        "/* Written by otto cov kmodcov export; the otto that exported these sources. */\n"
        f'#define KMODCOV_OTTO_VERSION "{otto_version}"\n'
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
class CheckResult(BaseCheckResult):
    """What :func:`check_tree` found: the shared result plus what only this library reports."""

    exported_by: str | None = None
    local_override: bool = False


def export_tree(dest: Path) -> ExportResult:
    """Write the shipped library into *dest*, overwriting; the version header names this otto.

    Never refuses: the copy is committed in the user's git repository, so their own
    diff is the review of what a re-export changed. :data:`LOCAL_HEADER` is never
    touched.
    """
    from ..version import get_version

    version = get_version()
    result = ExportResult(directory=dest, version=version)
    result.changed = export_files(LIBRARY_DIR, list(SHIPPED_FILES), dest)
    header = version_header(version)
    target = dest / VERSION_HEADER
    if not target.is_file() or target.read_text(encoding="utf-8") != header:
        target.write_text(header, encoding="utf-8")
        result.changed.append(VERSION_HEADER)
    return result


def check_tree(directory: Path) -> CheckResult:
    """Compare *directory* with the shipped library byte for byte.

    ``absent`` when there is no ``kmodcov.h`` there at all; otherwise every
    shipped file must be present and identical for ``current``. The version
    header is read for the message, not compared (an export names its otto;
    the tree says ``"source"``), and :data:`LOCAL_HEADER` is reported, never
    compared.
    """
    found = compare_files(LIBRARY_DIR, list(SHIPPED_FILES), directory)
    base = check_result(directory, found, absent=not (directory / "kmodcov.h").is_file())
    return CheckResult(
        **vars(base),
        exported_by=exported_version(directory),
        local_override=(directory / LOCAL_HEADER).is_file(),
    )
