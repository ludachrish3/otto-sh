"""A file ADDED to a watched directory moves ITS directory's own stat digest.

The discriminating claim under test is narrower than "the section digest
moves": a reader that stores ``(path, mtime_ns, size)`` triples for a FROZEN
key-path list (never re-globbing) must still see a new file arrive, purely
through the directory entry's own mtime. `test_a_new_lab_file_in_a_globbed_
directory_moves_the_names_digest` freezes the key list before the write and
re-hashes only those frozen paths, so it cannot pass via the new file
entering a re-run glob's match list — only via the glob root's directory
entry changing.
"""

import hashlib
import os
import time
from pathlib import Path

from otto.config import completion_cache as _cc
from otto.config.cache_sections import section_by_name, section_digest
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

_CREDS = [{"login": "u", "password": "p"}]

# Three distinct lab-source shapes, each pinning a different `visited` sink in
# `expand_lab_paths`: "lab" is a bare directory entry (adds itself); "solo/
# solo.json" is a `.json` entry (adds its OWN parent, "solo" — a directory no
# other entry visits, so deleting that sink is the only way to lose it);
# "extra/**/*.json" is a glob (adds its non-glob root "extra" AND, per match,
# that match's parent — "extra/nested", reachable only through a pre-existing
# nested match, never through the root sink alone).
SETTINGS = (
    '[[lab.sources]]\nbackend = "json"\npaths = ["lab", "solo/solo.json", "extra/**/*.json"]\n'
)


def _hash_path(path: Path) -> str:
    """Stat-based digest of one path, the same primitive `_digest` folds per key path."""
    h = hashlib.sha256()
    _cc.hash_file(h, path)
    return h.hexdigest()


def _repo(tmp_path: Path):
    from otto.config.repo import Repo

    root = make_sut_repo(
        tmp_path / "sut",
        tests=["tests"],
        extra=SETTINGS,
        files={
            "tests/test_top.py": "def test_a():\n    pass\n",
            "tests/sub/test_deep.py": "def test_b():\n    pass\n",
        },
    )
    write_lab_json(
        root / "lab" / "lab.json",
        [{"ip": "1.1.1.1", "element": "h", "labs": ["e"], "creds": _CREDS}],
    )
    write_lab_json(
        root / "solo" / "solo.json",
        [{"ip": "2.2.2.2", "element": "h2", "labs": ["e"], "creds": _CREDS}],
        declare_labs=False,
    )
    # Pre-existing and NESTED — not directly in `extra/` — so a later write of
    # `extra/more.json` (directly in `extra/`) exercises the glob ROOT sink
    # without ever touching this match's parent.
    write_lab_json(
        root / "extra" / "nested" / "x.json",
        [{"ip": "3.3.3.3", "element": "h3", "labs": ["e"], "creds": _CREDS}],
        declare_labs=False,
    )
    return Repo(sut_dir=root)


def test_walkers_report_visited_directories(tmp_path):
    """Every `expand_lab_paths` sink is exercised: directory, `.json`-entry
    parent, glob root, and glob-match parent — four distinct directories,
    four distinct code paths, none of them redundant with another.
    """
    repo = _repo(tmp_path)
    names = set(section_by_name("names").key_paths([repo]))
    tests = set(section_by_name("tests").key_paths([repo]))
    assert {
        repo.sut_dir / "lab",  # bare directory entry
        repo.sut_dir / "solo",  # `.json` entry's parent
        repo.sut_dir / "extra",  # glob's non-glob root
        repo.sut_dir / "extra" / "nested",  # a glob match's parent
        repo.sut_dir / "tests",
    } <= names
    assert {repo.sut_dir / "tests", repo.sut_dir / "tests" / "sub"} <= tests


def test_a_new_lab_file_in_a_globbed_directory_moves_the_names_digest(tmp_path):
    """A file added directly under `extra/` moves ITS directory entry's
    stat digest — proven by freezing the key-path list before the write and
    re-hashing only those frozen paths, so the new file (absent from the
    frozen list) cannot be what moves the digest. Only `extra` itself may
    change: `extra/nested` was untouched, and every other key path is
    unrelated to this write.
    """
    repo = _repo(tmp_path)
    # Backdate `extra`'s mtime 2s: Linux stamps directory mtimes off a coarse
    # clock, so the fixture's `mkdir` (of `extra/nested`) and this write below
    # can land in the same tick, leaving the mtime unchanged despite the entry
    # having changed — backdating guarantees the write must move it.
    t = time.time_ns() - 2_000_000_000
    os.utime(repo.sut_dir / "extra", ns=(t, t))
    keys = sorted(set(section_by_name("names").key_paths([repo])))
    before = [_hash_path(p) for p in keys]
    write_lab_json(repo.sut_dir / "extra" / "more.json", [], declare_labs=False)
    after = [_hash_path(p) for p in keys]
    assert after != before
    changed = [key for key, b, a in zip(keys, before, after, strict=True) if b != a]
    assert changed == [repo.sut_dir / "extra"]


def test_a_new_nested_test_file_moves_the_tests_digest_only(tmp_path):
    """A NESTED test file is outside `Repo.iter_test_files` (top-level only),
    so it can never enter the `names` key set — as file or as directory — and
    `names` has nothing to move. It DOES enter the `tests` key set: the full
    corpus walk re-discovers the file itself on the next call, so the new
    file's OWN path becomes a new member of the hashed key set — a
    structural change to WHICH paths are hashed, not a comparison of any one
    path's mtime — through the ordinary, pre-existing file-rediscovery path,
    not specifically through the new directory-visiting sink this task adds.
    Not wall-clock-bound: no assertion here depends on an existing path's
    mtime moving, so no backdating is needed.
    """
    repo = _repo(tmp_path)
    names_before = section_digest(section_by_name("names"), [repo])
    tests_before = section_digest(section_by_name("tests"), [repo])
    (repo.sut_dir / "tests" / "sub" / "test_new.py").write_text("def test_c():\n    pass\n")
    assert section_digest(section_by_name("tests"), [repo]) != tests_before
    assert section_digest(section_by_name("names"), [repo]) == names_before
