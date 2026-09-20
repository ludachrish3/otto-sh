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

Filled by the ``built_with`` fixture (bed columns) and ``cross_build``
(``x86_64-cross``), read by the kgcov observation hook
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
    untouched; the copy is built the way the cross build builds its own copy
    (``test_kgcov_cross_build.py``). ``KBUILD_MODPOST_WARN=1``: a gcc demo
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
