"""The kernel-module coverage library is otto_kmodcov; its old stem survives only in frozen history.

The old name is spelled by concatenation below so that this file is not itself a hit.
"""

import subprocess
from pathlib import Path

from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT

OLD_STEM = "k" + "gcov"
FROZEN_HISTORY = ("docs/superpowers/specs/", "CHANGELOG.md")


def test_the_old_library_stem_survives_only_in_frozen_history(tmp_path: Path):
    listing = subprocess.run(
        ["git", "grep", "-il", OLD_STEM, "--", "."],
        cwd=PROJECT_ROOT,
        env=git_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.returncode in (0, 1), listing.stderr
    offenders = sorted(
        path
        for path in listing.stdout.splitlines()
        if not path.startswith(FROZEN_HISTORY) and path != "tests/unit/test_library_name.py"
    )
    assert offenders == [], f"the old stem is back in: {offenders}"
