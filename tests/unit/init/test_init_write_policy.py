"""One write policy: otto-owned files refresh, user-owned files are created once, never edited."""

import os
import stat
import subprocess
import sys
from pathlib import Path

from otto.init.write_policy import FileWrite, write_file


def test_a_user_file_is_created_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "a" / "conftest.py"
    assert write_file(target, "x", "user") == FileWrite(target, "created")
    assert target.read_text() == "x"


def test_an_existing_user_file_is_kept_byte_identical(tmp_path: Path) -> None:
    target = tmp_path / "conftest.py"
    target.write_bytes(b"mine\r\n")
    assert write_file(target, "otto's", "user").outcome == "kept"
    assert target.read_bytes() == b"mine\r\n"


def test_an_otto_file_is_refreshed(tmp_path: Path) -> None:
    target = tmp_path / "otto.code-snippets"
    target.write_text("old")
    assert write_file(target, "new", "otto").outcome == "refreshed"
    assert target.read_text() == "new"


def test_a_mode_is_applied_at_creation(tmp_path: Path) -> None:
    target = tmp_path / "creds.json"
    write_file(target, "{}", "user", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_user_symlink_is_kept_and_its_target_never_written(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("theirs")
    live = tmp_path / "live.py"
    live.symlink_to(elsewhere)
    dangling = tmp_path / "dangling.py"
    dangling.symlink_to(tmp_path / "nowhere.py")
    assert write_file(live, "otto's", "user").outcome == "kept"
    assert write_file(dangling, "otto's", "user").outcome == "kept"
    assert elsewhere.read_text() == "theirs"
    assert not (tmp_path / "nowhere.py").exists()


def test_an_absent_otto_file_is_created(tmp_path: Path) -> None:
    target = tmp_path / "otto.code-snippets"
    assert write_file(target, "new", "otto") == FileWrite(target, "created")


def test_text_is_written_as_utf8_whatever_the_locale(tmp_path: Path) -> None:
    """Run under the C locale with UTF-8 mode off, where an unpinned encoding is ASCII."""
    script = (
        "import sys; from pathlib import Path; from otto.init.write_policy import write_file; "
        "root = Path(sys.argv[1]); "
        "write_file(root / 'a.toml', '# a ' + chr(0x2014) + ' b', 'user'); "
        "write_file(root / 'b.json', '# a ' + chr(0x2014) + ' b', 'user', mode=0o600)"
    )
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env, check=True)
    assert (tmp_path / "a.toml").read_bytes() == "# a \u2014 b".encode()
    assert (tmp_path / "b.json").read_bytes() == "# a \u2014 b".encode()
