"""Shared support for repo5's two bed coverage e2es.

Two things both e2es need, factored here so neither carries its own,
drifting copy:

- Build-and-freshness, PER FAMILY. ``otto test``/``OttoSuite`` never touch
  a product verb (``stage``/``install``/``uninstall``/``get_product_logs``
  are reachable only from the project-CLI actions, ``otto install`` and
  friends) — each suite installs only its OWN products, on the hosts it
  itself selects: ``TestKmodDemo`` on test1/test2, ``TestCovContainer`` on
  test3. So the kmod e2e needs only the kernel-module half built
  (``ensure_kmod_artifacts``, which runs ``tests/repo5/kmod/build.sh`` —
  the kernel half alone, not the whole fixture's ``build.sh``), and the
  docker e2e needs only the container-image half
  (``ensure_image_artifacts``) — neither e2e's fallback build depends on
  the other family's toolchain (the docker e2e needs no kernel headers).
  What DOES cross families: ``--cov-clean`` and the post-run fetch run
  every INSTRUMENTED, MATCHED product's
  ``reset_coverage``/``prepare_coverage`` on every ``[coverage] hosts``
  host, regardless of which suite ran — harmless (a module that was never
  loaded writes nothing; a ``find`` under an absent ``cov_dir`` is just a
  warning) and needs no artifact on disk, which is why neither ensure
  function needs to know about the other's outputs. One staleness rule
  (:func:`_artifacts_are_stale`) serves both, so it stays a single copy;
  the kernel-module half adds a toolchain stamp (``STAMP``) on top, so
  artifacts built by one compiler are stale for a rebuild with another.
- Coverage-store lookups: :func:`_line_of`/:func:`_record`/:func:`_hits`
  read a captured line's hit count off a source file and a
  :class:`~otto.coverage.store.model.CoverageStore`, failing with a message
  that names the file/line rather than a bare ``KeyError``/``StopIteration``.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from otto.coverage.store.model import CoverageStore, FileRecord
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import REPO5

BUILD = REPO5 / "build"
DEMO_SRC = REPO5 / "kmod" / "demo"
KGCOV = PROJECT_ROOT / "docs" / "examples" / "kgcov"
DOCKER = REPO5 / "docker"
DOCKER_SRC = DOCKER / "src"
TARBALL = DOCKER / "otto-cov-demo.tar"
KMOD_BUILD = REPO5 / "kmod" / "build.sh"
LAB_DATA = PROJECT_ROOT / "tests" / "_fixtures" / "lab_data" / "tech1"
SYSTEM_LCOV = "/usr/bin/lcov"
STAMP = BUILD / "toolchain"
"""Which compiler built the kernel-module half last: its ``Toolchain.name``.

The library's ``.ko`` lands in one place (``build/lib``, where the fixture's
``settings.toml`` points) and the demo builds in place, so "built by which
compiler" cannot be read off a path — it is read off this file. Written by
``tests/repo5/kmod/build.sh`` itself on a successful build, from
``OTTO_KGCOV_TOOLCHAIN`` (``manual`` when unset, e.g. a hand-run build) — the
single writer, so a hand run is never mistaken for one of this helper's own
toolchains. A stamp naming another compiler makes the artifacts stale.
"""

# kbuild applies a stock kernel's compiler-specific flags to every external
# module, and a stock kernel's config was written for the compiler that built
# it. Two proven override sets (2026-09-18, 6.8.0-86-generic's headers,
# configured for gcc 13.3):
#
# - clang against a gcc-built kernel: tell kbuild the compiler is clang, and
#   replace the two gcc-only flags that clang rejects.
# - a gcc older than the kernel's: tell kbuild the real version (it gates flags
#   on CONFIG_GCC_VERSION) and turn off the options whose flags that gcc does
#   not have — a fact about the compiler, hence the floors below.
CLANG_ON_GCC_KERNEL = (
    "CONFIG_CC_IS_GCC= CONFIG_GCC_VERSION=0 CONFIG_CC_IS_CLANG=y CONFIG_CLANG_VERSION={version} "
    "CONFIG_CC_IMPLICIT_FALLTHROUGH=-Wimplicit-fallthrough "
    "CONFIG_UBSAN_BOUNDS_STRICT= CONFIG_UBSAN_ARRAY_BOUNDS=y"
)
GCC_OPTION_FLOORS = {
    "CONFIG_SHADOW_CALL_STACK": 12,  # -fsanitize=shadow-call-stack (arm64)
    "CONFIG_INIT_STACK_ALL_ZERO": 12,  # -ftrivial-auto-var-init=zero
    "CONFIG_ZERO_CALL_USED_REGS": 11,  # -fzero-call-used-regs=used-gpr
}


@dataclass
class Toolchain:
    """One compiler for the kernel-module half, as ``kmod/build.sh``'s environment takes it."""

    name: str
    env: dict[str, str]
    gcov: "str | None" = None
    """Absolute path of the gcov that reads what this compiler writes, or ``None``.

    ``None`` means the system gcov, i.e. no bed configuration at all — the
    routine e2e's exact behaviour. Otherwise the bed hosts must be TOLD this
    path: for a Unix host otto reads the counters with the gcov the host
    RECORD names (collection records every fetched Unix host's toolchain, the
    default ``usr/bin/gcov`` included, and the reporter takes an explicit
    entry before any ``.gcno`` discovery), so nothing is ever inferred from
    the data. :func:`overlay_lab` is what puts it there.
    """
    lcov_args: list[str] = field(default_factory=list)
    """Extra arguments this compiler's output needs lcov to carry — usually none.

    A property of the COMPILER, not of one host or one call: whatever is here
    has to reach both the capture and the merge, which is why
    :func:`overlay_lab` delivers it as a wrapper named in ``toolchain.lcov``
    rather than as a flag on one command. See :data:`CLANG_LCOV_ARGS`.
    """


DEFAULT_TOOLCHAIN = Toolchain("default", {})

# Why clang, and only clang, needs lcov talked round.
#
# gcc records the compile's working directory in the .gcno — a gcc-built
# demo_main.gcno holds `/usr/src/linux-headers-6.8.0-86-generic` beside
# `./arch/arm64/include/asm/uaccess.h` — so gcov resolves the records for the
# kernel headers the module inlined from. Clang writes the gcov-4.2-format
# .gcno, which has no such record, and no clang flag adds one
# (-fcoverage-compilation-dir and -fcoverage-prefix-map leave the recorded
# path `./…`). llvm-cov therefore emits those header paths as kbuild gave
# them, relative to the kernel tree, and geninfo resolves a relative path
# against the DATA directory — the fetched .gcda directory, which holds no
# `arch/` tree — and refuses the whole capture over it.
#
# They are kernel headers, never the demo's own files, and otto keeps only
# files it can anchor to a committed blob in the repo, so nothing measured is
# lost by telling lcov to carry on: `--ignore-errors source` is lcov's own
# remedy for exactly this. A user would spell it `ignore_errors = source` in
# ~/.lcovrc; a test that must not touch the developer's home names a wrapper
# instead.
CLANG_LCOV_ARGS = ["--ignore-errors", "source"]

# The bed elements the overlay re-declares. The composite lab replaces an
# element WHOLESALE by slug (later source wins), so each is copied from the
# fixture's lab data key for key — dropping `labs` would take the host out of
# the `unix` lab, dropping `creds` would lock otto out of it.
OVERLAY_ELEMENTS = ("test1", "test2")


def running_kernel_kdir() -> Path:
    return Path("/lib/modules") / os.uname().release / "build"


def kernel_config(kdir: Path) -> str:
    """The text of the kernel tree's ``.config``, or ``""`` when it has none."""
    config = kdir / ".config"
    return config.read_text() if config.is_file() else ""


def compiler_version_number(cc: str) -> int:
    """kbuild's numeric version for *cc*: 18.1.3 -> 180103, 9.5.0 -> 90500."""
    flag = "-dumpversion" if "clang" in cc else "-dumpfullversion"
    out = subprocess.run([cc, flag], check=True, capture_output=True, text=True).stdout.strip()
    parts = [int(x) for x in out.split(".")[:3]] + [0, 0]
    return parts[0] * 10000 + parts[1] * 100 + parts[2]


def _require(tool: str, hint: str) -> None:
    assert shutil.which(tool), f"{tool} is not on PATH; {hint}"


def toolchain(name: str, kdir: Path | None = None) -> Toolchain:
    """The :class:`Toolchain` for a compiler name against the kernel tree at *kdir*.

    *name* is ``"default"`` (kbuild's own compiler, nothing set), a gcc such
    as ``"gcc-12"`` (``CC``, plus the older-gcc overrides when the kernel was
    configured for a newer gcc) or ``"clang"`` (``LLVM=1``, plus the gcc-kernel
    overrides when the kernel was built by gcc). A compiler that is not
    installed fails here, naming it — nothing downstream may skip.
    """
    if name == "default":
        return DEFAULT_TOOLCHAIN
    kdir = kdir or running_kernel_kdir()
    config = kernel_config(kdir)
    if name == "clang":
        _require("clang", "install clang and lld, or drop clang from OTTO_KGCOV_TOOLCHAINS")
        _require(
            "ld.lld", "install lld (LLVM=1 links with it), or drop clang from OTTO_KGCOV_TOOLCHAINS"
        )
        _require("llvm-cov", "install llvm (llvm-cov) or drop clang from OTTO_KGCOV_TOOLCHAINS")
        env = {"LLVM": "1"}
        if "CONFIG_CC_IS_GCC=y" in config:
            env["KMAKEFLAGS"] = CLANG_ON_GCC_KERNEL.format(version=compiler_version_number("clang"))
        # The plain name: otto's ``ensure_gcov_tool`` recognises any
        # ``llvm-cov(-N)?`` and wraps it as the two-word ``llvm-cov gcov``
        # that lcov cannot be handed directly.
        return Toolchain(name, env, gcov=shutil.which("llvm-cov"), lcov_args=list(CLANG_LCOV_ARGS))
    _require(name, f"install it (apt install {name}) or drop it from OTTO_KGCOV_TOOLCHAINS")
    gcov = name.replace("gcc", "gcov", 1)
    _require(
        gcov,
        f"install {name} (its package ships {gcov}) or drop it from OTTO_KGCOV_TOOLCHAINS",
    )
    env = {"CC": name}
    actual = compiler_version_number(name)
    kernel = re.search(r"^CONFIG_GCC_VERSION=(\d+)$", config, re.MULTILINE)
    if kernel and actual // 10000 < int(kernel.group(1)) // 10000:
        off = [
            f"{option}="
            for option, floor in GCC_OPTION_FLOORS.items()
            if actual // 10000 < floor and f"{option}=y" in config
        ]
        env["KMAKEFLAGS"] = " ".join([f"CONFIG_GCC_VERSION={actual}", *off])
    return Toolchain(name, env, gcov=shutil.which(gcov))


def _lcov_for(tc: Toolchain, root: Path) -> str:
    """The lcov the bed hosts should name: the system one, or a wrapper carrying *tc*'s args.

    otto runs the lcov the host record names for BOTH the capture and the
    merge, and a host record names a command, not a command line — so extra
    arguments have to travel as an executable. Hence the wrapper, written
    beside the overlay lab file rather than into the developer's ``~/.lcovrc``.
    """
    if not tc.lcov_args:
        return SYSTEM_LCOV
    wrapper = root / "lcov-wrapper.sh"
    wrapper.write_text(f'#!/bin/sh\nexec {SYSTEM_LCOV} {" ".join(tc.lcov_args)} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(wrapper)


def overlay_lab(tc: Toolchain, root: Path) -> "Path | None":
    """Write a lab file under *root* giving the bed hosts *tc*'s gcov, or ``None``.

    ``None`` when *tc* names no gcov (the system one), so the default build
    runs against the fixture's lab data exactly as the routine e2e does.

    Otherwise the file re-declares the two bed elements and nothing else. The
    composite lab replaces an element WHOLESALE by slug, later source winning,
    so each element is copied from the fixture's lab data key for key and only
    a ``toolchain`` is added to its host entries. The ``labs`` TABLE is
    deliberately absent: re-declaring it would replace the lab's resource
    lists, and this overlay has nothing to say about them.
    """
    if tc.gcov is None:
        return None
    fixture = json.loads((LAB_DATA / "lab.json").read_text())
    elements = [e for e in fixture["elements"] if e["name"] in OVERLAY_ELEMENTS]
    assert len(elements) == len(OVERLAY_ELEMENTS), (
        f"{LAB_DATA / 'lab.json'} holds {[e['name'] for e in elements]}, "
        f"expected {list(OVERLAY_ELEMENTS)} — the bed elements were renamed?"
    )
    root.mkdir(parents=True, exist_ok=True)
    lcov = _lcov_for(tc, root)
    for element in elements:
        assert element.get("hosts"), f"{element['name']} has no host entry to configure"
        for host in element["hosts"]:
            host["toolchain"] = {"sysroot": "/", "gcov": tc.gcov, "lcov": lcov}
    lab = root / "lab.json"
    lab.write_text(json.dumps({"elements": elements}, indent=4) + "\n")
    return lab


def overlay_repo(tc: Toolchain, root: Path) -> "Path | None":
    """A SUT repo under *root* contributing only :func:`overlay_lab`'s lab data.

    Layered over repo5 through ``OTTO_SUT_DIRS`` (``<repo5>:<overlay>``):
    ``build_lab_sources`` concatenates every repo's ``[[lab.sources]]`` in that
    order, so this repo's source is read last and its two elements replace
    repo5's. It declares no tests, no project block and no products — it
    exists to configure two hosts, and anything else it declared would shadow
    repo5's own.
    """
    if tc.gcov is None:
        return None
    lab = overlay_lab(tc, root)
    assert lab is not None
    return make_sut_repo(
        root / "kgcov_overlay",
        name="kgcov_overlay",
        version="1.0.0",
        extra=f'[[lab.sources]]\nbackend = "json"\npaths = ["{lab}"]\n',
    )


def kmod_artifacts_are_stale(artifacts: list[Path], sources: list[Path], tc: Toolchain) -> bool:
    """The file rule of :func:`_artifacts_are_stale`, plus: built by another compiler is stale."""
    if _artifacts_are_stale(artifacts, sources):
        return True
    return (STAMP.read_text().strip() if STAMP.is_file() else "") != tc.name


def _readelf(ko: Path, flag: str) -> str:
    return subprocess.run(
        ["readelf", flag, str(ko)], check=True, capture_output=True, text=True
    ).stdout


def _symbol_at(ko: Path, section: str, value: int) -> str:
    """The name of the local FUNC symbol at *value* inside *section* of *ko*.

    GNU as on aarch64 (and lld for local symbols) relocates against the
    enclosing SECTION symbol plus an offset rather than the local symbol's
    own name, so ``readelf -r`` prints ``.text.startup + 58``; the symbol
    table still holds ``_sub_I_00100_0`` at that offset in that section.
    Matched by type ``FUNC`` only: aarch64 mapping symbols (``$x``, ``$d``,
    always ``NOTYPE``, size 0) are emitted at the same address and a LOWER
    symbol index than the function they precede, and would otherwise shadow
    it — every marker and every gcov constructor here is ``FUNC``.
    """
    index = None
    for line in _readelf(ko, "-SW").splitlines():
        m = re.match(r"\s*\[\s*(\d+)\]\s+(\S+)", line)
        if m and m.group(2) == section:
            index = m.group(1)
            break
    assert index is not None, f"{ko}: no section named {section}"
    for line in _readelf(ko, "-sW").splitlines():
        cols = line.split()
        # Num: Value Size Type Bind Vis Ndx Name
        if len(cols) >= 8 and cols[6] == index and cols[3] == "FUNC" and int(cols[1], 16) == value:
            return cols[7]
    raise AssertionError(f"{ko}: no symbol at {section}+{value:#x}")


def init_array_symbols(ko: Path) -> list[str]:
    """The symbols the ``.init_array`` relocations of *ko* point at, in section-offset order.

    What the consumer's link put between the sentinels: the begin marker,
    one gcov constructor per instrumented unit, the end marker — the order
    ``kgcov_register()`` walks. A relocation against a section symbol
    (``.text.startup + 58``) is resolved through the symbol table.
    """
    out = _readelf(ko, "-rW")
    block = re.search(r"Relocation section '\.rela\.init_array'.*?(?:\n\n|\Z)", out, re.DOTALL)
    assert block, f"{ko} has no .rela.init_array section:\n{out[:2000]}"
    rows = []
    for line in block.group(0).splitlines()[2:]:
        cols = line.split()
        if len(cols) >= 5 and re.fullmatch(r"[0-9a-f]+", cols[0]):
            addend = int(cols[6], 16) if len(cols) >= 7 and cols[5] == "+" else 0
            rows.append((int(cols[0], 16), cols[4], addend))
    return [
        _symbol_at(ko, sym, addend) if sym.startswith(".") else sym
        for _, sym, addend in sorted(rows)
    ]


def _line_of(path: Path, needle: str) -> int:
    """1-based line number of the first source line containing *needle*."""
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found in {path}")


def _record(store: CoverageStore, name: str) -> FileRecord:
    """The store's :class:`FileRecord` whose path ends with *name*, or a named failure."""
    for fr in store.files():
        if str(fr.path).endswith(name):
            return fr
    have = [str(f.path) for f in store.files()]
    raise AssertionError(f"no FileRecord ending with {name!r}; have {have}")


def _hits(rec: FileRecord, lineno: int) -> int:
    """*lineno*'s system-tier hit count, or a named failure when it carries no ``DA:`` record."""
    assert lineno in rec.lines, f"{rec.path}:{lineno} carries no coverage data"
    return rec.lines[lineno].hits.for_tier("system")


def _demo_and_kgcov_sources() -> list[Path]:
    """Everything the kernel-module rebuild depends on: the demo's own files and otto_kgcov's."""
    return [
        # Kbuild writes the generated <module>.mod.c beside the sources AFTER the
        # library .ko; counting it would make every build look stale.
        *(p for p in DEMO_SRC.glob("*.c") if not p.name.endswith(".mod.c")),
        *DEMO_SRC.glob("*.h"),
        DEMO_SRC / "Kbuild",
        DEMO_SRC / "Makefile",
        KMOD_BUILD,
        *(p for p in KGCOV.rglob("*") if p.is_file()),
    ]


def _image_sources() -> list[Path]:
    """Everything the container-image rebuild depends on."""
    return [
        *DOCKER_SRC.glob("*.c"),
        *DOCKER_SRC.glob("*.h"),
        DOCKER / "Dockerfile",
        DOCKER / "build.sh",
    ]


def _artifacts_are_stale(artifacts: list[Path], sources: list[Path]) -> bool:
    """True when any of *artifacts* is missing, or older than any source it was built from.

    One rule for both artifact sets: a list of two ``.ko``\\ s (the kmod half)
    or a single-element list holding the tarball (the image half) — either
    way "missing" or "older than the newest source" means stale. *sources*
    must be non-empty — a caller whose glob silently returned nothing (a
    moved source directory) must not read that as "nothing to compare
    against, so never stale".
    """
    assert sources, "no sources to check staleness against — a source list moved or emptied?"
    if any(not a.is_file() for a in artifacts):
        return True
    newest_source = max(p.stat().st_mtime for p in sources if p.is_file())
    oldest_artifact = min(a.stat().st_mtime for a in artifacts)
    return oldest_artifact < newest_source


def _assert_committed(paths: list[Path], tmp_path_factory) -> None:
    """Fail loudly when any of *paths* carries an uncommitted OR untracked change.

    A capture anchors every measured file to a committed git blob at HEAD,
    while each e2e's own ``_line_of`` helper reads the worktree — an
    uncommitted file makes the two disagree on line numbers silently, and a
    brand-new, never-``git add``-ed file has no blob at HEAD at all, so it
    drops out of the capture entirely. ``git status --porcelain
    --untracked-files=all``, not ``git diff``: a diff against HEAD is silent
    about a file git has never seen — exactly the trap this guard exists to
    catch.
    """
    committed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *(str(p) for p in paths)],
        cwd=REPO5,
        env=git_env(tmp_path_factory.mktemp("githome")),
        check=False,
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0, f"git status failed: {committed.stderr}"
    assert not committed.stdout.strip(), (
        f"{', '.join(str(p) for p in paths)} have uncommitted or untracked changes: a capture "
        "anchors every measured file to a committed git blob at HEAD, while each e2e's "
        "_line_of() helper reads the worktree — such a file makes the two disagree on line "
        "numbers silently, or (if untracked) drops out of the capture entirely. Commit or "
        f"revert before running these e2es:\n{committed.stdout}"
    )


def ensure_kmod_artifacts(tmp_path_factory, tc: Toolchain = DEFAULT_TOOLCHAIN) -> None:
    """Build the kernel-module half (both .ko's) with *tc* when missing, stale, or built by
    another compiler.

    Runs ``tests/repo5/kmod/build.sh`` — only the kernel half; the
    container-image half has its own script and its own ensure function —
    with *tc*'s environment, plus ``OTTO_KGCOV_TOOLCHAIN=tc.name``, on top of
    the process's; the script itself is the toolchain stamp's single writer.
    """
    _assert_committed([DEMO_SRC], tmp_path_factory)
    lib_ko = BUILD / "lib" / "otto_kgcov.ko"
    demo_ko = DEMO_SRC / "otto_kmod_demo.ko"
    if kmod_artifacts_are_stale([lib_ko, demo_ko], _demo_and_kgcov_sources(), tc):
        try:
            subprocess.run(
                [str(KMOD_BUILD)],
                env={**os.environ, **tc.env, "OTTO_KGCOV_TOOLCHAIN": tc.name},
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{KMOD_BUILD} failed with {tc.name} (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc


def ensure_image_artifacts(tmp_path_factory) -> None:
    """Build the container-image half (the tarball) when missing or stale.

    Runs ``docker/build.sh`` directly, not ``tests/repo5/build.sh`` — the
    image half stands alone and needs no kernel headers, so the e2e that
    only exercises it must not inherit the kmod half's prerequisites.
    """
    _assert_committed([DOCKER_SRC], tmp_path_factory)
    if _artifacts_are_stale([TARBALL], _image_sources()):
        try:
            subprocess.run([str(DOCKER / "build.sh")], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{DOCKER / 'build.sh'} failed (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc
