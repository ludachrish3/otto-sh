"""otto.kgcov: the shipped library, its interface number and the .modinfo reader."""

import re
from pathlib import Path

import pytest

from otto import kgcov
from otto.version import get_version
from tests._fixtures.paths import PROJECT_ROOT

PACKAGE = PROJECT_ROOT / "src" / "otto" / "kgcov"


def test_the_interface_number_in_kgcov_h_is_the_python_one():
    header = (PACKAGE / "kgcov.h").read_text()
    m = re.search(r"^#define KGCOV_INTERFACE (\d+)$", header, re.MULTILINE)
    assert m, "kgcov.h defines no KGCOV_INTERFACE"
    assert int(m.group(1)) == kgcov.INTERFACE


def test_every_shipped_file_exists_and_the_package_holds_nothing_else():
    for name in kgcov.SHIPPED_FILES:
        assert (PACKAGE / name).is_file(), name
    on_disk = {p.name for p in PACKAGE.iterdir() if p.name != "__pycache__"}
    assert on_disk == {*kgcov.SHIPPED_FILES, kgcov.VERSION_HEADER, "__init__.py"}, (
        "the wheel ships every file under src/otto/: a build product or a stray file here "
        "would ship too"
    )


def test_the_in_tree_version_header_says_source():
    assert (PACKAGE / kgcov.VERSION_HEADER).read_text() == kgcov.version_header("source")


def test_the_module_version_is_built_from_the_version_header_and_the_interface():
    source = (PACKAGE / "kgcov.c").read_text()
    assert '#include "kgcov_version.h"' in source
    assert 'MODULE_VERSION(KGCOV_OTTO_VERSION "+kgcov" KGCOV_STR(KGCOV_INTERFACE));' in source


def test_the_override_header_is_force_included_only_when_present():
    kbuild = (PACKAGE / "Kbuild").read_text()
    assert (
        "ccflags-y += $(if $(wildcard $(src)/kgcov_local.h),-include $(src)/kgcov_local.h)"
        in kbuild
    )
    gcov_h = (PACKAGE / "kgcov_gcov.h").read_text()
    for macro in (
        "KGCOV_ALLOC",
        "KGCOV_ALLOC_ARRAY",
        "KGCOV_STRDUP",
        "KGCOV_MEMDUP",
        "KGCOV_ASPRINTF",
        "KGCOV_FREE",
        "KGCOV_BIG_ALLOC",
        "KGCOV_BIG_FREE",
        "KGCOV_DEFINE_LOCK",
        "KGCOV_LOCK",
        "KGCOV_UNLOCK",
        "KGCOV_DEBUGFS_DIR",
        "KGCOV_DEBUGFS_FILE",
        "KGCOV_DEBUGFS_REMOVE",
    ):
        assert f"#ifndef {macro}\n#define {macro}" in gcov_h, macro


@pytest.mark.parametrize("name", ["kgcov.c", "kgcov_gcc.c", "kgcov_gcc_abi.c", "kgcov_clang.c"])
def test_the_library_calls_no_kernel_allocator_lock_or_debugfs_helper_directly(name):
    source = (PACKAGE / name).read_text()
    for bare in (
        "kzalloc(",
        "kcalloc(",
        "kstrdup(",
        "kmemdup(",
        "kasprintf(",
        "kfree(",
        "kvmalloc(",
        "kvfree(",
        "vmalloc(",
        "vfree(",
        "DEFINE_MUTEX(",
        "mutex_lock(",
        "mutex_unlock(",
        "debugfs_create_dir(",
        "debugfs_create_file(",
        "debugfs_remove_recursive(",
    ):
        assert bare not in source, f"{name} calls {bare} directly; use the KGCOV_ macro"


def _ko_bytes(*modinfo: str) -> bytes:
    """A stand-in .ko: NUL-separated key=value strings, the way .modinfo lays them out."""
    return b"\x7fELF" + b"\x00" + b"\x00".join(s.encode() for s in modinfo) + b"\x00"


def test_modinfo_version_reads_version_and_not_srcversion(tmp_path: Path):
    ko = tmp_path / "otto_kgcov.ko"
    ko.write_bytes(_ko_bytes("srcversion=ABCDEF", "version=1.6.0+kgcov1", "vermagic=6.8.0 SMP"))
    assert kgcov.modinfo_version(ko) == "1.6.0+kgcov1"


def test_modinfo_version_is_none_without_a_version_string(tmp_path: Path):
    ko = tmp_path / "otto_kgcov.ko"
    ko.write_bytes(_ko_bytes("srcversion=ABCDEF", "vermagic=6.8.0 SMP"))
    assert kgcov.modinfo_version(ko) is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.6.0+kgcov1", 1), ("source+kgcov12", 12), ("1.6.0", None), ("kgcov1", None), ("", None)],
)
def test_interface_of_parses_the_kgcov_suffix_only(version, expected):
    assert kgcov.interface_of(version) == expected


REPO5_VENDORED = PROJECT_ROOT / "tests" / "repo5" / "third_party" / "otto_kgcov"


def test_export_into_an_empty_directory_writes_every_shipped_file_and_the_version_header(
    tmp_path: Path,
):
    result = kgcov.export_tree(tmp_path / "vendored")
    assert sorted(result.changed) == sorted([*kgcov.SHIPPED_FILES, kgcov.VERSION_HEADER])
    assert result.version == get_version()
    for name in kgcov.SHIPPED_FILES:
        assert (tmp_path / "vendored" / name).read_bytes() == (PACKAGE / name).read_bytes()
    assert (tmp_path / "vendored" / kgcov.VERSION_HEADER).read_text() == kgcov.version_header(
        get_version()
    )
    assert (tmp_path / "vendored" / "build.sh").stat().st_mode & 0o111, "build.sh lost its x bit"


def test_a_second_export_changes_nothing_and_says_so(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    assert kgcov.export_tree(tmp_path).changed == []


def test_export_overwrites_an_edited_file_and_names_it(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    (tmp_path / "kgcov.c").write_text("// edited\n")
    result = kgcov.export_tree(tmp_path)
    assert result.changed == ["kgcov.c"]
    assert (tmp_path / "kgcov.c").read_bytes() == (PACKAGE / "kgcov.c").read_bytes()


def test_export_restores_a_stripped_execute_bit_on_an_unchanged_file(tmp_path: Path):
    """A vendored file whose bytes match but whose x bit was stripped still needs restoring."""
    kgcov.export_tree(tmp_path)
    (tmp_path / "build.sh").chmod(0o644)
    result = kgcov.export_tree(tmp_path)
    assert result.changed == ["build.sh"]
    assert (tmp_path / "build.sh").stat().st_mode & 0o111, "build.sh lost its x bit"


def test_export_ignores_a_mode_difference_outside_the_execute_bits(tmp_path: Path):
    """Only the x bit is version-tracked, so only the x bit is part of "unchanged".

    An installed wheel's 0644 sources against a umask-002 checkout's 0664
    would otherwise make the first re-export report every file as written
    while the vendored copy is in fact clean.
    """
    kgcov.export_tree(tmp_path)
    (tmp_path / "kgcov.c").chmod(0o600)
    result = kgcov.export_tree(tmp_path)
    assert result.changed == []
    assert (tmp_path / "kgcov.c").stat().st_mode & 0o777 == 0o600


def test_export_never_touches_the_local_override(tmp_path: Path):
    (tmp_path / kgcov.LOCAL_HEADER).write_text("#define KGCOV_ALLOC(s) my_alloc(s)\n")
    kgcov.export_tree(tmp_path)
    assert (tmp_path / kgcov.LOCAL_HEADER).read_text() == "#define KGCOV_ALLOC(s) my_alloc(s)\n"
    assert kgcov.LOCAL_HEADER not in kgcov.SHIPPED_FILES


def test_check_reports_current_for_a_fresh_export(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    result = kgcov.check_tree(tmp_path)
    assert (result.state, result.exit_code) == ("current", 0)
    assert result.exported_by == get_version()
    assert result.local_override is False


def test_check_ignores_the_version_header_and_the_local_override(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    (tmp_path / kgcov.VERSION_HEADER).write_text(kgcov.version_header("0.0.1"))
    (tmp_path / kgcov.LOCAL_HEADER).write_text("/* local */\n")
    result = kgcov.check_tree(tmp_path)
    assert result.state == "current"
    assert result.exported_by == "0.0.1"
    assert result.local_override is True


def test_check_names_a_differing_and_a_missing_file(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    (tmp_path / "consumer.mk").write_text("# edited\n")
    (tmp_path / "kgcov_gcc_abi.c").unlink()
    result = kgcov.check_tree(tmp_path)
    assert (result.state, result.exit_code) == ("differs", 1)
    assert result.differing == ["consumer.mk"]
    assert result.missing == ["kgcov_gcc_abi.c"]


def test_check_reports_absent_where_there_is_no_kgcov_h(tmp_path: Path):
    (tmp_path / "unrelated.txt").write_text("x")
    result = kgcov.check_tree(tmp_path)
    assert (result.state, result.exit_code) == ("absent", 2)
    assert kgcov.check_tree(tmp_path / "nowhere").state == "absent"


def test_the_demo_repos_vendored_copy_is_current():
    """The two in-tree copies of the library cannot drift apart."""
    result = kgcov.check_tree(REPO5_VENDORED)
    assert result.state == "current", (result.differing, result.missing)
    assert result.local_override is False
    assert (REPO5_VENDORED / "build.sh").stat().st_mode & 0o111, "build.sh lost its x bit"
