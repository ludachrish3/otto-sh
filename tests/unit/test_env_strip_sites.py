"""Every environment sanitizer strips ``OTTOPATH`` as well as the ``OTTO_`` prefix.

``OTTOPATH`` does not start with ``OTTO_``, so a sanitizer written as a prefix
test silently lets it through. Code that imports otto calls
:func:`otto.config.env.is_otto_env_var`, or :func:`otto.config.env.without_otto_env`
to copy an environment without it. The noxfile and the scripts that do not
import otto spell ``"OTTOPATH"`` on the same line as the prefix test.
This scan finds every prefix test on a name and requires the second spelling
wherever the first is not used.

Its blind spots, stated so nobody reads more into a green run: it matches
``"OTTO_"`` only as the FIRST item of a ``startswith`` tuple (a later item is
not seen); any line that names ``"OTTOPATH"`` passes, whatever else it does;
and it reads tracked ``.py`` files only. The Makefile's doctest strip is a
recipe, not Python, so ``tests/unit/test_doctest_lane_hermeticity.py`` guards
it instead.
"""

import re
import subprocess
from pathlib import Path

from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

PREFIX_TEST = re.compile(r"""\.startswith\(\s*\(?\s*["']OTTO_["']""")
THIS_FILE = "tests/unit/test_env_strip_sites.py"
PLANTED = 'env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}'
"""The prefix-only sanitizer the scan must catch: its own red, below."""

NOT_THE_ENVIRONMENT = {
    # Reads the lines a remote probe printed, not this process's environment.
    "src/otto/host/survey/dial.py",
    # Parses one OTTO_FILE= line of a remote probe's output.
    "src/otto/host/survey/inventory.py",
    # This scan. Its one prefix test is PLANTED, a fixture string that
    # sanitizes nothing; test_the_self_exemption_covers_only_the_planted_line
    # keeps the exemption that narrow.
    THIS_FILE,
}
LITERAL_SITES = [
    "noxfile.py",
    "scripts/import_budget.py",
    "scripts/wheel_smoke.py",
]


def offenders(root: Path, files: list[str]) -> list[str]:
    """``path:line: text`` for each prefix test on a line that does not also name OTTOPATH."""
    found = []
    for rel in files:
        if rel in NOT_THE_ENVIRONMENT:
            continue
        for lineno, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            if PREFIX_TEST.search(line) and '"OTTOPATH"' not in line:
                found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


def _tracked_python(home: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=CHECKOUT_ROOT,
        env=git_env(home),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p and not p.startswith("docs/superpowers/")]


def test_every_prefix_sanitizer_also_strips_ottopath(tmp_path):
    assert offenders(CHECKOUT_ROOT, _tracked_python(tmp_path)) == []


def test_the_sites_that_cannot_import_otto_spell_ottopath():
    missing = [
        rel for rel in LITERAL_SITES if '"OTTOPATH"' not in (CHECKOUT_ROOT / rel).read_text()
    ]
    assert missing == []


def test_a_prefix_only_sanitizer_is_caught(tmp_path):
    (tmp_path / "s.py").write_text(PLANTED + "\n")
    assert offenders(tmp_path, ["s.py"]) == [f"s.py:1: {PLANTED}"]


def test_the_self_exemption_covers_only_the_planted_line():
    """This file is exempt only because its one prefix test is the planted string."""
    lines = (CHECKOUT_ROOT / THIS_FILE).read_text(encoding="utf-8").splitlines()
    hits = [line.strip() for line in lines if PREFIX_TEST.search(line)]
    assert hits == [f"PLANTED = {PLANTED!r}"]


def test_the_shared_strip_keeps_only_the_machines_own_settings():
    """``without_otto_env`` drops otto's configuration but keeps a slow machine's deadline."""
    from otto.config.env import machine_env_vars, without_otto_env

    env = {
        "PATH": "/bin",
        "OTTOPATH": "/p",
        "OTTO_LAB": "lab",
        "OTTO_XDIR": "/x",
        "OTTO_INIT_TIMEOUT": "30",
    }
    assert without_otto_env(env) == {"PATH": "/bin", "OTTO_INIT_TIMEOUT": "30"}
    assert {"OTTO_INIT_TIMEOUT"} == machine_env_vars()
