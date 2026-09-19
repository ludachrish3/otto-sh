"""otto_kgcov cross build: the library and the demo for x86_64, from a kernel source tree.

Build-only, on the dev VM, never loaded anywhere: the dev VM and the beds
are arm64, so an x86_64 module from ``x86_64-linux-gnu-gcc`` is a
foreign-ISA cross build in full, and ``readelf -h`` is what proves it. The
tree comes from OTTO_KGCOV_CROSS_KDIR (`make kgcov` sets it from
KGCOV_CROSS_KDIR; default /home/vagrant/build/linux-6.8), prepared once with

    make ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- defconfig modules_prepare

The library builds into a temporary directory through its own build.sh,
the demo from a COPY of its sources under the same temporary directory (the
in-place fixture build stays untouched), both with the same environment a
module of the user's own would take: KDIR, ARCH, CROSS_COMPILE. A missing
tree or cross compiler FAILS naming it and the command that prepares it —
nothing skips, so a release can trust a green. Carries `kgcov`: excluded
from every default lane, selected by `make kgcov`.

`defconfig modules_prepare` alone never produces a ``Module.symvers`` — the
kernel's own docs say a full build is needed for that — so modpost cannot
resolve the core kernel's own exports (``memcpy``, ``kfree``, ``_printk``,
…) here and the build runs with ``KBUILD_MODPOST_WARN=1`` to turn those into
warnings rather than let them fail it. What still proves the two .ko's
linked against each other is the demo's ``depends`` entry and the library's
``Module.symvers``; a module meant to actually be loaded is built against a
fully built tree or the target's headers, never this way.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._ambient_env import ambient
from tests.e2e.cov._repo5_build import DEMO_SRC, KGCOV, init_array_symbols

pytestmark = [pytest.mark.hostless, pytest.mark.kgcov]

ARCH = "x86_64"
CROSS = "x86_64-linux-gnu-"
KDIR = Path(ambient("OTTO_KGCOV_CROSS_KDIR", "/home/vagrant/build/linux-6.8"))
PREPARE = f"make -C {KDIR} ARCH={ARCH} CROSS_COMPILE={CROSS} defconfig modules_prepare"
_ENV = {
    **os.environ,
    "KDIR": str(KDIR),
    "ARCH": ARCH,
    "CROSS_COMPILE": CROSS,
    "KMAKEFLAGS": "KBUILD_MODPOST_WARN=1",
}

# A library/consumer symbol on an ``undefined!`` modpost line would mean the
# two .ko's did not actually link against each other, which
# KBUILD_MODPOST_WARN=1 would otherwise let slide silently. Modpost prints at
# most ten such lines per module and suppresses the rest, so this scan is a
# second opinion, never the link proof — that is the ``depends`` check.
_LIBRARY_SYMBOL_PREFIXES = ("kgcov_", "__gcov_", "llvm_")


def _run_full(argv, cwd=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, env=_ENV, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"{' '.join(str(a) for a in argv)} exited {exc.returncode}\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc


def _run(argv, cwd=None) -> str:
    return _run_full(argv, cwd).stdout


@pytest.fixture(scope="module")
def prepared_tree() -> Path:
    assert shutil.which(f"{CROSS}gcc"), (
        f"{CROSS}gcc is not on PATH: install gcc-x86-64-linux-gnu, the cross compiler this "
        "proof builds with"
    )
    assert (KDIR / "Makefile").is_file(), (
        f"no kernel source tree at {KDIR}: download linux-6.8.tar.xz from kernel.org, "
        f"extract it there (or point OTTO_KGCOV_CROSS_KDIR at a tree), then run `{PREPARE}`"
    )
    assert (KDIR / "include" / "config" / "kernel.release").is_file(), (
        f"{KDIR} is not prepared for external modules: run `{PREPARE}`"
    )
    return KDIR


@pytest.fixture(scope="module")
def cross_build(prepared_tree, tmp_path_factory):
    """Both .ko's built for x86_64 in a temp dir.

    Returns ``(lib_ko, demo_ko, release, build_output)`` — ``build_output`` is
    the combined stdout+stderr of both builds, kept so a test can inspect the
    modpost warnings ``KBUILD_MODPOST_WARN=1`` turned the undefined-symbol
    errors into.
    """
    tmp = tmp_path_factory.mktemp("kgcov_cross")
    release = (prepared_tree / "include" / "config" / "kernel.release").read_text().strip()
    lib_build = _run_full([str(KGCOV / "build.sh"), str(tmp / "build"), release])
    demo = tmp / "demo"
    ignore = shutil.ignore_patterns(
        "*.o",
        "*.ko",
        "*.mod*",
        "*.cmd",
        "*.gcno",
        "*.gcda",
        "Module.symvers",
        "modules.order",
        ".*",
    )
    shutil.copytree(DEMO_SRC, demo, ignore=ignore)
    demo_build = _run_full(
        ["make", "-C", str(demo), f"KDIR={prepared_tree}", f"KGCOV={tmp / 'build' / 'lib'}"]
    )
    output = lib_build.stdout + lib_build.stderr + demo_build.stdout + demo_build.stderr
    return tmp / "build" / "lib" / "otto_kgcov.ko", demo / "otto_kmod_demo.ko", release, output


def test_both_modules_carry_the_trees_release(cross_build):
    lib, demo, release, _ = cross_build
    for ko in (lib, demo):
        vermagic = _run(["modinfo", "-F", "vermagic", str(ko)])
        assert vermagic.startswith(release + " "), (ko, vermagic, release)


def test_both_modules_are_x86_64_objects(cross_build):
    lib, demo, _, _ = cross_build
    for ko in (lib, demo):
        header = _run(["readelf", "-h", str(ko)])
        assert "Advanced Micro Devices X86-64" in header, (ko, header)


def test_the_cross_compiler_built_them(cross_build):
    lib, demo, _, _ = cross_build
    for ko in (lib, demo):
        comment = _run(["readelf", "-p", ".comment", str(ko)])
        assert "GCC" in comment, (ko, comment)


def test_the_demo_is_instrumented_and_bracketed(cross_build):
    _, demo, _, _ = cross_build
    for unit in ("demo_main", "demo_parse", "demo_policy"):
        assert (demo.parent / f"{unit}.gcno").is_file(), unit
    syms = init_array_symbols(demo)
    assert syms == ["__kgcov_begin_marker", *(["_sub_I_00100_0"] * 3), "__kgcov_end_marker"], syms


def test_the_in_place_fixture_build_was_not_touched(cross_build):
    # The fixture's own demo build (if any) belongs to the bed e2es; the
    # cross build worked on a copy, so nothing under tests/repo5 is x86_64.
    ko = DEMO_SRC / "otto_kmod_demo.ko"
    assert not ko.is_file() or "AArch64" in _run(["readelf", "-h", str(ko)]), ko


def test_the_demo_linked_against_the_library(cross_build):
    """KBUILD_MODPOST_WARN=1 hides the tree's missing Module.symvers, not a real link gap.

    modpost writes a module's ``depends`` entry from the symbols it resolved
    against another module's ``Module.symvers``, and nothing else, so the
    demo depending on ``otto_kgcov`` — and only on it — is the whole claim
    that the two .ko's linked against each other through the
    ``KBUILD_EXTRA_SYMBOLS`` the library's ``Module.symvers`` supplied. It is
    also uncapped, which the modpost text below is not.

    The scan is kept as a second opinion: every ``undefined!`` line modpost
    did print is expected to name a core kernel export (``memcpy``,
    ``kfree``, ``_printk``, …) that only a fully built tree's
    ``Module.symvers`` would resolve. Modpost stops at ten such lines per
    module and reports the rest only as a count, so a clean scan proves
    nothing on its own.
    """
    lib, demo, _, output = cross_build
    assert _run(["modinfo", "-F", "depends", str(demo)]).strip() == "otto_kgcov"

    symvers = (lib.parent / "Module.symvers").read_text()
    for symbol in ("kgcov_register", "kgcov_unregister"):
        assert symbol in symvers, (symbol, symvers)

    bad = []
    for line in output.splitlines():
        if "undefined!" not in line:
            continue
        m = re.search(r'"(\S+)"', line)
        assert m, line
        if m.group(1).startswith(_LIBRARY_SYMBOL_PREFIXES):
            bad.append(line)
    assert not bad, bad
