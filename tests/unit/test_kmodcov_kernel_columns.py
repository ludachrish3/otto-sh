"""The kernel-column table behind `make kmodcov`'s build columns, and its pure helpers.

Everything here runs on a machine with neither docker nor a kernel tree: the
table is data, the environment and error text are functions of it, and the
ELF helpers work on a hand-built object.
"""

import re
import struct
from pathlib import Path

import pytest

from tests.e2e.cov._repo5_build import (
    CROSS_KERNEL,
    CTORS_SECTION,
    DEFAULT_KERNELS_DIR,
    GCOV_CONSTRUCTOR,
    ISA_MACHINE,
    ISA_OBJCOPY,
    KERNEL_IDS,
    SHT_INIT_ARRAY,
    SHT_PROGBITS,
    KernelColumn,
    assert_bracketed,
    column_env,
    column_prerequisite_error,
    compiler_version_from_banner,
    constructor_section,
    elf_sections,
    kernel_column,
    kernel_columns,
    set_section_type,
    snapshot_tree,
    tree_mount,
    tree_prepared,
    warnings_under,
)

pytestmark = pytest.mark.interpreter_agnostic

KERNELS = DEFAULT_KERNELS_DIR
CROSS = Path("/srv/linux-6.8")


def _column(kernel_id: str) -> KernelColumn:
    return kernel_column(kernel_id, kernels_dir=KERNELS, cross_kdir=CROSS)


def test_the_table_is_the_makefile_order_and_the_cross_build_leads():
    assert [c.id for c in kernel_columns(KERNELS, CROSS)] == KERNEL_IDS
    assert KERNEL_IDS[0] == CROSS_KERNEL


def test_an_unknown_id_is_refused_naming_the_set():
    with pytest.raises(ValueError, match=r"5\.10.*x86_64-cross.*6\.17"):
        _column("5.10")


def test_the_cross_column_is_a_host_build_of_the_6_8_tree():
    c = _column(CROSS_KERNEL)
    assert c.image is None
    assert c.platform is None
    assert c.tree == CROSS
    assert c.isa == "x86_64"
    assert c.cc is None
    assert c.env == {"ARCH": "x86_64", "CROSS_COMPILE": "x86_64-linux-gnu-"}
    assert c.modpost_may_warn is True
    assert "defconfig modules_prepare" in c.provision
    assert c.stamp is None


def test_the_2_6_32_column_is_emulated_amd64_with_gcc_4_7_for_host_tools_too():
    c = _column("2.6.32")
    assert (c.image, c.platform, c.isa) == ("otto-kmodcov-kernel:2.6.32", "linux/amd64", "x86_64")
    assert c.tree == KERNELS / "2.6.32" / "src" / "linux-2.6.32.71"
    assert c.cc == "gcc-4.7"
    assert c.kmakeflags == "HOSTCC=gcc-4.7"
    assert c.modpost_may_warn is True
    assert c.provision == "scripts/provision_kmodcov_kernels.sh 2.6.32"
    assert c.stamp == KERNELS / "2.6.32" / ".prepared-linux-2.6.32.71"


@pytest.mark.parametrize("kernel_id", ["3.13", "4.4", "5.4", "5.15", "6.17"])
def test_the_headers_columns_are_arm64_images_with_strict_modpost(kernel_id):
    c = _column(kernel_id)
    assert (c.image, c.platform, c.isa) == (
        f"otto-kmodcov-kernel:{kernel_id}",
        "linux/arm64",
        "arm64",
    )
    assert c.tree.parent == KERNELS / kernel_id / "root" / "usr" / "src"
    assert c.tree.name.startswith(f"linux-headers-{kernel_id}.")
    assert c.tree.name.endswith("-generic")
    assert c.cc is None
    assert c.kmakeflags == ""
    assert c.env == {}
    assert c.modpost_may_warn is False
    assert c.provision == f"scripts/provision_kmodcov_kernels.sh {kernel_id}"
    assert c.stamp == KERNELS / kernel_id / f".prepared-{c.tree.name}"


def test_a_non_default_kernels_dir_names_itself_in_the_provision_remedy():
    non_default = Path("/srv/kernels")
    oldest = kernel_column("2.6.32", kernels_dir=non_default, cross_kdir=CROSS)
    assert oldest.provision == (
        f"OTTO_KMODCOV_KERNELS_DIR={non_default} scripts/provision_kmodcov_kernels.sh 2.6.32"
    )
    headers = kernel_column("3.13", kernels_dir=non_default, cross_kdir=CROSS)
    assert headers.provision == (
        f"OTTO_KMODCOV_KERNELS_DIR={non_default} scripts/provision_kmodcov_kernels.sh 3.13"
    )
    # The cross column's own recipe never goes through the script, so it is unaffected.
    cross = kernel_column(CROSS_KERNEL, kernels_dir=non_default, cross_kdir=CROSS)
    assert "OTTO_KMODCOV_KERNELS_DIR" not in cross.provision


def test_tree_mount_covers_the_headers_symlink_farms_usr_src():
    assert tree_mount(_column("3.13")) == KERNELS / "3.13" / "root" / "usr" / "src"
    assert tree_mount(_column("6.17")) == KERNELS / "6.17" / "root" / "usr" / "src"


def test_tree_mount_the_2_6_32_column_covers_its_own_src_directory():
    assert tree_mount(_column("2.6.32")) == KERNELS / "2.6.32" / "src"


def test_column_env_is_what_build_sh_and_the_demo_makefile_take():
    assert column_env(_column(CROSS_KERNEL)) == {
        "KDIR": str(CROSS),
        "ARCH": "x86_64",
        "CROSS_COMPILE": "x86_64-linux-gnu-",
        "KMAKEFLAGS": "KBUILD_MODPOST_WARN=1",
    }
    assert column_env(_column("2.6.32")) == {
        "KDIR": str(KERNELS / "2.6.32" / "src" / "linux-2.6.32.71"),
        "CC": "gcc-4.7",
        "KMAKEFLAGS": "HOSTCC=gcc-4.7 KBUILD_MODPOST_WARN=1",
    }
    assert column_env(_column("5.15")) == {"KDIR": str(_column("5.15").tree)}


def test_isa_tables_cover_every_column():
    for c in kernel_columns(KERNELS, CROSS):
        assert c.isa in ISA_MACHINE
        assert c.isa in ISA_OBJCOPY
    assert ISA_MACHINE == {"x86_64": "Advanced Micro Devices X86-64", "arm64": "AArch64"}
    assert ISA_OBJCOPY == {"x86_64": "x86_64-linux-gnu-objcopy", "arm64": "objcopy"}


def _ok(column, **overrides):
    kw = {
        "tree_prepared": True,
        "image_present": True,
        "docker_present": True,
        "compiler_present": True,
        "objcopy_present": True,
    }
    kw.update(overrides)
    return column_prerequisite_error(column, **kw)


def test_a_complete_column_has_no_prerequisite_error():
    assert _ok(_column("3.13")) is None
    assert _ok(_column(CROSS_KERNEL)) is None


def test_an_x86_64_column_needs_the_cross_objcopy_for_the_ctors_row():
    for column in (_column(CROSS_KERNEL), _column("2.6.32")):
        error = _ok(column, objcopy_present=False)
        assert error is not None, column.id
        assert "x86_64-linux-gnu-objcopy" in error, column.id
        assert "binutils-x86-64-linux-gnu" in error, column.id


def test_an_arm64_column_needs_no_cross_objcopy():
    assert _ok(_column("3.13"), objcopy_present=False) is None


def test_a_missing_tree_or_image_names_the_provisioning_command():
    c = _column("3.13")
    assert "scripts/provision_kmodcov_kernels.sh 3.13" in _ok(c, tree_prepared=False)
    assert "scripts/provision_kmodcov_kernels.sh 3.13" in _ok(c, image_present=False)
    assert "otto-kmodcov-kernel:3.13" in _ok(c, image_present=False)


def test_missing_docker_names_its_package_and_the_host_column_never_needs_it():
    assert "docker" in _ok(_column("4.4"), docker_present=False)
    assert "docker.io" in _ok(_column("4.4"), docker_present=False)
    assert _ok(_column(CROSS_KERNEL), docker_present=False) is None


def test_the_host_column_needs_its_cross_compiler_and_image_columns_do_not():
    assert "x86_64-linux-gnu-gcc" in _ok(_column(CROSS_KERNEL), compiler_present=False)
    assert _ok(_column("5.4"), compiler_present=False) is None


def test_a_missing_cross_compiler_is_reported_before_a_missing_objcopy():
    error = _ok(_column(CROSS_KERNEL), compiler_present=False, objcopy_present=False)
    assert "x86_64-linux-gnu-gcc" in error
    assert "objcopy" not in error


# The six provisioned images' own `<cc> --version` first line, gathered
# 2026-09-29 via `docker run --rm --platform <p> otto-kmodcov-kernel:<id> <cc>
# --version | head -1`. 4.4's Ubuntu 16.04 gcc is the one that appends a
# build DATE straight after its own version with no separating punctuation.
_REAL_BANNERS = {
    "2.6.32": ("gcc-4.7 (Ubuntu/Linaro 4.7.3-2ubuntu1~12.04) 4.7.3", "4.7.3"),
    "3.13": ("gcc (Ubuntu/Linaro 4.8.4-2ubuntu1~14.04.4) 4.8.4", "4.8.4"),
    "4.4": ("gcc (Ubuntu/Linaro 5.4.0-6ubuntu1~16.04.12) 5.4.0 20160609", "5.4.0"),
    "5.4": ("gcc (Ubuntu 9.4.0-1ubuntu1~20.04.2) 9.4.0", "9.4.0"),
    "5.15": ("gcc (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0", "11.4.0"),
    "6.17": ("gcc (Ubuntu 15.2.0-4ubuntu4) 15.2.0", "15.2.0"),
}


@pytest.mark.parametrize("kernel_id", list(_REAL_BANNERS))
def test_compiler_version_from_banner_reads_the_six_images_own_banners(kernel_id):
    first_line, version = _REAL_BANNERS[kernel_id]
    assert compiler_version_from_banner(first_line) == version


def test_compiler_version_from_banner_the_4_4_date_never_wins():
    # The regression this guards: a naive "last token of the line" read would
    # take "20160609" -- all-digits, so it looks as plausible as a version.
    first_line, _version = _REAL_BANNERS["4.4"]
    assert compiler_version_from_banner(first_line) != "20160609"


def test_compiler_version_from_banner_refuses_a_banner_with_no_parenthesis():
    with pytest.raises(AssertionError, match=re.escape("gcc version 9.4.0")):
        compiler_version_from_banner("gcc version 9.4.0")


def _make_tree(tmp_path: Path, name: str, *, release: bool = True, modpost: bool = True) -> Path:
    """A fake kernel tree under *tmp_path* with the two files a module build reads first."""
    tree = tmp_path / name
    if release:
        (tree / "include" / "config").mkdir(parents=True)
        (tree / "include" / "config" / "kernel.release").write_text("5.4.0-218-generic\n")
    if modpost:
        (tree / "scripts" / "mod").mkdir(parents=True, exist_ok=True)
        (tree / "scripts" / "mod" / "modpost").write_text("")
    return tree


def _fake_column(
    tree: Path, stamp: Path | None, *, image: str | None = "otto-kmodcov-kernel:5.4"
) -> KernelColumn:
    return KernelColumn(
        id="5.4",
        isa="arm64",
        tree=tree,
        stamp=stamp,
        cc=None,
        image=image,
        platform="linux/arm64" if image else None,
        env={},
        kmakeflags="",
        modpost_may_warn=False,
        provision="scripts/provision_kmodcov_kernels.sh 5.4",
    )


def test_tree_prepared_is_true_with_a_nonempty_stamp_and_both_build_files(tmp_path: Path):
    tree = _make_tree(tmp_path, "tree")
    stamp = tmp_path / ".prepared-tree"
    stamp.write_text("5.4.0-218-generic deadbeef cafef00d\n")
    assert tree_prepared(_fake_column(tree, stamp)) is True


def test_tree_prepared_is_false_when_the_stamp_is_missing(tmp_path: Path):
    tree = _make_tree(tmp_path, "tree")
    assert tree_prepared(_fake_column(tree, tmp_path / ".prepared-tree")) is False


def test_tree_prepared_is_false_when_the_stamp_is_empty(tmp_path: Path):
    tree = _make_tree(tmp_path, "tree")
    stamp = tmp_path / ".prepared-tree"
    stamp.write_text("")
    assert tree_prepared(_fake_column(tree, stamp)) is False


def test_tree_prepared_is_false_when_modpost_is_missing_despite_the_stamp(tmp_path: Path):
    tree = _make_tree(tmp_path, "tree", modpost=False)
    stamp = tmp_path / ".prepared-tree"
    stamp.write_text("5.4.0-218-generic deadbeef cafef00d\n")
    assert tree_prepared(_fake_column(tree, stamp)) is False


def test_tree_prepared_is_false_when_kernel_release_is_missing(tmp_path: Path):
    tree = _make_tree(tmp_path, "tree", release=False)
    stamp = tmp_path / ".prepared-tree"
    stamp.write_text("5.4.0-218-generic deadbeef cafef00d\n")
    assert tree_prepared(_fake_column(tree, stamp)) is False


def test_tree_prepared_the_cross_column_needs_no_stamp(tmp_path: Path):
    tree = _make_tree(tmp_path, "cross")
    assert tree_prepared(_fake_column(tree, None, image=None)) is True


def test_tree_prepared_the_cross_column_still_needs_modpost(tmp_path: Path):
    tree = _make_tree(tmp_path, "cross", modpost=False)
    assert tree_prepared(_fake_column(tree, None, image=None)) is False


def test_snapshot_tree_is_stable_across_two_reads_of_an_untouched_tree(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "top.c").write_text("int top;\n")
    (tmp_path / "sub" / ".hidden.h").write_text("#define X 1\n")
    before = snapshot_tree(tmp_path)
    after = snapshot_tree(tmp_path)
    assert before == after
    assert {p for p, _size, _mtime in before} == {"top.c", "sub/.hidden.h"}


def test_snapshot_tree_moves_when_a_file_is_touched(tmp_path: Path):
    target = tmp_path / "top.c"
    target.write_text("int top;\n")
    before = snapshot_tree(tmp_path)
    target.write_text("int top; /* changed, so the size moves too */\n")
    after = snapshot_tree(tmp_path)
    assert before != after


def test_warnings_under_counts_the_library_directory_only(tmp_path: Path):
    lib = tmp_path / "build" / "lib"
    lib.mkdir(parents=True)
    (lib / "kmodcov.c").write_text("")
    (tmp_path / "tree" / "include" / "linux").mkdir(parents=True)
    (tmp_path / "tree" / "include" / "linux" / "fs.h").write_text("")
    output = "\n".join(
        [
            f"{lib}/kmodcov.c:12:3: warning: unused variable 'x' [-Wunused-variable]",
            f"{lib}/kmodcov_compat.h:40: warning: something old-style",
            f"{tmp_path}/tree/include/linux/fs.h:100:1: warning: header noise",
            "./include/linux/fs.h:100:1: warning: relative header noise",
            "kmodcov.c:12:3: warning: relative to the library directory",
            "demo_main.c:5:1: warning: a demo warning that lands nowhere under lib",
            f"  CC [M]  {lib}/kmodcov.o",
            f"{lib}/kmodcov.c:20:5: error: not a warning line",
        ]
    )
    hits = warnings_under(output, lib)
    assert [h.split(" warning:")[0] for h in hits] == [
        f"{lib}/kmodcov.c:12:3:",
        f"{lib}/kmodcov_compat.h:40:",
        "kmodcov.c:12:3:",
    ]


def test_the_constructor_regex_knows_the_three_names():
    for name in (
        "_sub_I_00100_0",
        "_GLOBAL__sub_I_65535_0_demo_parse",
        "_GLOBAL__sub_I_65535_0_demo_main.c",
        "__llvm_gcov_init",
    ):
        assert GCOV_CONSTRUCTOR.match(name), name
    for name in (
        "__kmodcov_begin_marker",
        "_sub_I_00100_1",
        "_GLOBAL__sub_D_65535_0_x",
        "_GLOBAL__sub_D_65535_0_demo_main.c",
        "main",
    ):
        assert not GCOV_CONSTRUCTOR.match(name), name


def test_assert_bracketed_wants_markers_around_exactly_the_units():
    assert_bracketed(["__kmodcov_begin_marker", *["_sub_I_00100_0"] * 3, "__kmodcov_end_marker"], 3)
    assert_bracketed(
        [
            "__kmodcov_begin_marker",
            "_GLOBAL__sub_I_65535_0_a",
            "_GLOBAL__sub_I_65535_0_b",
            "__kmodcov_end_marker",
        ],
        2,
    )
    with pytest.raises(AssertionError):
        assert_bracketed(["__kmodcov_begin_marker", "_sub_I_00100_0", "__kmodcov_end_marker"], 3)
    with pytest.raises(AssertionError):
        assert_bracketed(["_sub_I_00100_0", "_sub_I_00100_0", "__kmodcov_end_marker"], 1)
    with pytest.raises(AssertionError):
        assert_bracketed(
            ["__kmodcov_begin_marker", "__kmodcov_begin_marker", "__kmodcov_end_marker"], 1
        )


def _elf64(sections: dict[str, int]) -> bytes:
    """A minimal relocatable ELF64 (aarch64) with the named sections and a .shstrtab."""
    names = b"\0" + b"".join(n.encode() + b"\0" for n in sections) + b".shstrtab\0"
    shstr_off = 64
    shoff = shstr_off + len(names)
    shoff += (-shoff) % 8
    count = len(sections) + 2
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
    ehdr = ident + struct.pack(
        "<HHIQQQIHHHHHH", 1, 183, 1, 0, 0, shoff, 0, 64, 0, 0, 64, count, count - 1
    )
    body = bytearray(ehdr.ljust(shoff, b"\0"))
    body[shstr_off : shstr_off + len(names)] = names
    headers = [struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    name_off = 1
    for name, sh_type in sections.items():
        headers.append(struct.pack("<IIQQQQIIQQ", name_off, sh_type, 3, 0, 0, 0, 0, 0, 8, 0))
        name_off += len(name) + 1
    headers.append(struct.pack("<IIQQQQIIQQ", name_off, 3, 0, 0, shstr_off, len(names), 0, 0, 1, 0))
    return bytes(body) + b"".join(headers)


def test_elf_sections_reads_names_and_types(tmp_path: Path):
    obj = tmp_path / "a.o"
    obj.write_bytes(_elf64({".text": SHT_PROGBITS, ".init_array.00100": SHT_INIT_ARRAY}))
    assert [(s.name, s.sh_type) for s in elf_sections(obj)] == [
        ("", 0),
        (".text", SHT_PROGBITS),
        (".init_array.00100", SHT_INIT_ARRAY),
        (".shstrtab", 3),
    ]


def test_set_section_type_rewrites_one_header_in_place(tmp_path: Path):
    obj = tmp_path / "a.o"
    obj.write_bytes(_elf64({".text": SHT_PROGBITS, CTORS_SECTION: SHT_INIT_ARRAY}))
    set_section_type(obj, CTORS_SECTION, SHT_PROGBITS)
    assert {s.name: s.sh_type for s in elf_sections(obj)}[CTORS_SECTION] == SHT_PROGBITS
    assert {s.name: s.sh_type for s in elf_sections(obj)}[".text"] == SHT_PROGBITS
    with pytest.raises(AssertionError, match=r"no section named \.absent"):
        set_section_type(obj, ".absent", SHT_PROGBITS)


@pytest.mark.parametrize("section", [".init_array", ".init_array.00100", ".init_array.0"])
def test_constructor_section_finds_the_one_init_array_section(tmp_path: Path, section):
    obj = tmp_path / "a.o"
    obj.write_bytes(_elf64({".text": SHT_PROGBITS, section: SHT_INIT_ARRAY, ".rela" + section: 4}))
    assert constructor_section(obj) == section


def test_constructor_section_refuses_none_or_two(tmp_path: Path):
    obj = tmp_path / "a.o"
    obj.write_bytes(_elf64({".text": SHT_PROGBITS}))
    with pytest.raises(AssertionError, match=r"no \.init_array"):
        constructor_section(obj)
    obj.write_bytes(_elf64({".init_array": SHT_INIT_ARRAY, ".init_array.00100": SHT_INIT_ARRAY}))
    with pytest.raises(AssertionError, match="two"):
        constructor_section(obj)
