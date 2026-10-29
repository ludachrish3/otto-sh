"""``atomic_write_bytes``: otto's one write-then-rename sequence.

The inventory snapshot, both completion caches and the known-hosts store's
forget all write through it; these pin what each of them relies on.
"""

import os
import stat
from pathlib import Path

import pytest

from otto.utils import atomic_write_bytes


def test_a_short_write_is_resumed_until_every_byte_lands(tmp_path, monkeypatch):
    """``os.write`` may write fewer bytes than asked; the rest must not be dropped."""
    real_write = os.write

    def one_byte_at_a_time(fd, data):
        return real_write(fd, bytes(data[:1]))

    monkeypatch.setattr(os, "write", one_byte_at_a_time)
    path = tmp_path / "f"
    atomic_write_bytes(path, b"hello world\n")
    monkeypatch.undo()
    assert path.read_bytes() == b"hello world\n"


def test_the_new_file_is_private_unless_the_original_mode_is_preserved(tmp_path):
    path = tmp_path / "f"
    atomic_write_bytes(path, b"a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    atomic_write_bytes(path, b"b", preserve=path.stat())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_bytes() == b"b"


@pytest.mark.parametrize(
    ("owner", "failing"), [(os, "write"), (os, "fsync"), (Path, "replace")], ids=str
)
def test_a_failure_leaves_the_original_and_no_temporary(tmp_path, monkeypatch, owner, failing):
    path = tmp_path / "f"
    path.write_bytes(b"original")

    def broken(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(owner, failing, broken)
    with pytest.raises(OSError, match="No space left"):
        atomic_write_bytes(path, b"new")
    monkeypatch.undo()
    assert path.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_a_symlink_is_replaced_at_its_target(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b"old")
    link = tmp_path / "link"
    link.symlink_to(real)
    atomic_write_bytes(link, b"new")
    assert link.is_symlink()
    assert real.read_bytes() == b"new"


def test_the_temporary_is_listed_in_pending_only_while_it_exists(tmp_path, monkeypatch):
    pending: set[str] = set()
    seen: list[set[str]] = []
    real_fsync = os.fsync

    def spy(fd):
        seen.append(set(pending))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    atomic_write_bytes(tmp_path / "f", b"x", pending=pending)
    assert len(seen) == 1
    assert len(seen[0]) == 1
    assert pending == set()


def test_exclusive_creates_a_missing_file_whole_and_private(tmp_path):
    target = tmp_path / "key"
    atomic_write_bytes(target, b"first", exclusive=True)
    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["key"]  # no temporary left


def test_exclusive_never_replaces_an_existing_file(tmp_path):
    """Of two racing creators one wins; the other gets FileExistsError and no litter."""
    target = tmp_path / "key"
    atomic_write_bytes(target, b"winner", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"loser", exclusive=True)
    assert target.read_bytes() == b"winner"
    assert [p.name for p in tmp_path.iterdir()] == ["key"]
