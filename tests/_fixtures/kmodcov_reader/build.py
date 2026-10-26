"""Build otto_kmodcov's own C sources into a user-space program, per compiler.

The library files (``src/otto/kmodcov``) compile unchanged against
``kshim.h``: every ``<linux/...>`` header they include is generated into the
build directory as a one-line include of the shim, and this directory's
``kmodcov_local.h`` is force-included the way Kbuild force-includes a real
one. The instrumented "module" is ``unit.c``, bracketed by the two sentinels
and folded through ``kmodcov.lds`` at an ``ld -r`` link exactly as a
consumer's intermediate link is, so ``kmodcov_register()`` walks the same
constructor array a kernel module's would.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

HERE = Path(__file__).resolve().parent
LIBRARY = CHECKOUT_ROOT / "src" / "otto" / "kmodcov"
_LINUX_INCLUDE = re.compile(r"^#include <linux/([a-z_]+\.h)>", re.MULTILINE)


@dataclass(frozen=True)
class Toolchain:
    """One compiler the reader is proven with, and the gcov that reads what it writes."""

    name: str
    cc: str
    gcov: list[str]
    family: str  # "gcc" or "clang"
    major: int
    coverage_version: str | None = None
    """The gcov stamp the instrumented unit is forced to write (clang's
    ``-coverage-version``); ``None`` for the compiler's own."""

    @property
    def header_bytes(self) -> int:
        """The ``.gcda`` header's length: gcc 12 and later add a checksum word."""
        return 16 if self.family == "gcc" and self.major >= 12 else 12

    @property
    def unit_bytes(self) -> int:
        """What one unit of a record's length counts: bytes from gcc 12, words otherwise."""
        return 4 if self.family == "gcc" and self.major >= 12 else 1


def _major(cc: str) -> int:
    out = subprocess.run([cc, "-dumpversion"], check=True, capture_output=True, text=True)
    return int(out.stdout.strip().split(".")[0])


def gcc_toolchain(cc: str) -> Toolchain:
    major = _major(cc)
    gcov = f"gcov-{major}" if shutil.which(f"gcov-{major}") else "gcov"
    return Toolchain(name=cc, cc=cc, gcov=[gcov], family="gcc", major=major)


def clang_toolchain(cc: str = "clang", coverage_version: str | None = None) -> Toolchain:
    """clang, read by its llvm-cov; *coverage_version* forces the stamp it writes."""
    major = _major(cc)
    llvm_cov = next(
        (c for c in (f"llvm-cov-{major}", "llvm-cov") if shutil.which(c)), f"llvm-cov-{major}"
    )
    name = cc if coverage_version is None else f"{cc} ({coverage_version})"
    return Toolchain(
        name=name,
        cc=cc,
        gcov=[llvm_cov, "gcov"],
        family="clang",
        major=major,
        coverage_version=coverage_version,
    )


def _run(argv: list[str], cwd: Path) -> None:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} failed:\n{result.stdout}\n{result.stderr}")


def build(tc: Toolchain, out: Path) -> Path:
    """Build the harness for *tc* under *out*; returns the program's path."""
    linux = out / "include" / "linux"
    linux.mkdir(parents=True)
    names = set()
    for src in [*LIBRARY.glob("*.c"), *LIBRARY.glob("*.h")]:
        names.update(_LINUX_INCLUDE.findall(src.read_text()))
    for name in names:
        # glibc's own <errno.h> includes <linux/errno.h>: that one must still
        # reach the system header, or no E* constant is defined.
        system = "#include_next <linux/errno.h>\n" if name == "errno.h" else ""
        (linux / name).write_text(f'{system}#include "{HERE / "kshim.h"}"\n')
    obj = out / "obj"
    obj.mkdir()
    (obj / "begin.c").write_text('#include "kmodcov.h"\nKMODCOV_SENTINEL_BEGIN;\n')
    (obj / "end.c").write_text('#include "kmodcov.h"\nKMODCOV_SENTINEL_END;\n')
    cflags = [
        "-O0",
        "-g",
        "-Wall",
        "-Werror",
        "-Wno-unused-function",
        f"-I{out / 'include'}",
        f"-I{LIBRARY}",
        "-include",
        str(HERE / "kmodcov_local.h"),
        '-DKBUILD_MODNAME="otto_kmodcov"',
    ]
    backend = (
        ["kmodcov_clang.c"] if tc.family == "clang" else ["kmodcov_gcc.c", "kmodcov_gcc_abi.c"]
    )
    for name in ["kmodcov.c", *backend]:
        _run([tc.cc, *cflags, "-c", str(LIBRARY / name), "-o", name[:-2] + ".o"], obj)
    _run([tc.cc, *cflags, "-c", str(HERE / "harness.c"), "-o", "harness.o"], obj)
    _run([tc.cc, *cflags, "-c", "begin.c", "end.c"], obj)
    # The "module": instrumented, linked without the compiler's own gcov
    # runtime — the library provides every symbol its constructor calls.
    forced: list[str] = []
    if tc.coverage_version is not None:
        forced = ["-Xclang", f"-coverage-version={tc.coverage_version}"]
    _run(
        [
            tc.cc,
            "-O0",
            "-fprofile-arcs",
            "-ftest-coverage",
            *forced,
            "-c",
            str(HERE / "unit.c"),
            "-o",
            "unit.o",
        ],
        obj,
    )
    _run(
        [
            "ld",
            "-r",
            "-T",
            str(LIBRARY / "kmodcov.lds"),
            "begin.o",
            "unit.o",
            "end.o",
            "-o",
            "consumer.o",
        ],
        obj,
    )
    objects = ["harness.o", "kmodcov.o", *(n[:-2] + ".o" for n in backend), "consumer.o"]
    _run([tc.cc, "-o", "harness", *objects], obj)
    return obj / "harness"
