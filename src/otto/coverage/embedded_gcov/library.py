"""What otto knows about the embedded-gcov runtime patch and starter it ships.

NASA's embedded-gcov runtime is not vendored here. otto ships the one patch it
needs to build for Zephyr under a modern GNU compiler, and a starter C file
that includes the runtime. ``otto cov embedded-gcov export`` copies
:data:`SHIPPED_FILES` into a directory the user chooses, and ``check`` compares
such a copy with what is shipped here; both run on :mod:`otto.vendoring`, the
core ``otto cov kmodcov`` uses.
"""

from pathlib import Path

from ...vendoring import CheckResult, check_result, compare_files, export_files

SHIPPED_FILES = ["embedded-gcov-zephyr-gcc12plus.patch"]
"""Every file an export writes verbatim, and every file a check compares."""

STARTER_FILES = ["starter/cov_entry.c"]
"""Package data an init copies into a project; an export never writes them."""

LIBRARY_DIR = Path(__file__).parent
"""Where the shipped files live (the package directory, beside this module)."""


def export_patch(dest: Path) -> list[str]:
    """Write the shipped patch into *dest*, overwriting; return the names that changed."""
    return export_files(LIBRARY_DIR, SHIPPED_FILES, dest)


def check_patch(directory: Path) -> CheckResult:
    """Compare *directory* with the shipped patch byte for byte.

    ``absent`` when none of the shipped files is there; otherwise every one
    must be present and identical for ``current``.
    """
    found = compare_files(LIBRARY_DIR, SHIPPED_FILES, directory)
    return check_result(directory, found, absent=len(found.missing) == len(SHIPPED_FILES))
