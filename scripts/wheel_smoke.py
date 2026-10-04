"""Smoke an INSTALLED otto: the one a user gets, not the dev checkout.

Run by ``nox -s wheel_smoke`` (``make wheel-smoke``) with the interpreter of a
venv that holds the built wheel and otto's runtime dependencies only: no dev
group, no editable install, no source tree on ``sys.path``. Every other lane
runs otto from the dev venv, where a development dependency (pytest-cov, #593)
or a plugin pytest auto-loads hides what an installed otto lacks.

It checks, running otto in a scratch directory outside the checkout:

1. ``otto`` is imported from the installed wheel, not ``src/`` (an editable
   install, or ``src/`` on ``sys.path``);
2. every ``otto`` module imports;
3. the hostless CLI lists, runs (seeded, unseeded, in file order, and
   failing, with its exit code) and honors an async test and a
   ``@pytest.mark.timeout`` — with pytest's plugin autoload on, and off
   (``PYTEST_DISABLE_PLUGIN_AUTOLOAD``), which otto must not depend on.

The SUT is generated under a temporary directory; its lab names one host that
nothing contacts. Exits non-zero naming every check that failed.
"""

import importlib
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

SUITE = """\
import asyncio
import time

import pytest


def test_sync():
    assert True


async def test_async():
    await asyncio.sleep(0)


@pytest.mark.timeout(1)
def test_times_out():
    time.sleep(30)


def test_fails():
    assert False
"""

# (argv, expected exit code, text the output must contain)
CHECKS: list[tuple[list[str], int, str]] = [
    (["--help"], 0, "Usage"),
    (["test", "--list-tests"], 0, "test_async"),
    (["-l", "smoke", "test", "test_sync", "test_async", "--no-cov"], 0, "2 passed"),
    (["-l", "smoke", "test", "test_sync", "--no-cov", "--seed", "7"], 0, "1 passed"),
    (["-l", "smoke", "test", "test_async", "--no-cov", "--no-random"], 0, "1 passed"),
    (["-l", "smoke", "test", "test_times_out", "--no-cov", "--no-random"], 1, "Timeout"),
    (["-l", "smoke", "test", "test_fails", "--no-cov"], 1, "1 failed"),
]


def _import_all() -> list[str]:
    """Import every otto module; return a failure line per one that does not import."""
    import otto

    if SRC in Path(otto.__file__).resolve().parents:
        return [f"otto imported from {SRC} ({otto.__file__}), not an installed wheel"]
    failures = []
    for info in pkgutil.walk_packages(otto.__path__, "otto."):
        if info.name == "otto.__main__":  # importing it runs the CLI
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001 — every failure is reported, none stops the walk
            failures.append(f"import {info.name}: {type(e).__name__}: {e}")
    return failures


def _sut(root: Path) -> Path:
    repo = root / "sut"
    for d in (".otto", "tests", "labs"):
        (repo / d).mkdir(parents=True)
    (repo / ".otto" / "settings.toml").write_text(
        'name = "smoke"\nversion = "1.0.0"\ntests = ["tests"]\n'
        '[[lab.sources]]\nbackend = "json"\npaths = ["labs"]\n'
    )
    host = {
        "ip": "127.0.0.1",
        "os_type": "unix",
        "valid_terms": ["ssh"],
        "creds": [{"login": "nobody", "password": "unused"}],
    }
    lab = {"labs": {"smoke": {}}, "elements": [{"name": "h", "labs": ["smoke"], "hosts": [host]}]}
    (repo / "labs" / "lab.json").write_text(json.dumps(lab))
    (repo / "tests" / "test_smoke.py").write_text(SUITE)
    return repo


def _cli(root: Path, repo: Path) -> list[str]:
    """Run every CLI check with plugin autoload on and off; return a line per failure."""
    otto_bin = Path(sys.executable).parent / "otto"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("OTTO_", "PYTEST_", "COV"))}
    base.update(OTTO_HOME=str(root / "home"), OTTO_SUT_DIRS=str(repo), NO_COLOR="1")
    failures = []
    for autoload, extra in (("on", {}), ("off", {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})):
        for argv, want, text in CHECKS:
            env = {**base, **extra, "OTTO_XDIR": str(root / "x")}
            r = subprocess.run(  # noqa: S603 — the installed otto, with fixed arguments
                [str(otto_bin), *argv],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            out = r.stdout + r.stderr
            ok = r.returncode == want and text in out
            label = f"autoload {autoload}: otto {' '.join(argv)}"
            print(f"{'ok  ' if ok else 'FAIL'} {label} (exit {r.returncode})")
            if not ok:
                failures.append(f"{label}: exit {r.returncode}, wanted {want} and {text!r}\n{out}")
    return failures


def main() -> int:
    """Run every check; return the exit code (1 when any failed)."""
    failures = _import_all()
    with tempfile.TemporaryDirectory(prefix="otto-wheel-smoke-") as d:
        root = Path(d)
        failures += _cli(root, _sut(root))
    for failure in failures:
        print(f"\nFAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
