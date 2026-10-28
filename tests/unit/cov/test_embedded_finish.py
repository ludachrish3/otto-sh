"""``otto.coverage.embedded_finish``: strip an extension, then check its gcov stamp."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from otto.coverage import embedded_finish, stamp_guard
from otto.coverage.embedded_gcov import library as shipped
from tests.unit.cov._gcno import stamp_image, stamp_notes

STAMP = 0x8372DF94


@pytest.fixture
def fake_objcopy(tmp_path: Path) -> Path:
    """An objcopy that records its arguments and copies the input ELF to the output."""
    script = tmp_path / "objcopy"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$@" > {tmp_path}/objcopy-args.txt
            cp "${{@: -2:1}}" "${{@: -1}}"
            """
        )
    )
    script.chmod(0o755)
    return script


def _notes_dir(tmp_path: Path, *notes: bytes) -> Path:
    directory = tmp_path / "notes" / "deep"
    directory.mkdir(parents=True)
    for n, data in enumerate(notes):
        (directory / f"unit{n}.c.gcno").write_bytes(data)
    return directory.parent


def _elf(tmp_path: Path, *stamps: int) -> Path:
    elf = tmp_path / "ext.llext"
    elf.write_bytes(stamp_image(*stamps))
    return elf


def _leftovers(directory: Path) -> list[str]:
    """Every file ``finish`` may have left in *directory* besides the test's own inputs."""
    return sorted(
        p.name for p in directory.iterdir() if p.name.startswith((".ext.stripped", "ext.stripped"))
    )


def test_strip_names_every_section_once_and_strips_debug(tmp_path, fake_objcopy):
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    embedded_finish.strip_extension(elf, out, fake_objcopy)
    args = (tmp_path / "objcopy-args.txt").read_text().split("\n")[:-1]
    assert args == [
        *(f"--remove-section={name}" for name in shipped.STRIPPED_SECTIONS),
        "--strip-debug",
        str(elf),
        str(out),
    ]
    assert out.read_bytes() == elf.read_bytes()


def test_a_missing_elf_is_named(tmp_path, fake_objcopy):
    with pytest.raises(embedded_finish.FinishError, match=r"nowhere\.llext"):
        embedded_finish.strip_extension(tmp_path / "nowhere.llext", tmp_path / "o", fake_objcopy)
    assert not (tmp_path / "objcopy-args.txt").exists(), "objcopy ran on a missing input"


def test_a_missing_objcopy_is_named(tmp_path):
    elf = tmp_path / "ext.llext"
    elf.write_bytes(b"x")
    with pytest.raises(embedded_finish.FinishError, match="no-objcopy"):
        embedded_finish.strip_extension(elf, tmp_path / "o", tmp_path / "no-objcopy")


def test_a_finish_error_is_a_stamp_error():
    """The CLI catches one family: a strip failure and a stamp failure both exit 1."""
    assert issubclass(embedded_finish.FinishError, stamp_guard.StampError)


def test_finish_strips_then_verifies_the_stripped_file(tmp_path, fake_objcopy):
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    notes = _notes_dir(tmp_path, stamp_notes(STAMP))
    assert embedded_finish.finish(elf, out, fake_objcopy, notes) == [STAMP]
    assert out.read_bytes() == elf.read_bytes()
    assert _leftovers(tmp_path) == ["ext.stripped.llext"]


def test_objcopy_writes_a_temporary_file_beside_the_destination_not_the_destination(
    tmp_path, fake_objcopy
):
    """The destination only ever appears verified: objcopy and the check see another name."""
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))
    written = Path((tmp_path / "objcopy-args.txt").read_text().split("\n")[-2])
    assert written.parent.parent == out.parent
    assert written != out
    assert not written.parent.exists(), "the staging directory outlived finish"


def test_the_published_extension_keeps_the_mode_objcopy_gave_it(tmp_path, fake_objcopy):
    """Staging must not narrow the file to the 0600 a temporary file is created with."""
    umask = os.umask(0)
    os.umask(umask)
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert out.stat().st_mode & 0o777 == 0o666 & ~umask


def test_a_stale_extension_leaves_no_output(tmp_path, fake_objcopy):
    elf = _elf(tmp_path, 0x11)
    out = tmp_path / "ext.stripped.llext"
    with pytest.raises(stamp_guard.StampError, match=r"ext\.stripped\.llext"):
        embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert _leftovers(tmp_path) == []


def test_a_failed_finish_also_removes_the_previous_builds_output(tmp_path, fake_objcopy):
    """The old extension is stale against the rebuilt notes: it must not survive to install."""
    elf = _elf(tmp_path, 0x11)
    out = tmp_path / "ext.stripped.llext"
    out.write_bytes(b"the previous build")
    with pytest.raises(stamp_guard.StampError):
        embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert _leftovers(tmp_path) == []


def test_an_unreadable_notes_file_fails_finish_naming_it_and_leaves_no_output(
    tmp_path, fake_objcopy
):
    """A dangling ``.gcno`` symlink is a stamp error naming the file, not a raw OSError."""
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    notes = _notes_dir(tmp_path, stamp_notes(STAMP))
    (notes / "deep" / "gone.c.gcno").symlink_to(tmp_path / "nowhere.gcno")
    with pytest.raises(stamp_guard.StampError, match=r"gone\.c\.gcno"):
        embedded_finish.finish(elf, out, fake_objcopy, notes)
    assert _leftovers(tmp_path) == []


def test_a_failed_strip_leaves_no_output(tmp_path):
    out = tmp_path / "ext.stripped.llext"
    out.write_bytes(b"the previous build")
    failing = tmp_path / "objcopy"
    failing.write_text("#!/bin/bash\necho 'bad section' >&2\nexit 1\n")
    failing.chmod(0o755)
    elf = _elf(tmp_path, STAMP)
    with pytest.raises(embedded_finish.FinishError, match="bad section"):
        embedded_finish.finish(elf, out, failing, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert _leftovers(tmp_path) == []


def test_a_destination_directory_that_does_not_exist_is_named(tmp_path, fake_objcopy):
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "missing" / "ext.stripped.llext"
    with pytest.raises(embedded_finish.FinishError, match=r"missing/ext\.stripped\.llext"):
        embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))


def test_a_destination_that_is_a_directory_is_named_and_left_alone(tmp_path, fake_objcopy):
    """A directory OUT is refused by name; cleanup never masks the message or touches it."""
    elf = _elf(tmp_path, STAMP)
    out = tmp_path / "ext.stripped.llext"
    out.mkdir()
    with pytest.raises(embedded_finish.FinishError, match=r"ext\.stripped\.llext: is a directory"):
        embedded_finish.finish(elf, out, fake_objcopy, _notes_dir(tmp_path, stamp_notes(STAMP)))
    assert out.is_dir()
    assert list(tmp_path.glob(".ext.stripped.llext.*")) == []


_CTOR_SOURCE = """\
static int hits;
__attribute__((constructor)) static void ctor(void) { hits++; }
int foo(int x) { return x ? 4 + hits : 9; }
"""


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
@pytest.mark.parametrize("objcopy", ["objcopy", "llvm-objcopy"])
def test_finish_with_a_real_objcopy_removes_the_constructor_tables(tmp_path, objcopy):
    """GNU objcopy and llvm-objcopy both take the strip flags, section wildcards included."""
    tool = shutil.which(objcopy)
    if tool is None:
        pytest.skip(f"{objcopy} not installed")
    (tmp_path / "ext.c").write_text(_CTOR_SOURCE)
    build = tmp_path / "build"
    build.mkdir()
    elf = build / "ext.o"
    flags = ["-g", "-fprofile-arcs", "-ftest-coverage", "-O0"]
    subprocess.run(["gcc", *flags, "-c", str(tmp_path / "ext.c"), "-o", str(elf)], check=True)
    out = tmp_path / "ext.stripped.llext"
    assert len(embedded_finish.finish(elf, out, Path(tool), build)) == 1
    sections = subprocess.run(
        ["readelf", "-S", "-W", str(out)], capture_output=True, text=True, check=True
    ).stdout
    assert "init_array" not in sections
    assert ".debug" not in sections
