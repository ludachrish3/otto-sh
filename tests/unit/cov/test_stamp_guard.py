"""``otto.coverage.stamp_guard``: a built binary must carry the stamp of every ``.gcno``."""

import shutil
import subprocess
from pathlib import Path

import pytest

from otto.coverage import stamp_guard
from otto.coverage.objects import elf_ident
from otto.host import gcovformat
from tests.unit.cov._gcno import STAMP_VERSION, stamp_image, stamp_notes

STAMP = 0x8372DF94


def _notes_dir(tmp_path: Path, *notes: bytes) -> Path:
    directory = tmp_path / "notes" / "deep"
    directory.mkdir(parents=True)
    for n, data in enumerate(notes):
        (directory / f"unit{n}.c.gcno").write_bytes(data)
    return directory.parent


def _binary(tmp_path: Path, image: bytes) -> Path:
    path = tmp_path / "ext.llext"
    path.write_bytes(image)
    return path


def test_the_info_stamps_found_in_an_image_are_those_after_each_version_word():
    found = gcovformat.info_stamps(
        stamp_image(1, STAMP, 7), STAMP_VERSION, pointer_bits=32, big_endian=False
    )
    assert found == [1, STAMP, 7]
    assert not gcovformat.info_stamps(
        b"no gcov data here", STAMP_VERSION, pointer_bits=32, big_endian=False
    )


def test_the_info_stamp_offset_follows_the_pointer_width():
    image = stamp_image(STAMP, bits=64)
    assert gcovformat.info_stamps(image, STAMP_VERSION, pointer_bits=64, big_endian=False) == [
        STAMP
    ]
    assert gcovformat.info_stamps(image, STAMP_VERSION, pointer_bits=32, big_endian=False) != [
        STAMP
    ]


def test_the_version_word_is_in_order_on_a_big_endian_target_and_reversed_on_a_little_one():
    assert gcovformat.version_word(STAMP_VERSION, big_endian=True) == b"B24*"
    assert gcovformat.version_word(STAMP_VERSION, big_endian=False) == b"*42B"


@pytest.mark.parametrize("bits", [32, 64])
@pytest.mark.parametrize("elf_big", [False, True], ids=["elf-le", "elf-be"])
@pytest.mark.parametrize("notes_big", [False, True], ids=["notes-le", "notes-be"])
def test_the_binary_is_read_in_its_own_byte_order_whatever_the_notes_order(
    tmp_path, bits, elf_big, notes_big
):
    """A cross compiler may write notes in the host's order and the binary in the target's.

    ``arm-zephyr-eabi-gcc -mbig-endian`` does: a big-endian ELF (EI_DATA=2) beside
    little-endian notes. Every pairing of the two orders, at both widths, verifies.
    """
    binary = _binary(tmp_path, stamp_image(0x11, STAMP, bits=bits, big_endian=elf_big))
    notes = _notes_dir(tmp_path, stamp_notes(STAMP, big_endian=notes_big))
    assert stamp_guard.verify_stamps(binary, notes) == [STAMP]


@pytest.mark.parametrize("elf_big", [False, True], ids=["elf-le", "elf-be"])
@pytest.mark.parametrize("notes_big", [False, True], ids=["notes-le", "notes-be"])
def test_a_stale_binary_is_refused_whatever_the_byte_orders(tmp_path, elf_big, notes_big):
    binary = _binary(tmp_path, stamp_image(0x11, big_endian=elf_big))
    notes = _notes_dir(tmp_path, stamp_notes(STAMP, big_endian=notes_big))
    with pytest.raises(stamp_guard.StampError, match=r"unit0\.c\.gcno.*0x8372df94.*ext\.llext"):
        stamp_guard.verify_stamps(binary, notes)


@pytest.mark.parametrize(
    ("ident_bytes", "expected"),
    [
        (b"\x01\x01", (False, False)),
        (b"\x02\x01", (True, False)),
        (b"\x01\x02", (False, True)),
        (b"\x02\x02", (True, True)),
        (b"\x00\x01", None),  # ELFCLASSNONE
        (b"\x01\x00", None),  # ELFDATANONE
        (b"\x03\x01", None),
    ],
)
def test_the_elf_identification_names_class_and_order_or_nothing(ident_bytes, expected):
    ident = elf_ident(b"\x7fELF" + ident_bytes + bytes(58))
    assert (None if ident is None else (ident.is64, ident.big_endian)) == expected


def test_a_matching_stamp_verifies(tmp_path):
    binary = _binary(tmp_path, stamp_image(0x11, STAMP))
    assert stamp_guard.verify_stamps(binary, _notes_dir(tmp_path, stamp_notes(STAMP))) == [STAMP]


def test_a_binary_with_no_gcov_info_says_so_not_stale(tmp_path):
    binary = _binary(tmp_path, stamp_image())
    with pytest.raises(
        stamp_guard.StampError, match=r"ext\.llext: no gcov_info.*instrumented"
    ) as e:
        stamp_guard.verify_stamps(binary, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert "stale" not in str(e.value)


def test_a_file_that_is_no_elf_is_named(tmp_path):
    binary = _binary(tmp_path, b"not an elf image")
    with pytest.raises(stamp_guard.StampError, match=r"ext\.llext.*not an ELF"):
        stamp_guard.verify_stamps(binary, _notes_dir(tmp_path, stamp_notes(STAMP)))


def test_a_gcno_directory_that_does_not_exist_is_not_a_missing_gcno(tmp_path):
    binary = _binary(tmp_path, stamp_image(STAMP))
    with pytest.raises(stamp_guard.StampError, match=r"typo.*not a directory"):
        stamp_guard.verify_stamps(binary, tmp_path / "typo")


def test_every_gcno_under_the_directory_must_match(tmp_path):
    binary = _binary(tmp_path, stamp_image(STAMP))
    notes = _notes_dir(tmp_path, stamp_notes(STAMP), stamp_notes(0x99))
    with pytest.raises(stamp_guard.StampError, match=r"unit1\.c\.gcno"):
        stamp_guard.verify_stamps(binary, notes)


def test_no_gcno_is_an_error_naming_the_directory(tmp_path):
    binary = _binary(tmp_path, stamp_image(STAMP))
    (tmp_path / "empty").mkdir()
    with pytest.raises(stamp_guard.StampError, match="empty"):
        stamp_guard.verify_stamps(binary, tmp_path / "empty")


def test_a_file_that_is_no_notes_file_is_named(tmp_path):
    binary = _binary(tmp_path, stamp_image(STAMP))
    notes = _notes_dir(tmp_path, b"not gcov at all, but long enough")
    with pytest.raises(stamp_guard.StampError, match=r"unit0\.c\.gcno"):
        stamp_guard.verify_stamps(binary, notes)


def test_an_unreadable_notes_file_is_a_stamp_error_naming_it(tmp_path):
    """A dangling ``.gcno`` symlink (or one removed mid-walk) is named, not an OSError."""
    binary = _binary(tmp_path, stamp_image(STAMP))
    notes = _notes_dir(tmp_path, stamp_notes(STAMP))
    (notes / "deep" / "gone.c.gcno").symlink_to(tmp_path / "nowhere.gcno")
    with pytest.raises(stamp_guard.StampError, match=r"gone\.c\.gcno: cannot read the notes file"):
        stamp_guard.verify_stamps(binary, notes)


def test_a_missing_binary_is_named(tmp_path):
    notes = _notes_dir(tmp_path, stamp_notes(STAMP))
    with pytest.raises(stamp_guard.StampError, match=r"gone\.llext"):
        stamp_guard.verify_stamps(tmp_path / "gone.llext", notes)


def test_messages_name_the_binary_as_shown(tmp_path):
    binary = _binary(tmp_path, stamp_image(0x11))
    notes = _notes_dir(tmp_path, stamp_notes(STAMP))
    with pytest.raises(stamp_guard.StampError, match=r"is not in .*dest\.llext") as e:
        stamp_guard.verify_stamps(binary, notes, shown_as=tmp_path / "dest.llext")
    assert "ext.llext" not in str(e.value).replace("dest.llext", "")


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_a_real_gcc_binary_verifies_and_a_recompile_makes_it_stale(tmp_path):
    """A native build's own notes pass; recompiling the source (a fresh stamp) fails it."""
    source = tmp_path / "app.c"
    source.write_text("int main(void) { return 0; }\n")
    build = tmp_path / "build"
    build.mkdir()
    subprocess.run(["gcc", "--coverage", "-c", str(source), "-o", str(build / "app.o")], check=True)
    subprocess.run(
        ["gcc", "--coverage", str(build / "app.o"), "-o", str(tmp_path / "app")], check=True
    )
    assert len(stamp_guard.verify_stamps(tmp_path / "app", build)) == 1
    subprocess.run(["gcc", "--coverage", "-c", str(source), "-o", str(build / "app.o")], check=True)
    with pytest.raises(stamp_guard.StampError, match=r"app\.gcno.*stale"):
        stamp_guard.verify_stamps(tmp_path / "app", build)
