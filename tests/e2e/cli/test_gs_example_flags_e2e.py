"""The Getting Started example's flag joins ``otto run install`` and no other command.

``gs_example.actions`` registers ``BedVariant`` (``--variant``) for
``"run install"``; the install body's own class is otto's ``InstallOptions``
(``--ensure``/``--recover-partial``), which only ``otto run install`` takes.
"""

import re

import pytest

from tests._fixtures.gs_example import EXAMPLE
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

_FLAGS = ["--variant", "--ensure", "--recover-partial"]


def _flag_counts(argv: list[str], tmp_path) -> dict[str, int]:
    r = run_otto(argv, xdir=tmp_path, roots=EXAMPLE)
    assert r.returncode == 0, r.stdout + r.stderr
    # Count option rows only (a flag in the first column of rich's options
    # panel), not mentions in help prose ("With --ensure: ...").
    return {flag: len(re.findall(rf"^│ +{flag} ", r.stdout, re.MULTILINE)) for flag in _FLAGS}


def test_install_takes_its_own_flags_and_the_registered_one_once(tmp_path) -> None:
    counts = _flag_counts(["run", "install", "--help"], tmp_path)
    assert counts == {"--variant": 1, "--ensure": 1, "--recover-partial": 1}


@pytest.mark.parametrize("argv", [["run", "status", "--help"], ["test", "--help"]])
def test_other_commands_take_none_of_them(argv, tmp_path) -> None:
    counts = _flag_counts(argv, tmp_path)
    assert counts == {"--variant": 0, "--ensure": 0, "--recover-partial": 0}
