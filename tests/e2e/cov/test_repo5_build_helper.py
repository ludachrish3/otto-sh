"""The pure logic of ``tests/e2e/cov/_repo5_build.py``'s toolchain support.

No compiler runs here: the version probes and PATH lookups are patched, the
kernel config is a temporary file. What is asserted is the environment a
compiler name turns into, the staleness rule the toolchain stamp adds, and
the ``.init_array`` reader's parse of readelf's table.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests.e2e.cov import _repo5_build as helper

pytestmark = pytest.mark.hostless

_GCC13_CONFIG = (
    "CONFIG_CC_IS_GCC=y\nCONFIG_GCC_VERSION=130300\nCONFIG_SHADOW_CALL_STACK=y\n"
    "CONFIG_INIT_STACK_ALL_ZERO=y\nCONFIG_ZERO_CALL_USED_REGS=y\n"
)


@pytest.fixture
def gcc13_kdir(tmp_path: Path) -> Path:
    (tmp_path / ".config").write_text(_GCC13_CONFIG)
    return tmp_path


@pytest.fixture
def compilers_present(monkeypatch) -> dict[str, str]:
    """Every compiler on PATH, every version probe answered, and NOTHING run.

    Returns the version table the arms fill -- ``compilers_present["gcc-9"] =
    "9.5.0"`` -- read by the patched :func:`helper.compiler_version_string`,
    which is the single seam every version goes through: ``toolchain`` asks it
    once through :func:`helper.compiler_version_number` for kbuild's numeric
    version and once directly for the stamp on the :class:`helper.Toolchain`.
    Patching the derived ``compiler_version_number`` instead leaves that second
    call exec'ing the real compiler, so each arm passed or failed on whether
    the machine happened to have the gcc it names -- green on a dev box with
    every gcc installed, red on a CI runner with three of them (issue #405).

    Banning ``subprocess.run`` for the duration is what keeps that from coming
    back: a probe that escapes the patch is an AssertionError on EVERY machine
    rather than a red only where the compiler is absent.
    """
    versions: dict[str, str] = {}

    def version_of(cc: str) -> str:
        assert cc in versions, f"no version set for {cc}; compilers_present has {sorted(versions)}"
        return versions[cc]

    def no_compiler_runs(argv, **kwargs):
        raise AssertionError(f"this file runs no compiler; {argv} escaped the patched probes")

    monkeypatch.setattr(helper.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(helper, "compiler_version_string", version_of)
    monkeypatch.setattr(helper.subprocess, "run", no_compiler_runs)
    return versions


def test_default_toolchain_adds_nothing_to_the_environment():
    assert helper.toolchain("default") == helper.DEFAULT_TOOLCHAIN
    assert helper.DEFAULT_TOOLCHAIN.env == {}


def test_a_gcc_newer_than_or_equal_to_the_kernels_only_sets_cc(gcc13_kdir, compilers_present):
    compilers_present["gcc-14"] = "14.2.0"
    tc = helper.toolchain("gcc-14", gcc13_kdir)
    assert tc.env == {"CC": "gcc-14"}
    assert tc.compiler_version == "14.2.0"


def test_an_older_gcc_tells_kbuild_its_version_and_drops_the_options_it_lacks(
    gcc13_kdir, compilers_present
):
    compilers_present["gcc-11"] = "11.5.0"
    env = helper.toolchain("gcc-11", gcc13_kdir).env
    assert env["CC"] == "gcc-11"
    flags = env["KMAKEFLAGS"].split()
    assert flags[0] == "CONFIG_GCC_VERSION=110500"
    # gcc 11 has -fzero-call-used-regs; it lacks shadow-call-stack and
    # trivial-auto-var-init, both gcc 12.
    assert set(flags[1:]) == {"CONFIG_SHADOW_CALL_STACK=", "CONFIG_INIT_STACK_ALL_ZERO="}


def test_an_older_gcc_drops_only_options_the_kernel_turned_on(tmp_path, compilers_present):
    (tmp_path / ".config").write_text("CONFIG_CC_IS_GCC=y\nCONFIG_GCC_VERSION=130300\n")
    compilers_present["gcc-9"] = "9.5.0"
    assert helper.toolchain("gcc-9", tmp_path).env["KMAKEFLAGS"] == "CONFIG_GCC_VERSION=90500"


def test_clang_on_a_gcc_kernel_uses_llvm_and_the_gcc_kernel_overrides(
    gcc13_kdir, compilers_present
):
    compilers_present["clang"] = "18.1.3"
    env = helper.toolchain("clang", gcc13_kdir).env
    assert env["LLVM"] == "1"
    assert "CC" not in env
    assert env["KMAKEFLAGS"] == helper.CLANG_ON_GCC_KERNEL.format(version=180103)


def test_clang_on_a_clang_kernel_needs_no_overrides(tmp_path, compilers_present):
    (tmp_path / ".config").write_text("CONFIG_CC_IS_CLANG=y\nCONFIG_CLANG_VERSION=170000\n")
    compilers_present["clang"] = "18.1.3"
    assert helper.toolchain("clang", tmp_path).env == {"LLVM": "1"}


def test_a_compiler_that_is_not_installed_fails_naming_it(gcc13_kdir, monkeypatch):
    monkeypatch.setattr(helper.shutil, "which", lambda name: None)
    with pytest.raises(AssertionError, match="gcc-12 is not on PATH"):
        helper.toolchain("gcc-12", gcc13_kdir)


def test_the_stamp_makes_artifacts_built_by_another_compiler_stale(tmp_path, monkeypatch):
    lib = tmp_path / "lib.ko"
    demo = tmp_path / "demo.ko"
    src = tmp_path / "src.c"
    src.write_text("int x;\n")
    lib.write_bytes(b"\x7fELF")
    demo.write_bytes(b"\x7fELF")
    stamp = tmp_path / "toolchain"
    monkeypatch.setattr(helper, "STAMP", stamp)
    assert helper.kmod_artifacts_are_stale([lib, demo], [src], helper.DEFAULT_TOOLCHAIN)
    stamp.write_text("default\n")
    assert not helper.kmod_artifacts_are_stale([lib, demo], [src], helper.DEFAULT_TOOLCHAIN)
    assert helper.kmod_artifacts_are_stale(
        [lib, demo], [src], helper.Toolchain("gcc-12", {"CC": "gcc-12"})
    )


def test_ensure_kmod_artifacts_tells_the_build_script_which_toolchain_ran(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "_assert_committed", lambda paths, tmp_path_factory: None)
    monkeypatch.setattr(helper, "kmod_artifacts_are_stale", lambda artifacts, sources, tc: True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    helper.ensure_kmod_artifacts(None, helper.Toolchain("gcc-12", {"CC": "gcc-12"}))
    assert len(calls) == 1
    argv, env = calls[0]
    assert argv == [str(helper.KMOD_BUILD)]
    assert env["CC"] == "gcc-12"
    assert env["OTTO_KGCOV_TOOLCHAIN"] == "gcc-12"


# The measured shape of readelf's three tables for a demo .ko as GNU as on
# aarch64 writes them (2026-09-18, gcc 13.3 / binutils 2.42) — reduced to the
# rows the parser must handle, not a verbatim capture: the .init_array
# relocations name the enclosing SECTION plus an offset, the section headers
# give each section its REAL index (.text = 1, .text.startup = 7, as built),
# and the symbol table holds, immediately before every FUNC (both markers
# and every gcov constructor), a same-Value, same-Ndx aarch64 mapping symbol
# ($x, NOTYPE, size 0) that a naive scan could mistake for the constructor.
# The .rela.init_array rows are deliberately out of offset order.
_READELF_R = """
Relocation section '.rela.init_array' at offset 0x33748 contains 5 entries:
    Offset             Info             Type               Symbol's Value  Symbol's Name + Addend
0000000000000000  0000000100000101 R_AARCH64_ABS64        0000000000000000 .text + 8
0000000000000010  0000000400000101 R_AARCH64_ABS64        0000000000000000 .text.startup + 58
0000000000000008  0000000400000101 R_AARCH64_ABS64        0000000000000000 .text.startup + 8
0000000000000018  0000000400000101 R_AARCH64_ABS64        0000000000000000 .text.startup + a8
0000000000000020  0000000100000101 R_AARCH64_ABS64        0000000000000000 .text + 1340

Relocation section '.rela.text' at offset 0x2000 contains 1 entry:
    Offset             Info             Type               Symbol's Value  Symbol's Name + Addend
0000000000000004  0000000a0000011b R_AARCH64_CALL26       0000000000000000 printk + 0
"""
_READELF_S = """
There are 40 section headers, starting at offset 0x35a48:

Section Headers:
  [Nr] Name              Type            Address          Off    Size   ES Flg Lk Inf Al
  [ 0]                   NULL            0000000000000000 000000 000000 00      0   0  0
  [ 1] .text             PROGBITS        0000000000000000 000040 001360 00  AX  0   0  8
  [ 2] .rela.text        RELA            0000000000000000 020000 000300 18   I 37   1  8
  [ 7] .text.startup     PROGBITS        0000000000000000 001400 000100 00  AX  0   0  4
  [ 9] .init_array       INIT_ARRAY      0000000000000000 001600 000028 08  WA  0   0  8
"""
_READELF_SYM = """
Symbol table '.symtab' contains 11 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     0: 0000000000000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 0000000000000008     0 NOTYPE  LOCAL  DEFAULT    1 $x
     2: 0000000000000008    12 FUNC    LOCAL  DEFAULT    1 __kgcov_begin_marker
     3: 0000000000000008     0 NOTYPE  LOCAL  DEFAULT    7 $x
     4: 0000000000000008    80 FUNC    LOCAL  DEFAULT    7 _sub_I_00100_0
     5: 0000000000000058     0 NOTYPE  LOCAL  DEFAULT    7 $x
     6: 0000000000000058    80 FUNC    LOCAL  DEFAULT    7 _sub_I_00100_0
     7: 00000000000000a8     0 NOTYPE  LOCAL  DEFAULT    7 $x
     8: 00000000000000a8    80 FUNC    LOCAL  DEFAULT    7 _sub_I_00100_0
     9: 0000000000001340     0 NOTYPE  LOCAL  DEFAULT    1 $x
    10: 0000000000001340    12 FUNC    LOCAL  DEFAULT    1 __kgcov_end_marker
"""
_READELF_BY_FLAG = {"-rW": _READELF_R, "-SW": _READELF_S, "-sW": _READELF_SYM}


@pytest.fixture
def fake_readelf(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[0] == "readelf", argv
        return subprocess.CompletedProcess(argv, 0, stdout=_READELF_BY_FLAG[argv[1]], stderr="")

    monkeypatch.setattr(helper.subprocess, "run", fake_run)


def test_init_array_symbols_resolve_section_relocations_in_offset_order(fake_readelf, tmp_path):
    assert helper.init_array_symbols(tmp_path / "x.ko") == [
        "__kgcov_begin_marker",
        "_sub_I_00100_0",
        "_sub_I_00100_0",
        "_sub_I_00100_0",
        "__kgcov_end_marker",
    ]


def test_init_array_symbols_keep_a_named_relocation_as_is(monkeypatch, tmp_path):
    named = _READELF_R.replace(".text + 8", "__kgcov_begin_marker + 0")
    tables = {**_READELF_BY_FLAG, "-rW": named}
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=tables[argv[1]], stderr=""),
    )
    assert helper.init_array_symbols(tmp_path / "x.ko")[0] == "__kgcov_begin_marker"


def test_init_array_symbols_fails_on_a_module_without_the_section(monkeypatch, tmp_path):
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="no relocs\n", stderr=""),
    )
    with pytest.raises(AssertionError, match=r"no \.rela\.init_array"):
        helper.init_array_symbols(tmp_path / "x.ko")


# --- The overlay: only what otto cannot find from the data -------------------
#
# otto reads a Unix host's counters with the gcov the host RECORD names when
# it names one, and otherwise with the gcov the counters' own stamp names —
# gcov-N for a gcc, llvm-cov for clang, from PATH. So a gcc arm configures
# nothing on the bed and the matrix's green is the discovery proof. Clang's
# arm still needs lcov to ignore the kernel headers it cannot open, and that
# is an lcov argument, not a gcov, so it travels as the host record's
# toolchain.lcov_args. A compiler whose gcov is not installed fails HERE,
# naming the package, rather than in otto's report.


def test_a_gcc_requires_its_gcov_and_configures_nothing(gcc13_kdir, compilers_present):
    compilers_present["gcc-12"] = "12.3.0"
    tc = helper.toolchain("gcc-12", gcc13_kdir)
    assert tc.lcov_args == []
    assert helper.overlay_lab(tc, gcc13_kdir / "overlay") is None
    assert helper.overlay_repo(tc, gcc13_kdir / "overlay") is None


def test_a_gcc_whose_gcov_is_missing_fails_naming_the_compiler(gcc13_kdir, monkeypatch):
    monkeypatch.setattr(
        helper.shutil, "which", lambda name: None if "gcov" in name else f"/x/{name}"
    )
    with pytest.raises(AssertionError, match=r"gcov-12 is not on PATH.*install gcc-12"):
        helper.toolchain("gcc-12", gcc13_kdir)


def test_clang_requires_llvm_cov_and_carries_the_ignore_errors_pair(gcc13_kdir, compilers_present):
    compilers_present["clang"] = "18.1.3"
    assert helper.toolchain("clang", gcc13_kdir).lcov_args == ["--ignore-errors", "source"]


def test_clang_without_llvm_cov_fails_naming_llvm(gcc13_kdir, monkeypatch):
    monkeypatch.setattr(
        helper.shutil, "which", lambda name: None if name == "llvm-cov" else f"/usr/bin/{name}"
    )
    with pytest.raises(AssertionError, match=r"llvm-cov is not on PATH.*install llvm"):
        helper.toolchain("clang", gcc13_kdir)


def test_the_default_toolchain_builds_no_overlay(tmp_path):
    assert helper.DEFAULT_TOOLCHAIN.lcov_args == []
    assert helper.overlay_lab(helper.DEFAULT_TOOLCHAIN, tmp_path) is None
    assert helper.overlay_repo(helper.DEFAULT_TOOLCHAIN, tmp_path) is None


def test_extra_lcov_arguments_are_opt_in_and_clangs_are_the_ignore_errors_pair():
    assert helper.Toolchain("gcc-12", {"CC": "gcc-12"}).lcov_args == []
    assert helper.CLANG_LCOV_ARGS == ["--ignore-errors", "source"]


@pytest.fixture
def clang_overlay(tmp_path) -> Path:
    tc = helper.Toolchain("clang", {"LLVM": "1"}, lcov_args=list(helper.CLANG_LCOV_ARGS))
    lab = helper.overlay_lab(tc, tmp_path)
    assert lab is not None
    return lab


def test_the_overlay_redeclares_only_the_two_bed_elements(clang_overlay):
    data = json.loads(clang_overlay.read_text())
    assert sorted(data) == ["elements"], data.keys()
    assert [e["name"] for e in data["elements"]] == ["test1", "test2"]


def test_the_overlay_copies_every_original_key_of_those_elements(clang_overlay):
    fixture = json.loads((helper.LAB_DATA / "lab.json").read_text())
    originals = {e["name"]: e for e in fixture["elements"] if e["name"] in ("test1", "test2")}
    for element in json.loads(clang_overlay.read_text())["elements"]:
        original = originals[element["name"]]
        assert sorted(element) == sorted(original)
        for key, value in original.items():
            if key != "hosts":
                assert element[key] == value, key
        # `labs` in particular: the composite replaces an element WHOLESALE,
        # so an overlay that dropped it would take the host out of the lab.
        assert element["labs"] == original["labs"]


def test_the_overlay_names_only_lcov_arguments_in_every_host_entry(clang_overlay):
    for element in json.loads(clang_overlay.read_text())["elements"]:
        assert element["hosts"]
        for host in element["hosts"]:
            # No lcov, no gcov and no sysroot: the arguments are all the bed
            # needs told. The record stays silent about the binaries, so the
            # system lcov runs and otto reads the counters with the llvm-cov
            # the stamp names.
            assert sorted(host["toolchain"]) == ["lcov_args"], host["toolchain"]
            assert host["toolchain"]["lcov_args"] == ["--ignore-errors", "source"]


def test_the_overlay_repo_points_a_lab_source_at_that_lab_file(tmp_path):
    tc = helper.Toolchain("clang", {"LLVM": "1"}, lcov_args=list(helper.CLANG_LCOV_ARGS))
    repo = helper.overlay_repo(tc, tmp_path)
    assert repo is not None
    settings = (repo / ".otto" / "settings.toml").read_text()
    assert 'backend = "json"' in settings
    lab = helper.overlay_lab(tc, tmp_path)
    assert lab is not None
    assert str(lab) in settings
    # Nothing else: the overlay contributes lab data and no products, tests
    # or project block that could shadow repo5's.
    assert "[[products]]" not in settings
    assert "tests =" not in settings
