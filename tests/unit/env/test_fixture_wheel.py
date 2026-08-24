"""The committed fixture wheel is well-formed and matches its builder.

Not a formality: a wheel whose RECORD does not match its contents installs
fine on some pip versions and fails on others, and the failure would surface
as an unrelated env-command test going red on one CI leg.
"""

import base64
import hashlib
import subprocess
import zipfile

from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT, WHEELS_DIR

WHEEL = WHEELS_DIR / "otto_fixture_beetroot-0.1.0-py3-none-any.whl"


def test_the_committed_wheel_exists():
    assert WHEEL.is_file(), (
        f"{WHEEL} is missing — regenerate with "
        "`python tests/_fixtures/wheels/build_fixture_beetroot.py`"
    )


def test_it_carries_the_three_required_metadata_files():
    with zipfile.ZipFile(WHEEL) as zf:
        names = set(zf.namelist())
    for required in ("METADATA", "WHEEL", "RECORD"):
        assert f"otto_fixture_beetroot-0.1.0.dist-info/{required}" in names, required


def test_every_record_hash_matches_the_archived_bytes():
    with zipfile.ZipFile(WHEEL) as zf:
        record = zf.read("otto_fixture_beetroot-0.1.0.dist-info/RECORD").decode()
        for line in record.strip().splitlines():
            name, _, rest = line.partition(",")
            if not rest or rest.startswith(","):
                continue  # the RECORD's own entry carries no hash
            algo_digest, _, size = rest.partition(",")
            _, _, expected = algo_digest.partition("=")
            data = zf.read(name)
            actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert actual == expected, f"RECORD hash mismatch for {name}"
            assert int(size) == len(data), f"RECORD size mismatch for {name}"


def test_rebuilding_reproduces_the_committed_bytes(tmp_path):
    """The builder is the wheel's source of truth; drift between them means
    someone edited the artifact by hand.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_fixture_beetroot", WHEELS_DIR / "build_fixture_beetroot.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rebuilt = mod.build(tmp_path)
    with zipfile.ZipFile(rebuilt) as new, zipfile.ZipFile(WHEEL) as old:
        assert {n: new.read(n) for n in new.namelist()} == {n: old.read(n) for n in old.namelist()}


def test_the_wheel_is_tracked_by_git_not_merely_present_on_disk(tmp_path):
    """Present and committed are different claims, and only one of them travels.

    ``.gitignore`` ignores every ``wheels/`` directory as build output, so this
    fixture directory lives behind an explicit negation. Without that negation
    the whole tree is invisible to git: the suite passes locally, because the
    files are right there on disk, and CI clones a repo with no wheel to
    install. ``test_the_committed_wheel_exists`` above cannot see the
    difference -- it asks the filesystem, which is exactly the witness that
    lies here.
    """
    probe = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(WHEEL)],
        cwd=PROJECT_ROOT,
        env=git_env(tmp_path),  # hermetic per tests/_fixtures/gitrepo.py
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit IS the finding, not an error to raise
    )
    assert probe.returncode == 0, (
        f"{WHEEL.relative_to(PROJECT_ROOT)} is not tracked by git — it exists on "
        "disk but would not reach a clone. Check that .gitignore still carries "
        "the `!tests/_fixtures/wheels/` negation beside its `wheels/` rule."
    )
