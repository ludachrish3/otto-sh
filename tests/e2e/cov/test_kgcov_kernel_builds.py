"""otto_kgcov built for every kernel of the set: the library and the demo, build-only, per kernel.

    OTTO_KGCOV_KERNELS=x86_64-cross,2.6.32,3.13,4.4,5.4,5.15,6.17 \\
    OTTO_KGCOV_KERNELS_DIR=/home/vagrant/build/kgcov-kernels \\
        uv run pytest tests/e2e/cov/test_kgcov_kernel_builds.py -n0 --no-cov

(`make kgcov` sets both from KGCOV_KERNELS and KGCOV_KERNELS_DIR and runs this
together with the bed matrix.) One column per kernel id, the table in
tests/e2e/cov/_repo5_build.py: ``x86_64-cross`` is the 6.8 source tree at
OTTO_KGCOV_CROSS_KDIR built on the host with the VM's cross gcc; every other
column is a tree scripts/provision_kgcov_kernels.sh prepared, built under
``docker run`` in a container of that kernel's own Ubuntu release with that
release's gcc (a kernel tree is coupled to the compilers of its era), the
repository, the tree and the temporary directory mounted at their own host
paths so every path a diagnostic or a .gcno names is the same inside and
out. Nothing is ever loaded: the dev VM and the beds run 6.8.

The library builds into a temporary directory through its own build.sh, the
demo from a COPY of its sources under the same directory (the in-place
fixture build stays untouched), with the same environment a module of the
user's own would take: KDIR, ARCH, CROSS_COMPILE, CC, KMAKEFLAGS. A missing
tree, image, docker or cross compiler FAILS naming what provisions it —
nothing skips, so a release can trust a green. Unset or empty,
OTTO_KGCOV_KERNELS means ``x86_64-cross`` alone, the one column that needs
neither docker nor provisioning. Carries `kgcov`: excluded from every
default lane, selected by `make kgcov`.

The two source trees (``x86_64-cross``, ``2.6.32``) come from
``defconfig modules_prepare`` and have no Module.symvers, so modpost cannot
resolve the core kernel's own exports there and those columns build with
KBUILD_MODPOST_WARN=1; the demo's ``depends`` entry and the library's
Module.symvers still prove the two .ko's linked against each other. The
headers columns ship Module.symvers and build with modpost strict.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._ambient_env import ambient
from tests.e2e.cov._repo5_build import (
    CTORS_SECTION,
    DEFAULT_KERNELS_DIR,
    DEMO_SRC,
    ISA_MACHINE,
    ISA_OBJCOPY,
    KGCOV,
    TOOLCHAINS_KEY,
    KernelColumn,
    Toolchain,
    assert_bracketed,
    column_compiler_version,
    column_env,
    column_prerequisite_error,
    elf_sections,
    init_array_symbols,
    kernel_column,
    note_toolchain,
    rename_constructor_section,
    run_in_column,
    snapshot_tree,
    tree_mount,
    tree_prepared,
    warnings_under,
)

pytestmark = [pytest.mark.hostless, pytest.mark.kgcov]

KERNELS = [k.strip() for k in ambient("OTTO_KGCOV_KERNELS", "").split(",") if k.strip()] or [
    "x86_64-cross"
]
KERNELS_DIR = Path(ambient("OTTO_KGCOV_KERNELS_DIR", str(DEFAULT_KERNELS_DIR)))
CROSS_KDIR = Path(ambient("OTTO_KGCOV_CROSS_KDIR", "/home/vagrant/build/linux-6.8"))
INSTRUMENTED_UNITS = ("demo_main", "demo_parse", "demo_policy")

# A library/consumer symbol on an ``undefined!`` modpost line would mean the
# two .ko's did not actually link against each other, which
# KBUILD_MODPOST_WARN=1 would otherwise let slide silently. Modpost prints at
# most ten such lines per module and suppresses the rest, so this scan is a
# second opinion, never the link proof — that is the ``depends`` check.
_LIBRARY_SYMBOL_PREFIXES = ("kgcov_", "__gcov_", "llvm_")
_DEMO_IGNORE = shutil.ignore_patterns(
    "*.o", "*.ko", "*.mod*", "*.cmd", "*.gcno", "*.gcda", "Module.symvers", "modules.order", ".*"
)


@dataclass
class KernelBuild:
    """Both .ko's built for one column in a temp dir, with each build's combined output."""

    column: KernelColumn
    root: Path
    lib_ko: Path
    demo_ko: Path
    release: str
    lib_output: str
    demo_output: str
    fixture_state: list[tuple[str, int, int]]
    """:func:`snapshot_tree` of DEMO_SRC, taken before this column touched anything."""

    @property
    def lib_dir(self) -> Path:
        return self.lib_ko.parent

    @property
    def output(self) -> str:
        return self.lib_output + self.demo_output


def _run(column: KernelColumn, argv: list[str], *, cwd: Path, mounts: list[Path]) -> str:
    """Run *argv* in the column's environment; the combined output, or a named AssertionError."""
    try:
        done = run_in_column(column, argv, cwd=cwd, env=column_env(column), mounts=mounts)
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"[{column.id}] {' '.join(str(a) for a in argv)} exited {exc.returncode}\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc
    return done.stdout + done.stderr


def _host(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def _image_present(column: KernelColumn) -> bool:
    """Whether *column*'s image is there, or an AssertionError naming docker's own stderr.

    ``docker image inspect`` failing with "No such image" is the ordinary,
    expected shape of "not provisioned yet" (``False``, so the caller's own
    remedy — run the provisioning script — is the right one to print); any
    OTHER failure (the daemon down, a permission error, ...) is not that, and
    printing the provisioning remedy over it would send someone chasing the
    wrong fix, so it fails naming docker's stderr instead. Nothing skips.
    """
    if column.image is None:
        return True
    if shutil.which("docker") is None:
        return False
    inspect = subprocess.run(
        ["docker", "image", "inspect", column.image], check=False, capture_output=True, text=True
    )
    if inspect.returncode == 0:
        return True
    if "No such image" in inspect.stderr:
        return False
    raise AssertionError(
        f"[{column.id}] docker image inspect {column.image} failed: {inspect.stderr}"
    )


def _demo_make(column: KernelColumn, demo: Path, lib_dir: Path) -> str:
    argv = ["make", "-C", str(demo), f"KDIR={column.tree}", f"KGCOV={lib_dir}"]
    return _run(column, argv, cwd=demo, mounts=[demo.parent, tree_mount(column)])


def _assert_fixture_untouched(kernel_build: KernelBuild) -> None:
    """DEMO_SRC must read back exactly as ``kernel_build`` found it before touching anything.

    Every build here works on a COPY. Called last by any row that does its OWN extra
    building (beyond the fixture's), so a row whose copy-and-build slips and reaches into
    DEMO_SRC is caught by that same row rather than silently passed over by an earlier row
    that already ran its own check.
    """
    assert snapshot_tree(DEMO_SRC) == kernel_build.fixture_state


@pytest.fixture(scope="module", params=KERNELS, ids=KERNELS)
def kernel_build(request, tmp_path_factory) -> KernelBuild:
    """The library and the demo built for this parameter's kernel."""
    fixture_state = snapshot_tree(DEMO_SRC)
    column = kernel_column(request.param, kernels_dir=KERNELS_DIR, cross_kdir=CROSS_KDIR)
    compiler = f"{column.env.get('CROSS_COMPILE', '')}gcc"
    problem = column_prerequisite_error(
        column,
        tree_prepared=tree_prepared(column),
        image_present=_image_present(column),
        docker_present=shutil.which("docker") is not None,
        compiler_present=column.image is not None or shutil.which(compiler) is not None,
        objcopy_present=shutil.which(ISA_OBJCOPY["x86_64"]) is not None,
    )
    assert problem is None, problem
    root = tmp_path_factory.mktemp(f"kgcov_{column.id.replace('.', '_')}")
    release = (column.tree / "include" / "config" / "kernel.release").read_text().strip()
    note_toolchain(
        request.config,
        Toolchain(
            column.id, {}, compiler_version=column_compiler_version(column), kernel_release=release
        ),
    )
    lib_output = _run(
        column,
        [str(KGCOV / "build.sh"), str(root / "build"), release],
        cwd=root,
        mounts=[root, tree_mount(column)],
    )
    demo = root / "demo"
    shutil.copytree(DEMO_SRC, demo, ignore=_DEMO_IGNORE)
    demo_output = _demo_make(column, demo, root / "build" / "lib")
    return KernelBuild(
        column=column,
        root=root,
        lib_ko=root / "build" / "lib" / "otto_kgcov.ko",
        demo_ko=demo / "otto_kmod_demo.ko",
        release=release,
        lib_output=lib_output,
        demo_output=demo_output,
        fixture_state=fixture_state,
    )


def test_both_modules_carry_the_trees_release(kernel_build):
    for ko in (kernel_build.lib_ko, kernel_build.demo_ko):
        vermagic = _host(["modinfo", "-F", "vermagic", str(ko)])
        assert vermagic.startswith(kernel_build.release + " "), (ko, vermagic, kernel_build.release)


def test_both_modules_are_objects_for_the_columns_isa(kernel_build):
    machine = ISA_MACHINE[kernel_build.column.isa]
    for ko in (kernel_build.lib_ko, kernel_build.demo_ko):
        header = _host(["readelf", "-h", str(ko)])
        assert machine in header, (ko, header)


def test_the_columns_compiler_built_them(kernel_build, request):
    version = request.config.stash[TOOLCHAINS_KEY][kernel_build.column.id].compiler_version
    for ko in (kernel_build.lib_ko, kernel_build.demo_ko):
        comment = _host(["readelf", "-p", ".comment", str(ko)])
        assert "GCC" in comment, (ko, comment)
        assert version in comment, (ko, version, comment)


def test_the_demo_is_instrumented_and_bracketed(kernel_build):
    # A modversions-era kbuild (3.13, 4.4: CONFIG_MODVERSIONS=y, and the
    # branch of cmd_cc_o_c that goes with it) compiles every object as
    # .tmp_$(@F) first and, once it has checked the object for __ksymtab,
    # either relinks it through ld -r or plain-renames .tmp_$(@F) to $@ --
    # renaming only the .o it was asked for, never the .gcno gcc wrote
    # beside it (measured 2026-09-29: scripts/Makefile.build's cmd_cc_o_c).
    # The demo is instrumented and bracketed correctly on every column; only
    # this file's name is column-dependent, so both are accepted.
    for unit in INSTRUMENTED_UNITS:
        directory = kernel_build.demo_ko.parent
        assert (directory / f"{unit}.gcno").is_file() or (
            directory / f".tmp_{unit}.gcno"
        ).is_file(), unit
    assert_bracketed(init_array_symbols(kernel_build.demo_ko), len(INSTRUMENTED_UNITS))


def test_the_in_place_fixture_build_was_not_touched(kernel_build):
    # Every column here worked on a COPY of DEMO_SRC; the fixture's own
    # in-place tree must come back exactly as the fixture found it before
    # this column built anything. "absent or AArch64" alone cannot fail on
    # an arm64 column, which writes nothing new there in the first place —
    # _assert_fixture_untouched's snapshot equality is the row's real proof;
    # the ISA check on any in-place .ko (the bed e2es' own build, never this
    # column's) stays too.
    _assert_fixture_untouched(kernel_build)
    ko = DEMO_SRC / "otto_kmod_demo.ko"
    assert not ko.is_file() or "AArch64" in _host(["readelf", "-h", str(ko)]), ko


def test_the_demo_linked_against_the_library(kernel_build):
    """On a KBUILD_MODPOST_WARN=1 column the tree's missing Module.symvers hides nothing real.

    modpost writes a module's ``depends`` entry from the symbols it resolved
    against another module's ``Module.symvers`` and nothing else, so the demo
    depending on ``otto_kgcov`` — and only on it — is the whole claim that
    the two .ko's linked against each other. The ``undefined!`` scan is a
    second opinion on the two source-tree columns: every such line modpost
    printed must name a core kernel export, never a library symbol. A
    headers column builds with modpost strict, where an undefined symbol
    would have failed the build already.
    """
    assert _host(["modinfo", "-F", "depends", str(kernel_build.demo_ko)]).strip() == "otto_kgcov"
    symvers = (kernel_build.lib_dir / "Module.symvers").read_text()
    for symbol in ("kgcov_register", "kgcov_unregister"):
        assert symbol in symvers, (symbol, symvers)
    undefined = [line for line in kernel_build.output.splitlines() if "undefined!" in line]
    if not kernel_build.column.modpost_may_warn:
        assert undefined == [], undefined
    bad = []
    for line in undefined:
        m = re.search(r'"(\S+)"', line)
        assert m, line
        if m.group(1).startswith(_LIBRARY_SYMBOL_PREFIXES):
            bad.append(line)
    assert not bad, bad


def test_the_library_compiled_without_a_warning_of_its_own(kernel_build):
    assert warnings_under(kernel_build.lib_output, kernel_build.lib_dir) == []


def test_the_clang_backends_unit_compiles_with_the_columns_gcc(kernel_build):
    """The vendored clang backend (kgcov_clang.c) compiles against this kernel's headers with
    this column's gcc, warning-free, built as a module object of its own so the proof holds on
    every kernel in the set."""
    column = kernel_build.column
    clang_unit = kernel_build.root / "clang_unit"
    shutil.copytree(kernel_build.lib_dir, clang_unit, ignore=_DEMO_IGNORE)
    kbuild = clang_unit / "Kbuild"
    flags = [
        line
        for line in kbuild.read_text().splitlines()
        if line.strip().startswith(("ccflags-y", "EXTRA_CFLAGS"))
    ]
    # kgcov_clang.c alone calls kgcov_ctor_info(), a plain (non-exported)
    # otto_kgcov symbol the real library resolves at ITS OWN link because
    # kgcov.o rides in the same module — never crossing a module boundary,
    # so it carries no EXPORT_SYMBOL. Building kgcov.o alongside it here is
    # what makes this the clang variant of the real library rather than a
    # fragment modpost cannot link: MODULE_LICENSE/MODULE_DESCRIPTION and
    # every other symbol kgcov_clang.o needs come from kgcov.c itself, the
    # same as they would in a real clang-toolchain build of otto_kgcov.
    kbuild.write_text(
        "obj-m := otto_kgcov_clang_unit.o\n"
        "otto_kgcov_clang_unit-y := kgcov.o kgcov_clang.o\n"
        + "".join(f"{line}\n" for line in flags)
    )
    # Straight to kbuild, so CC and HOSTCC go on the command line: the kernel's own
    # Makefile assigns CC itself and only a command-line value overrides that. Read
    # off column_env's own composition (CC, and KMAKEFLAGS which already carries
    # HOSTCC and KBUILD_MODPOST_WARN=1 where the column needs them) rather than
    # re-deriving the same "which flags this column needs" logic here — there is
    # no wrapper Makefile to forward KMAKEFLAGS from the environment, so its
    # tokens go straight on the command line instead.
    env = column_env(column)
    argv = ["make", "-C", str(column.tree), f"M={clang_unit}"]
    if "CC" in env:
        argv.append(f"CC={env['CC']}")
    argv += [*env.get("KMAKEFLAGS", "").split(), "modules"]
    output = _run(column, argv, cwd=clang_unit, mounts=[kernel_build.root, tree_mount(column)])
    assert (clang_unit / "kgcov_clang.o").is_file(), output
    assert warnings_under(output, clang_unit) == []
    _assert_fixture_untouched(kernel_build)


def test_a_ctors_convention_demo_is_bracketed_too(kernel_build):
    """No compiler in the set emits .ctors, so the objects are made to: the standing proof of
    kgcov.lds's .ctors clause on every kernel, the pre-4.0 and pre-5.15 module linker scripts
    in particular.

    The demo is built once, giving kbuild's own compiled objects; each
    instrumented object's constructor section is then renamed to the
    PROGBITS ``.ctors.65435`` an Ubuntu x86_64 cross gcc 9 emits; then the
    module is built again from them. "Relinked, not recompiled" is two
    checks together: the .ko's mtime moving forward across the second build
    is the RELINK half (kbuild would have no reason to touch it otherwise —
    the renamed objects are newer than it, but the command line to build
    them is unchanged, so kbuild only recompiles a source that outdates its
    object); the renamed sections still being there afterwards is the
    NOT-RECOMPILED half. The .ko must hold one .init_array and no .ctors
    section at all.
    """
    column = kernel_build.column
    ctors = kernel_build.root / "ctors_demo"
    shutil.copytree(DEMO_SRC, ctors, ignore=_DEMO_IGNORE)
    lib_dir = kernel_build.lib_dir
    _demo_make(column, ctors, lib_dir)
    for unit in INSTRUMENTED_UNITS:
        rename_constructor_section(ctors / f"{unit}.o", column.isa)
    ko = ctors / "otto_kmod_demo.ko"
    mtime_before = ko.stat().st_mtime_ns
    _demo_make(column, ctors, lib_dir)
    assert ko.stat().st_mtime_ns > mtime_before, "kbuild did not relink the module"
    for unit in INSTRUMENTED_UNITS:
        names = {s.name for s in elf_sections(ctors / f"{unit}.o")}
        assert CTORS_SECTION in names, (
            unit,
            "kbuild recompiled the object instead of relinking it",
        )
    sections = [s.name for s in elf_sections(ko)]
    assert sections.count(".init_array") == 1, sections
    assert not [n for n in sections if n.startswith(".ctors")], sections
    assert_bracketed(init_array_symbols(ko), len(INSTRUMENTED_UNITS))
    _assert_fixture_untouched(kernel_build)
