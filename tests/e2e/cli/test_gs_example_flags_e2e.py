"""The Getting Started example registers only the flag every command should carry.

``gs_example.actions`` registers ``BedVariant`` (``--variant``) for ``otto run``
and ``otto test``; ``BedInstall`` adds ``--ensure``/``--recover-partial`` and
stays ``install``'s own options. A registration reaches every command of a
verb, so registering ``BedInstall`` instead would put ``--ensure`` on
``otto run status`` and on ``otto test``.
"""

import re

import pytest

from tests._fixtures.gs_example import EXAMPLE
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

_FLAGS = ["--variant", "--ensure", "--recover-partial"]


def _flag_counts(argv: list[str], tmp_path) -> dict[str, int]:
    r = run_otto(argv, xdir=tmp_path, sut_dirs=EXAMPLE)
    assert r.returncode == 0, r.stdout + r.stderr
    # Count option rows only (a flag in the first column of rich's options
    # panel), not mentions in help prose ("With --ensure: ...").
    return {flag: len(re.findall(rf"^│ +{flag} ", r.stdout, re.MULTILINE)) for flag in _FLAGS}


def test_install_takes_its_own_flags_and_the_registered_one_once(tmp_path) -> None:
    counts = _flag_counts(["run", "install", "--help"], tmp_path)
    assert counts == {"--variant": 1, "--ensure": 1, "--recover-partial": 1}


@pytest.mark.parametrize("argv", [["run", "status", "--help"], ["test", "--help"]])
def test_other_commands_take_only_the_registered_flag(argv, tmp_path) -> None:
    counts = _flag_counts(argv, tmp_path)
    assert counts == {"--variant": 1, "--ensure": 0, "--recover-partial": 0}
