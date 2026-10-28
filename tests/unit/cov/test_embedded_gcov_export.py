"""``otto.coverage.embedded_gcov``: the shipped runtime patch and starter, exported and checked."""

import re
from pathlib import Path

from otto.coverage.embedded_gcov import library
from tests._fixtures.paths import CHECKOUT_ROOT

PACKAGE = CHECKOUT_ROOT / "src" / "otto" / "coverage" / "embedded_gcov"


def test_the_patch_and_the_starter_ship_in_the_package():
    for name in (*library.SHIPPED_FILES, *library.STARTER_FILES):
        assert (PACKAGE / name).is_file(), name
    assert "embedded-gcov-zephyr-gcc12plus.patch" in library.SHIPPED_FILES


def test_the_starter_includes_the_runtime_and_exports_the_three_entry_points():
    text = (PACKAGE / library.STARTER_FILES[0]).read_text()
    for runtime in ("gcov_public.c", "gcov_gcc.c", "gcov_printf.c"):
        assert f'#include "{runtime}"' in text
    for entry in ("cov_dump", "cov_reset", "cov_init"):
        assert re.search(rf"^void {entry}\(void\)", text, re.MULTILINE), entry
        assert f"LL_EXTENSION_SYMBOL({entry})" in text
    assert "// doc: begin entry-points" in text
    assert "// doc: end entry-points" in text
    assert "op_" not in text


def test_export_writes_the_patch_into_an_empty_directory(tmp_path: Path):
    changed = library.export_patch(tmp_path / "out")
    assert changed == list(library.SHIPPED_FILES)
    for name in library.SHIPPED_FILES:
        assert (tmp_path / "out" / name).read_bytes() == (PACKAGE / name).read_bytes()


def test_a_second_export_changes_nothing(tmp_path: Path):
    library.export_patch(tmp_path)
    assert library.export_patch(tmp_path) == []


def test_export_over_a_modified_patch_restores_it(tmp_path: Path):
    library.export_patch(tmp_path)
    name = library.SHIPPED_FILES[0]
    (tmp_path / name).write_text("edited\n")
    assert library.export_patch(tmp_path) == [name]
    assert (tmp_path / name).read_bytes() == (PACKAGE / name).read_bytes()


def test_check_is_current_on_an_exported_copy(tmp_path: Path):
    library.export_patch(tmp_path)
    result = library.check_patch(tmp_path)
    assert (result.state, result.exit_code) == ("current", 0)


def test_check_differs_on_a_modified_patch(tmp_path: Path):
    library.export_patch(tmp_path)
    name = library.SHIPPED_FILES[0]
    (tmp_path / name).write_text("edited\n")
    result = library.check_patch(tmp_path)
    assert (result.state, result.exit_code) == ("differs", 1)
    assert result.differing == [name]


def test_check_is_absent_where_there_is_no_patch(tmp_path: Path):
    for where in (tmp_path / "nowhere", tmp_path):
        result = library.check_patch(where)
        assert (result.state, result.exit_code) == ("absent", 2)
