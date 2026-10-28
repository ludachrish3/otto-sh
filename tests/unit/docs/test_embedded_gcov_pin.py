"""The embedded-gcov commit the docs name is the commit the submodule pins."""

import re
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SUBMODULE = "tests/project3/third_party/embedded-gcov"
SHA = re.compile(r"\b[0-9a-f]{40}\b")
COPIES = [
    "docs/setup/tracks/embedded.md",
    "docs/cli/cov/instrumenting/embedded.md",
    "src/otto/coverage/embedded_gcov/README.md",
    "docs/examples/captures.toml",
]


def _gitlink(home: Path) -> str:
    out = subprocess.run(
        ["git", "ls-tree", "HEAD", SUBMODULE],
        cwd=CHECKOUT_ROOT,
        env=git_env(home),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = SHA.search(out)
    assert found, f"{SUBMODULE} is not a gitlink at HEAD: {out!r}"
    return found.group(0)


@pytest.mark.parametrize("copy", COPIES)
def test_the_copy_names_the_submodule_commit(copy: str, tmp_path: Path) -> None:
    text = (CHECKOUT_ROOT / copy).read_text(encoding="utf-8")
    assert _gitlink(tmp_path / "home") in text, f"{copy} does not name the submodule's commit"
