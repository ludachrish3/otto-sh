"""The otto_kgcov kernel-module library, shipped in the wheel.

This directory holds the library's C sources, so a wheel carries them;
:mod:`otto.kgcov.library` is otto's side of it: the files an export copies
into a user's repo, the export and the check of such a copy, and the
interface number a built ``.ko`` must report.

Every name is exported lazily (PEP 562), the shape every otto package shares.
The resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .library import INTERFACE as INTERFACE
    from .library import LIBRARY_DIR as LIBRARY_DIR
    from .library import LOCAL_HEADER as LOCAL_HEADER
    from .library import SHIPPED_FILES as SHIPPED_FILES
    from .library import VERSION_HEADER as VERSION_HEADER
    from .library import CheckResult as CheckResult
    from .library import ExportResult as ExportResult
    from .library import check_tree as check_tree
    from .library import export_tree as export_tree
    from .library import exported_version as exported_version
    from .library import interface_of as interface_of
    from .library import modinfo_version as modinfo_version
    from .library import version_header as version_header

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CheckResult": "otto.kgcov.library",
    "ExportResult": "otto.kgcov.library",
    "INTERFACE": "otto.kgcov.library",
    "LIBRARY_DIR": "otto.kgcov.library",
    "LOCAL_HEADER": "otto.kgcov.library",
    "SHIPPED_FILES": "otto.kgcov.library",
    "VERSION_HEADER": "otto.kgcov.library",
    "check_tree": "otto.kgcov.library",
    "export_tree": "otto.kgcov.library",
    "exported_version": "otto.kgcov.library",
    "interface_of": "otto.kgcov.library",
    "modinfo_version": "otto.kgcov.library",
    "version_header": "otto.kgcov.library",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.kgcov's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "INTERFACE",
    "LIBRARY_DIR",
    "LOCAL_HEADER",
    "SHIPPED_FILES",
    "VERSION_HEADER",
    "CheckResult",
    "ExportResult",
    "check_tree",
    "export_tree",
    "exported_version",
    "interface_of",
    "modinfo_version",
    "version_header",
]
