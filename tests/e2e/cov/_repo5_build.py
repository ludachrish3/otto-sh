"""Shared support for repo5's coverage e2es.

Two things both bed e2es need, and one the kgcov build e2e needs:

- Build-and-freshness, PER FAMILY. ``otto test`` and its test classes never touch
  a product verb (``stage``/``install``/``uninstall``/``get_product_logs``
  are reachable only from the project-CLI actions, ``otto install`` and
  friends) — each test class installs only its OWN products, on the hosts it
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
- The kernel-column table behind ``make kgcov``'s build columns
  (:class:`KernelColumn`, :func:`kernel_columns`/:func:`kernel_column`), the
  container-or-host runner that builds and tests inside one
  (:func:`run_in_column`), and the ELF section helpers
  (:func:`elf_sections`, :func:`set_section_type`,
  :func:`constructor_section`, :func:`rename_constructor_section`) that find
  and rewrite a build's one constructor section — for the kgcov build e2e
  (``test_kgcov_kernel_builds.py``), not the two e2es named above.
"""

import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from otto.coverage.store.model import CoverageStore, FileRecord
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import REPO5

BUILD = REPO5 / "build"
DEMO_SRC = REPO5 / "kmod" / "demo"
KGCOV = REPO5 / "third_party" / "otto_kgcov"
"""The demo repo's vendored copy, held ``current`` against ``otto.kgcov`` by a guard — what a
user's repo holds."""
DOCKER = REPO5 / "docker"
DOCKER_SRC = DOCKER / "src"
TARBALL = DOCKER / "otto-cov-demo.tar"
KMOD_BUILD = REPO5 / "kmod" / "build.sh"
LAB_DATA = PROJECT_ROOT / "tests" / "_fixtures" / "lab_data" / "tech1"
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
    lcov_args: list[str] = field(default_factory=list)
    """Extra arguments this compiler's output needs lcov to carry — usually none.

    A property of the COMPILER, not of one call: whatever is here has to
    reach the capture of every host this compiler's output lands on, which is
    what otto's ``toolchain.lcov_args`` host field is for —
    :func:`overlay_lab` puts it on the bed hosts' records. See
    :data:`CLANG_LCOV_ARGS`.

    No gcov is named anywhere: otto reads a Unix host's counters with the
    gcov the host record names only when it names one, and otherwise with
    the one the counters' own stamp names — ``gcov-N`` for a gcc,
    ``llvm-cov`` for clang, from PATH — so the bed stays unconfigured and the
    matrix's green is that discovery's proof. :func:`toolchain` still
    requires the tool up front, so a missing one fails naming its package.
    """
    compiler_version: str = ""
    """The compiler's own full version (``-dumpfullversion``; clang ``-dumpversion``).

    Provenance for the kgcov matrix: the column is the NAME the lane selects
    by (``gcc-12``, ``clang``), and this is what that name resolved to on the
    machine that measured. Empty only on :data:`DEFAULT_TOOLCHAIN`, which is
    no matrix column.
    """
    kernel_release: str = ""
    """The kernel release the modules were built for (the bed's running kernel
    for a bed build; the cross tree's ``include/config/kernel.release``)."""


DEFAULT_TOOLCHAIN = Toolchain("default", {})

TOOLCHAINS_KEY = pytest.StashKey["dict[str, Toolchain]"]()
"""``config.stash`` slot: profile id -> the :class:`Toolchain` a fixture built with.

Filled by the ``built_with`` fixture (bed columns) and ``kernel_build``
(every build column), read by the kgcov observation hook
(``tests/e2e/cov/_kgcov_observation.py``) — the hook has an item and its
callspec, which carry the NAME, and this is how the name reaches the
version and release the fixture already measured, without probing again.
"""


def note_toolchain(config: pytest.Config, tc: Toolchain) -> None:
    """Record *tc* under its name for the observation hook."""
    slot = config.stash.get(TOOLCHAINS_KEY, None)
    if slot is None:
        slot = {}
        config.stash[TOOLCHAINS_KEY] = slot
    slot[tc.name] = tc


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
# remedy for exactly this. It reaches lcov as the bed hosts' record field
# `toolchain.lcov_args` (issue #385) — no ~/.lcovrc for a test that must not
# write into the developer's home, and no wrapper script standing in for the
# lcov binary.
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


def compiler_version_string(cc: str) -> str:
    """*cc*'s full dotted version, as the compiler prints it: ``12.3.0``, ``18.1.3``."""
    flag = "-dumpversion" if "clang" in cc else "-dumpfullversion"
    return subprocess.run([cc, flag], check=True, capture_output=True, text=True).stdout.strip()


def compiler_version_number(cc: str) -> int:
    """kbuild's numeric version for *cc*: 18.1.3 -> 180103, 9.5.0 -> 90500."""
    parts = [int(x) for x in compiler_version_string(cc).split(".")[:3]] + [0, 0]
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
        return Toolchain(
            name,
            env,
            lcov_args=list(CLANG_LCOV_ARGS),
            compiler_version=compiler_version_string("clang"),
            kernel_release=os.uname().release,
        )
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
    return Toolchain(
        name,
        env,
        compiler_version=compiler_version_string(name),
        kernel_release=os.uname().release,
    )


def foreign_toolchain(name: str, names: "list[str]") -> Toolchain:
    """The compiler whose demo *name*'s library must REFUSE, from the lane's own list.

    The control has to exercise the rule the docs state for this column
    (docs/cli/cov/instrumenting/kernel-modules.md, "Another kernel, ISA or
    compiler"): for a ``gcc-N`` column the same-major rule, so the foreign
    compiler is the nearest OTHER gcc major *names* holds; for the ``clang``
    column the family rule, so it is the system gcc. A gcc column in a list
    with no second gcc falls back to the family rule too (``clang``), and the
    ``default`` column takes ``clang`` as well. Every arm goes through
    :func:`toolchain`, so the foreign build gets the same treatment a column's
    own does — the unversioned name ``gcc`` included (its ``gcov`` is
    required, and an older system gcc gets the ``CONFIG_GCC_VERSION``
    compensation) — and a foreign compiler that is not installed fails there,
    naming it: the control never skips.
    """
    if name == "clang":
        return toolchain("gcc")
    if name.startswith("gcc-"):
        mine = int(name.split("-", 1)[1])
        others = [int(n.split("-", 1)[1]) for n in names if n.startswith("gcc-") and n != name]
        if others:
            return toolchain(f"gcc-{min(others, key=lambda major: (abs(major - mine), major))}")
    return toolchain("clang")


def build_foreign_demo(tc: Toolchain, root: Path) -> Path:
    """Build a COPY of the demo with *tc* against the library already in ``BUILD/lib``.

    The in-place fixture build belongs to the column under test and stays
    untouched; the copy is built the way the kernel builds build their own
    copy (``test_kgcov_kernel_builds.py``). ``KBUILD_MODPOST_WARN=1``: a gcc demo
    against a clang library (or the reverse) references a runtime symbol the
    library does not export, and modpost would otherwise FAIL THE BUILD on it
    — the family rule has to be allowed to reach ``insmod``, where the
    documented refusal lives. Answers the built ``.ko``.
    """
    demo = root / "foreign_demo"
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
    env = {**os.environ, **tc.env}
    env["KMAKEFLAGS"] = f"{tc.env.get('KMAKEFLAGS', '')} KBUILD_MODPOST_WARN=1".strip()
    try:
        subprocess.run(
            ["make", "-C", str(demo), f"KDIR={running_kernel_kdir()}", f"KGCOV={BUILD / 'lib'}"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"foreign demo build with {tc.name} failed (exit {exc.returncode}):\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc
    ko = demo / "otto_kmod_demo.ko"
    assert ko.is_file(), f"{tc.name} built no {ko}"
    return ko


def overlay_lab(tc: Toolchain, root: Path) -> "Path | None":
    """Write a lab file under *root* giving the bed hosts *tc*'s lcov arguments, or ``None``.

    ``None`` when *tc* needs no lcov arguments — every gcc, and the default —
    so those builds run against the fixture's lab data exactly as the routine
    e2e does, and otto finds the gcov from the counters.

    Otherwise the file re-declares the two bed elements and nothing else. The
    composite lab replaces an element WHOLESALE by slug, later source winning,
    so each element is copied from the fixture's lab data key for key and only
    a ``toolchain`` carrying ``lcov_args`` is added to its host entries — no
    ``lcov``, so the system one runs, and no ``gcov``, so the record stays
    silent about it and otto reads the counters with the tool the stamp names.
    The ``labs`` TABLE is deliberately absent: re-declaring it would replace
    the lab's resource lists, and this overlay has nothing to say about them.
    """
    if not tc.lcov_args:
        return None
    fixture = json.loads((LAB_DATA / "lab.json").read_text())
    elements = [e for e in fixture["elements"] if e["name"] in OVERLAY_ELEMENTS]
    assert len(elements) == len(OVERLAY_ELEMENTS), (
        f"{LAB_DATA / 'lab.json'} holds {[e['name'] for e in elements]}, "
        f"expected {list(OVERLAY_ELEMENTS)} — the bed elements were renamed?"
    )
    root.mkdir(parents=True, exist_ok=True)
    for element in elements:
        assert element.get("hosts"), f"{element['name']} has no host entry to configure"
        for host in element["hosts"]:
            host["toolchain"] = {"lcov_args": list(tc.lcov_args)}
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
    lab = overlay_lab(tc, root)
    if lab is None:
        return None
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


# ---- the kernel set of `make kgcov`'s build columns ---------------------------
#
# One KernelColumn per column: where its tree is and how its modules are
# built. Pure data, so a unit test can read it on a machine with neither
# docker nor a tree, and the provisioning script's `--list` is held to it.
# Every image column builds under `docker run` in a container of the
# kernel's OWN Ubuntu release with that release's gcc (a kernel tree is
# coupled to the compilers of its era); x86_64-cross alone is a host build,
# the 6.8 source tree with the VM's cross gcc 13, 6.8's own generation.

CROSS_KERNEL = "x86_64-cross"
KERNEL_IDS = [CROSS_KERNEL, "2.6.32", "3.13", "4.4", "5.4", "5.15", "6.17"]
PROVISION = "scripts/provision_kgcov_kernels.sh"
DEFAULT_KERNELS_DIR = Path("/home/vagrant/build/kgcov-kernels")
"""``scripts/provision_kgcov_kernels.sh``'s own default for ``OTTO_KGCOV_KERNELS_DIR`` (and the
Makefile's ``KGCOV_KERNELS_DIR ?=``). A column's printed ``provision`` remedy is prefixed with
``OTTO_KGCOV_KERNELS_DIR=<dir>`` only when its *kernels_dir* differs from this, so the remedy
still works when `make kgcov` was pointed somewhere else."""
ISA_MACHINE = {"x86_64": "Advanced Micro Devices X86-64", "arm64": "AArch64"}
"""What ``readelf -h`` prints as Machine, per ISA."""
ISA_OBJCOPY = {"x86_64": "x86_64-linux-gnu-objcopy", "arm64": "objcopy"}
"""The objcopy that reads each ISA's objects on the dev VM (the native one reads arm64 only)."""
CTORS_SECTION = ".ctors.65435"
"""The section Ubuntu's x86_64-linux-gnu-gcc-9 emits its gcov constructor into
(measured 2026-09-28)."""
SHT_PROGBITS = 1
SHT_INIT_ARRAY = 14
GCOV_CONSTRUCTOR = re.compile(
    r"^(?:_sub_I_00100_0|_GLOBAL__sub_I_65535_0_\w+(?:\.c)?|__llvm_gcov_init)$"
)
"""A gcov constructor's symbol: gcc 9 and newer, gcc 4.7 to 5 (suffixed by the
unit's first public symbol, function or variable, or the file name when it
defines none — gcc 4.7/4.8/5 keep that file name's ``.c``, so the trailing
``.c`` in the pattern above is optional, not guaranteed; measured 2026-09-29,
gcc 4.8/5.4 on ``demo_main.c``, every top-level function of which is
``static``), clang."""
_HEADERS_ABI = {
    "3.13": "3.13.0-170",
    "4.4": "4.4.0-210",
    "5.4": "5.4.0-218",
    "5.15": "5.15.0-198",
    "6.17": "6.17.0-41",
}


@dataclass(frozen=True)
class KernelColumn:
    """One kernel of the build columns, as the lane builds against it."""

    id: str
    isa: str
    tree: Path
    """KDIR: a prepared source tree or a headers package's -generic directory."""
    stamp: Path | None
    """The provisioning script's finished-tree stamp for :attr:`tree`; None for the cross column."""
    cc: str | None
    """CC on kbuild's command line; None for kbuild's own default."""
    image: str | None
    """The docker image the column builds in; None for a host build."""
    platform: str | None
    """``docker run --platform``; None for a host build."""
    env: dict[str, str]
    """Environment kbuild reads itself (ARCH, CROSS_COMPILE)."""
    kmakeflags: str
    """Extra make arguments, before KBUILD_MODPOST_WARN=1 is appended for a source tree."""
    modpost_may_warn: bool
    """True for a modules_prepare tree, which has no Module.symvers."""
    provision: str
    """The command that provisions the column, named when it is absent."""


def kernel_columns(kernels_dir: Path, cross_kdir: Path) -> list[KernelColumn]:
    """The whole set, in Makefile order.

    Rooted at *kernels_dir*; the cross tree is at *cross_kdir*.
    """
    provision_prefix = (
        "" if kernels_dir == DEFAULT_KERNELS_DIR else f"OTTO_KGCOV_KERNELS_DIR={kernels_dir} "
    )
    cross = KernelColumn(
        id=CROSS_KERNEL,
        isa="x86_64",
        tree=cross_kdir,
        stamp=None,
        cc=None,
        image=None,
        platform=None,
        env={"ARCH": "x86_64", "CROSS_COMPILE": "x86_64-linux-gnu-"},
        kmakeflags="",
        modpost_may_warn=True,
        provision=(
            f"make -C {cross_kdir} ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu- "
            "defconfig modules_prepare"
        ),
    )
    oldest_tree = kernels_dir / "2.6.32" / "src" / "linux-2.6.32.71"
    oldest = KernelColumn(
        id="2.6.32",
        isa="x86_64",
        tree=oldest_tree,
        stamp=kernels_dir / "2.6.32" / f".prepared-{oldest_tree.name}",
        cc="gcc-4.7",
        image="otto-kgcov-kernel:2.6.32",
        platform="linux/amd64",
        env={},
        kmakeflags="HOSTCC=gcc-4.7",
        modpost_may_warn=True,
        provision=f"{provision_prefix}{PROVISION} 2.6.32",
    )
    headers = []
    for kernel_id, abi in _HEADERS_ABI.items():
        tree = kernels_dir / kernel_id / "root" / "usr" / "src" / f"linux-headers-{abi}-generic"
        headers.append(
            KernelColumn(
                id=kernel_id,
                isa="arm64",
                tree=tree,
                stamp=kernels_dir / kernel_id / f".prepared-{tree.name}",
                cc=None,
                image=f"otto-kgcov-kernel:{kernel_id}",
                platform="linux/arm64",
                env={},
                kmakeflags="",
                modpost_may_warn=False,
                provision=f"{provision_prefix}{PROVISION} {kernel_id}",
            )
        )
    return [cross, oldest, *headers]


def kernel_column(kernel_id: str, *, kernels_dir: Path, cross_kdir: Path) -> KernelColumn:
    """The column named *kernel_id*, or a ValueError naming the set."""
    for column in kernel_columns(kernels_dir, cross_kdir):
        if column.id == kernel_id:
            return column
    raise ValueError(f"{kernel_id!r} is not a kernel column; the set is {', '.join(KERNEL_IDS)}")


def column_env(column: KernelColumn) -> dict[str, str]:
    """What ``build.sh`` and the demo's Makefile take for *column*.

    The mapping merges ``column.env`` — the column's own kbuild variables,
    ``ARCH`` and ``CROSS_COMPILE`` on the cross column — with ``KDIR``
    always, ``CC`` when the column names a compiler, and ``KMAKEFLAGS`` when
    the column carries kmakeflags or needs the modpost-warn override.
    """
    env = {"KDIR": str(column.tree), **column.env}
    if column.cc:
        env["CC"] = column.cc
    flags = [
        f
        for f in (column.kmakeflags, "KBUILD_MODPOST_WARN=1" if column.modpost_may_warn else "")
        if f
    ]
    if flags:
        env["KMAKEFLAGS"] = " ".join(flags)
    return env


def column_prerequisite_error(
    column: KernelColumn,
    *,
    tree_prepared: bool,
    image_present: bool,
    docker_present: bool,
    compiler_present: bool,
    objcopy_present: bool,
) -> str | None:
    """Why *column* cannot build yet, naming what provisions it — or None. Nothing skips."""
    objcopy_error = (
        f"{column.id}: {ISA_OBJCOPY['x86_64']} is not on PATH; install "
        "binutils-x86-64-linux-gnu, the ctors row's objcopy"
    )
    if column.image is None:
        if not compiler_present:
            return (
                f"{column.id}: {column.env.get('CROSS_COMPILE', '')}gcc is not on PATH; install "
                "gcc-x86-64-linux-gnu, the cross compiler this column builds with"
            )
        if column.isa == "x86_64" and not objcopy_present:
            return objcopy_error
        if not tree_prepared:
            return (
                f"{column.id}: no prepared kernel tree at {column.tree}; run `{column.provision}`"
            )
        return None
    if not docker_present:
        return (
            f"{column.id}: docker is not on PATH (apt install docker.io); "
            f"the column builds in {column.image}"
        )
    if column.isa == "x86_64" and not objcopy_present:
        return objcopy_error
    if not tree_prepared:
        return f"{column.id}: no prepared kernel tree at {column.tree}; run `{column.provision}`"
    if not image_present:
        return f"{column.id}: no docker image {column.image}; run `{column.provision}`"
    return None


def tree_prepared(column: KernelColumn) -> bool:
    """Whether *column*'s tree is one a build can trust.

    A provisioned column's tree counts only with its non-empty stamp, which the
    provisioning script writes last; every column also needs the two files a
    module build reads first, ``include/config/kernel.release`` and
    ``scripts/mod/modpost``.
    """
    if column.stamp is not None and not (
        column.stamp.is_file() and column.stamp.stat().st_size > 0
    ):
        return False
    return (column.tree / "include" / "config" / "kernel.release").is_file() and (
        column.tree / "scripts" / "mod" / "modpost"
    ).is_file()


def run_in_column(
    column: KernelColumn,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    mounts: list[Path],
) -> subprocess.CompletedProcess[str]:
    """Run *argv* the way *column* builds: on the host, or under ``docker run`` in its image.

    Inside the image every mount sits at its own host path and the repository
    is mounted read-only at its host path too, so every path a diagnostic or
    a ``.gcno`` names is the same inside and out. The container runs as the
    calling user, so what it writes is the user's. Raises
    :class:`subprocess.CalledProcessError` with both streams captured.
    """
    if column.image is None:
        return subprocess.run(
            argv, cwd=cwd, env={**os.environ, **env}, check=True, capture_output=True, text=True
        )
    assert column.platform is not None
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        column.platform,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{PROJECT_ROOT}:{PROJECT_ROOT}:ro",
        "-w",
        str(cwd),
    ]
    for mount in mounts:
        cmd += ["-v", f"{mount}:{mount}"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [column.image, *argv]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def tree_mount(column: KernelColumn) -> Path:
    """What to bind-mount for *column*'s tree: its PARENT, not the leaf itself.

    An Ubuntu ``linux-headers-<abi>-generic`` package is a thin arch/config
    overlay whose ``scripts/``, ``fs/``, ``mm/`` and most of the rest are
    relative symlinks into a sibling ``linux-headers-<abi>`` directory one or
    more levels up (measured 2026-09-29: 455 of them for 6.17, none escaping
    their shared ``usr/src``); mounting only the ``-generic`` leaf leaves
    those targets outside the container and kbuild fails on the first one it
    reads (``scripts/Kbuild.include``). The cross and 2.6.32 source trees
    have no such sibling, so mounting their parent is simply a harmless wider
    mount.
    """
    return column.tree.parent


_BANNER_VERSION = re.compile(r"\)\s+(\d+\.\d+(?:\.\d+)?)")


def compiler_version_from_banner(first_line: str) -> str:
    """The compiler's dotted version in *first_line* of a ``<cc> --version`` banner.

    The first dotted number after the closing parenthesis: Ubuntu's gcc
    packages close the parenthesised distro string with the compiler's own
    version and, on some releases, a build DATE straight after it with no
    separating punctuation (``... 5.4.0-6ubuntu1~16.04.12) 5.4.0 20160609``,
    Ubuntu 16.04's gcc) — the date is not the version, and being all-digits
    it would otherwise win a naive "last token of the line" read.
    """
    m = _BANNER_VERSION.search(first_line)
    assert m, f"no version found in compiler banner: {first_line!r}"
    return m.group(1)


def column_compiler_version(column: KernelColumn) -> str:
    """The full version of the compiler *column* builds with: ``4.7.3``, ``13.3.0``.

    A host column asks ``-dumpfullversion``; an image column parses
    :func:`compiler_version_from_banner` off the first line of ``<cc>
    --version`` inside the image, because gcc 4.x has no ``-dumpfullversion``
    and its ``-dumpversion`` says ``4.8``.
    """
    cc = column.cc or f"{column.env.get('CROSS_COMPILE', '')}gcc"
    if column.image is None:
        return compiler_version_string(cc)
    out = run_in_column(column, [cc, "--version"], cwd=Path("/"), env={}, mounts=[]).stdout
    return compiler_version_from_banner(out.splitlines()[0])


_WARNING_LINE = re.compile(r"^(?P<path>[^:\s]+):\d+(?::\d+)?: warning:")
"""A ``warning:`` with no ``file:line`` prefix (a linker warning, or one about the command line
itself) is not attributed to any file, matching the spec's rule that a column fails only on a
warning whose file lies under the library's build directory."""


def warnings_under(output: str, directory: Path) -> list[str]:
    """The ``warning:`` lines of *output* whose file lies under *directory*.

    An absolute path counts by prefix; a relative one only when the file
    exists under *directory* (kbuild prints in-tree header paths relative to
    the kernel tree, and those are not the library's).
    """
    root = directory.resolve()
    hits = []
    for line in output.splitlines():
        m = _WARNING_LINE.match(line)
        if not m:
            continue
        path = Path(m.group("path"))
        if not path.is_absolute():
            path = directory / path
            if not path.exists():
                continue
        if path.resolve().is_relative_to(root):
            hits.append(line)
    return hits


def assert_bracketed(syms: list[str], units: int) -> None:
    """*syms* is the begin marker, one gcov constructor per unit, the end marker."""
    assert syms, syms
    assert syms[0] == "__kgcov_begin_marker", syms
    assert syms[-1] == "__kgcov_end_marker", syms
    inner = syms[1:-1]
    assert len(inner) == units, syms
    assert all(GCOV_CONSTRUCTOR.match(s) for s in inner), syms


@dataclass(frozen=True)
class ElfSection:
    index: int
    name: str
    sh_type: int


def _elf64_header_table(data: bytes | bytearray) -> tuple[str, int, int, int, int]:
    """``(endian, shoff, shentsize, shnum, shstrndx)`` of an ELF64 file, or an AssertionError."""
    assert data[:4] == b"\x7fELF", "not an ELF64 file"
    assert data[4] == 2, "not an ELF64 file"
    endian = "<" if data[5] == 1 else ">"
    (shoff,) = struct.unpack_from(endian + "Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", data, 0x3A)
    return endian, shoff, shentsize, shnum, shstrndx


def elf_sections(path: Path) -> list[ElfSection]:
    """Every section header's name and type, in index order, without readelf."""
    data = path.read_bytes()
    endian, shoff, shentsize, shnum, shstrndx = _elf64_header_table(data)

    def header(i: int):
        return struct.unpack_from(endian + "IIQQQQIIQQ", data, shoff + i * shentsize)

    strtab = header(shstrndx)[4]
    out = []
    for i in range(shnum):
        name_off, sh_type = header(i)[0], header(i)[1]
        end = data.index(b"\0", strtab + name_off)
        out.append(ElfSection(i, data[strtab + name_off : end].decode(), sh_type))
    return out


def set_section_type(path: Path, section: str, sh_type: int) -> None:
    """Overwrite one section header's ``sh_type`` in place (four bytes at a known offset)."""
    data = bytearray(path.read_bytes())
    endian, shoff, shentsize, _, _ = _elf64_header_table(data)
    matches = [s for s in elf_sections(path) if s.name == section]
    assert len(matches) == 1, (
        f"{path}: no section named {section}" if not matches else f"{path}: two {section}"
    )
    struct.pack_into(endian + "I", data, shoff + matches[0].index * shentsize + 4, sh_type)
    path.write_bytes(bytes(data))


def constructor_section(obj: Path) -> str:
    """The one ``.init_array`` or ``.init_array.NNNNN`` section of *obj* (its constructor)."""
    names = [s.name for s in elf_sections(obj) if re.fullmatch(r"\.init_array(\.\d+)?", s.name)]
    assert names, f"{obj}: no .init_array section"
    assert len(names) == 1, f"{obj}: two constructor sections {names}"
    return names[0]


def rename_constructor_section(obj: Path, isa: str) -> None:
    """Turn *obj*'s constructor section into the PROGBITS ``.ctors.65435``.

    That is what a `.ctors` toolchain emits natively. The ISA's objcopy
    renames it (and its relocation section with it); the type is then set
    by hand, because binutils 2.42 has no ``--set-section-type`` and a
    renamed section keeps ``SHT_INIT_ARRAY``. An *obj* that already carries
    a ``.ctors``-named section (already renamed) fails naming that section,
    rather than the unrelated "no .init_array section".
    """
    ctors = [s.name for s in elf_sections(obj) if re.fullmatch(r"\.ctors(\.\d+)?", s.name)]
    assert not ctors, f"{obj}: already has {ctors[0]}"
    old = constructor_section(obj)
    subprocess.run(
        [ISA_OBJCOPY[isa], "--rename-section", f"{old}={CTORS_SECTION}", str(obj)],
        check=True,
        capture_output=True,
        text=True,
    )
    set_section_type(obj, CTORS_SECTION, SHT_PROGBITS)


def snapshot_tree(directory: Path) -> list[tuple[str, int, int]]:
    """Every file under *directory* (recursively, dotfiles included), sorted.

    Each entry is ``(relative path, size, st_mtime_ns)``. Comparable across
    two points in time to prove a tree OUTSIDE the current build was left
    untouched: a file that changed size or mtime, or one that appeared or
    disappeared, moves this list.
    """
    return sorted(
        (str(p.relative_to(directory)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.rglob("*")
        if p.is_file()
    )


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
