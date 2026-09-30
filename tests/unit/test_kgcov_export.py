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
    assert on_disk == {*kgcov.SHIPPED_FILES, kgcov.VERSION_HEADER, "__init__.py", "library.py"}, (
        "the wheel ships every file under src/otto/: a build product or a stray file here "
        "would ship too"
    )


def test_the_in_tree_version_header_says_source():
    assert (PACKAGE / kgcov.VERSION_HEADER).read_text() == kgcov.version_header("source")


def test_the_module_version_is_built_from_the_version_header_and_the_interface():
    source = (PACKAGE / "kgcov.c").read_text()
    assert '#include "kgcov_version.h"' in source
    assert 'MODULE_VERSION(KGCOV_OTTO_VERSION "+kgcov" KGCOV_STR(KGCOV_INTERFACE));' in source


OVERRIDE_NAMES = (
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
    "KGCOV_MKDIR",
    "KGCOV_FILE_WRITE",
    "KGCOV_FOPS_OPEN",
    "KGCOV_LLSEEK",
    "KGCOV_WITHIN_MODULE",
)


def test_the_override_header_is_force_included_only_when_present():
    kbuild = (PACKAGE / "Kbuild").read_text()
    assert (
        "ccflags-y += $(if $(wildcard $(src)/kgcov_local.h),-include $(src)/kgcov_local.h)"
        in kbuild
    )
    compat = (PACKAGE / "kgcov_compat.h").read_text()
    for macro in OVERRIDE_NAMES:
        # The default may sit behind one version test, never more: an override
        # defined first must skip the whole block.
        pattern = rf"^#ifndef {macro}\n(?:#if .*\n)?#define {macro}\b"
        assert re.search(pattern, compat, re.MULTILINE), macro
    gcov_h = (PACKAGE / "kgcov_gcov.h").read_text()
    assert '#include "kgcov_compat.h"' in gcov_h
    assert "#ifndef KGCOV_" not in gcov_h, "the override surface has one home: kgcov_compat.h"


BARE_KERNEL_CALLS = (
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
    "within_module(",
    "kernel_write(",
    "kern_path_create(",
    "done_path_create(",
    "vfs_mkdir(",
    "simple_open",
    "noop_llseek",
    "mnt_idmap(",
    "d_inode(",
)


@pytest.mark.parametrize("name", ["kgcov.c", "kgcov_gcc.c", "kgcov_gcc_abi.c", "kgcov_clang.c"])
def test_the_library_calls_no_kernel_helper_the_ladder_owns_directly(name):
    source = (PACKAGE / name).read_text()
    for bare in BARE_KERNEL_CALLS:
        # A no-preceding-word-char anchor, not a bare substring: a vendored
        # identifier like gcov_info_within_module() must not trip on the
        # "within_module(" it contains as a tail. simple_open/noop_llseek
        # have no "(" of their own, so they need a trailing \b too.
        pattern = rf"(?<!\w){re.escape(bare)}"
        if bare in ("simple_open", "noop_llseek"):
            pattern += r"\b"
        assert not re.search(pattern, source), f"{name} calls {bare} directly; use the KGCOV_ macro"


def test_every_error_directive_in_the_ladder_sits_inside_an_override_guard():
    """An #error, if one ever appears in the ladder, must sit inside its override guard.

    Walks the preprocessor nesting: at each `#error`, the guard that must
    silence it is the INNERMOST `#ifndef KGCOV_` on the stack — any other
    nested `#ifndef`/`#ifdef`/`#if` (not a `KGCOV_` name) in between is
    skipped when picking it — and the `#error` message must name that
    guard's macro and kgcov_local.h.
    """
    stack: list[str] = []
    for line in (PACKAGE / "kgcov_compat.h").read_text().splitlines():
        s = line.strip()
        if s.startswith(("#if ", "#ifdef ", "#ifndef ")):
            stack.append(s)
        elif s.startswith("#endif"):
            assert stack, f"{s!r} has no matching #if/#ifdef/#ifndef"
            stack.pop()
        elif s.startswith("#error"):
            guards = [g for g in stack if g.startswith("#ifndef KGCOV_")]
            assert guards, f"{s!r} is outside every #ifndef KGCOV_ block"
            name = guards[-1].split()[1]
            assert name in s, s
            assert "kgcov_local.h" in s, s
    assert stack == [], "unbalanced conditionals"


def _kgcov_mkdir_block(header_path: Path) -> list[str]:
    """The lines of `kgcov_compat.h`'s `#ifndef KGCOV_MKDIR` block, both `#endif`s included."""
    lines = header_path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "#ifndef KGCOV_MKDIR")
    depth = 0
    for i in range(start, len(lines)):
        s = lines[i].strip()
        if s.startswith(("#if ", "#ifdef ", "#ifndef ")):
            depth += 1
        elif s.startswith("#endif"):
            depth -= 1
            if depth == 0:
                return lines[start : i + 1]
    raise AssertionError("#ifndef KGCOV_MKDIR has no matching #endif")


def _assert_untested_comment_follows(block: list[str], marker_index: int, marker: str) -> None:
    # The comment beside an UNTESTED arm runs a handful of lines (the arm's own
    # prose varies in length); a generous lookahead avoids coupling this test to
    # exact wraps while still requiring the comment to sit right beside the code.
    nearby = "\n".join(block[marker_index + 1 : marker_index + 10])
    assert "UNTESTED" in nearby, f"no UNTESTED comment beside {marker!r}"
    assert "headers column" in nearby, f"no 'headers column' beside {marker!r}"
    assert "kgcov_local.h" in nearby, f"no 'kgcov_local.h' override beside {marker!r}"


def _assert_kgcov_mkdir_untested_arms_are_documented(header_path: Path | None = None) -> None:
    """Each `KGCOV_MKDIR` arm no kernel in `make kgcov`'s set has run says so beside its code.

    Inside the `#ifndef KGCOV_MKDIR` block, the `#else` after the 2.6.39 check
    opens the 2.6.39-to-3.0 arm and the `< KERNEL_VERSION(3, 6, 0)` guard opens
    the 3.1-to-3.5 arm; each is unproven by any kernel in the set, so the
    comment beside it must say UNTESTED, name the headers column that would
    prove it, and name the kgcov_local.h override that replaces it outright.
    No `#error` may stand in for that comment anywhere in the block.

    Takes the header's path so a scratch copy can prove this red; defaults to
    the package's own `kgcov_compat.h` for the real, green check below.
    """
    block = _kgcov_mkdir_block(header_path or PACKAGE / "kgcov_compat.h")

    assert not any(line.strip().startswith("#error") for line in block), (
        "an #error sits inside the KGCOV_MKDIR block; the window ships as untested arms now"
    )

    below_239 = next(i for i, line in enumerate(block) if "KERNEL_VERSION(2, 6, 39)" in line)
    else_239 = next(i for i in range(below_239 + 1, len(block)) if block[i].strip() == "#else")
    _assert_untested_comment_follows(block, else_239, "the #else after the 2.6.39 check")

    below_360 = next(
        i
        for i, line in enumerate(block)
        if line.strip() == "#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 6, 0)"
    )
    _assert_untested_comment_follows(
        block, below_360, "#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 6, 0)"
    )


def test_every_untested_arm_says_so_beside_its_code():
    _assert_kgcov_mkdir_untested_arms_are_documented()


def test_the_consumer_fragment_applies_the_linker_script_and_build_sh_ships_it():
    assert "ldflags-y += -T $(KGCOV)/kgcov.lds" in (PACKAGE / "consumer.mk").read_text()
    lds = (PACKAGE / "kgcov.lds").read_text()
    assert (
        ".init_array : { *(SORT(.init_array.*)) *(SORT(.ctors.*)) *(.ctors) *(.init_array) }" in lds
    )
    build_sh_lines = (PACKAGE / "build.sh").read_text().splitlines()
    copy_line = next(line for line in build_sh_lines if line.startswith('cp "$SRC_DIR"/{'))
    for name in kgcov.SHIPPED_FILES:
        if name not in ("build.sh", "README.md"):
            assert name in copy_line, f"build.sh does not copy {name}"


def test_check_names_the_two_files_an_older_vendored_copy_lacks(tmp_path: Path):
    kgcov.export_tree(tmp_path)
    (tmp_path / "kgcov_compat.h").unlink()
    (tmp_path / "kgcov.lds").unlink()
    result = kgcov.check_tree(tmp_path)
    assert (result.state, result.exit_code) == ("differs", 1)
    assert sorted(result.missing) == ["kgcov.lds", "kgcov_compat.h"]
    assert result.differing == []


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
