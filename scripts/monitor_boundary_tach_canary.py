"""Prove the monitor boundary test counts every import the installed tach counts.

Run from the repo root (``make lint-arch`` and nox's ``lint`` session do)::

    uv run --group lint python scripts/monitor_boundary_tach_canary.py

``tests/unit/test_monitor_service_reads_no_lab.py`` keeps the lab and the
lab-aware runner out of every monitor file but ``live.py``, a boundary tach
kept while ``otto.monitor.live`` was a module of its own. Its docstring says
it counts every import tach counts and lists each case where it is stricter.
That claim is about tach's behaviour, and the ``tach>=0.35.0`` floor floats:
a release that started counting a shape the test skips would leave the test
green over a boundary weaker than tach's. So this builds a toy otto tree in a
temporary directory, where ``otto.monitor`` may not depend on the lab's
packages or on ``otto.monitor.live``, writes each fixture of the test as
``otto/monitor/probe.py``, and asks the installed tach whether it counts the
import. It requires:

* every fixture of ``test_counted_as_tach_counts``: tach counts it, the test counts it;
* every fixture of ``test_skipped_as_tach_skips``: tach skips it, the test skips it;
* every fixture of ``test_stricter_than_tach``: tach skips it, the test counts it.

The fixtures live in the test, one home: this reads them from its
``parametrize`` marks and judges each with the test's own ``reads_the_lab``.
tach's rc 0 means skipped and rc 1 counted; any other rc fails that fixture,
printing the rc and tach's output. A self-check first feeds a fake rc 2 and
requires that failure, so a crashing tach can never read as "skipped".
"""

import importlib
import importlib.util
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOUNDARY = "tests.unit.test_monitor_service_reads_no_lab"

FILES = [
    "otto/__init__.py",
    "otto/errors.py",
    "otto/config/__init__.py",
    "otto/config/repo.py",
    "otto/config/fleet.py",
    "otto/context/__init__.py",
    "otto/contexts/__init__.py",
    "otto/lab/__init__.py",
    "otto/lab/fleet.py",
    "otto/labs/__init__.py",
    "otto/tunnel/__init__.py",
    "otto/tunnel/records.py",
    "otto/models/__init__.py",
    "otto/models/monitor.py",
    "otto/monitor/__init__.py",
    "otto/monitor/live.py",
    "otto/monitor/collector.py",
    "otto/monitor/livestream.py",
]
"""The toy tree: every module a fixture names, so tach resolves each import."""

MODULES = {
    "otto": [],
    "otto.errors": [],
    "otto.config": [],
    "otto.context": [],
    "otto.contexts": [],
    "otto.lab": [],
    "otto.labs": [],
    "otto.tunnel": [],
    "otto.models": [],
    "otto.monitor.live": [],
    "otto.monitor": ["otto", "otto.errors", "otto.models", "otto.contexts", "otto.labs"],
}
"""The toy tach modules: ``otto.monitor`` may import anything but the banned ones."""


@dataclass
class Fixture:
    """One fixture of the boundary test and what tach and the test must make of it."""

    text: str
    tach_counts: bool
    test_counts: bool


@dataclass
class TachRun:
    """One ``tach check`` of the toy tree."""

    rc: int
    output: str


def _fixtures(test: Callable[..., object]) -> "list[str]":
    marks = getattr(test, "pytestmark", [])
    return next(mark.args[1] for mark in marks if mark.name == "parametrize")


def _write_project(root: Path) -> Path:
    for name in FILES:
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (root / "tach.toml").write_text(
        'source_roots = ["src"]\n'
        + "".join(
            f'[[modules]]\npath = "{path}"\ndepends_on = {deps!r}\n'.replace("'", '"')
            for path, deps in MODULES.items()
        )
    )
    return root / "src/otto/monitor/probe.py"


def _run_tach(root: Path) -> TachRun:
    done = subprocess.run(
        [sys.executable, "-m", "tach", "check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return TachRun(done.returncode, done.stdout + done.stderr)


def judge(
    fixture: Fixture, counts: Callable[[str], "list[str]"], tach: Callable[[], TachRun]
) -> "str | None":
    """Return what is wrong with *fixture*, or ``None``; rc 0 = skipped, 1 = counted."""
    run = tach()
    if run.rc not in (0, 1):
        return f"tach exited {run.rc} (neither 0 nor 1) on {fixture.text!r}:\n{run.output}"
    tach_counts = run.rc == 1
    test_counts = bool(counts(fixture.text))
    if [tach_counts, test_counts] == [fixture.tach_counts, fixture.test_counts]:
        return None
    said = {True: "counts", False: "skips"}
    return (
        f"tach {said[tach_counts]} (want {said[fixture.tach_counts]}), "
        f"the test {said[test_counts]} (want {said[fixture.test_counts]}): {fixture.text!r}"
    )


def main() -> int:
    """Run every fixture; exit 1 when tach or the test no longer does what the test claims."""
    if importlib.util.find_spec("tach") is None:
        print(
            "monitor boundary canary: tach is not installed in this environment; it is in the "
            "lint dependency group, so run this via uv run --group lint.",
            file=sys.stderr,
        )
        return 1
    sys.path.insert(0, str(ROOT))
    boundary = importlib.import_module(BOUNDARY)
    fixtures = [
        *[Fixture(t, True, True) for t in _fixtures(boundary.test_counted_as_tach_counts)],
        *[Fixture(t, False, False) for t in _fixtures(boundary.test_skipped_as_tach_skips)],
        *[Fixture(t, False, True) for t in _fixtures(boundary.test_stricter_than_tach)],
    ]
    skipped = next(f for f in fixtures if not f.tach_counts)
    if judge(skipped, boundary.reads_the_lab, lambda: TachRun(2, "fake: tach crashed")) is None:
        print("monitor boundary canary: a fake tach rc 2 read as a result", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        probe = _write_project(root)
        problems = []
        for fixture in fixtures:
            probe.write_text(fixture.text)
            problems.append(judge(fixture, boundary.reads_the_lab, lambda: _run_tach(root)))
        probe.write_text("")
        control = _run_tach(root)
    failures = [p for p in problems if p is not None]
    if control.rc != 0:
        failures.append(f"an empty probe: tach exited {control.rc}, want 0:\n{control.output}")
    for problem in failures:
        print(f"monitor boundary canary: {problem}", file=sys.stderr)
    if failures:
        print(
            "The monitor boundary test no longer counts and skips what the installed tach "
            "does, as its docstring claims; see scripts/monitor_boundary_tach_canary.py. "
            "A tach that counts more is reviewed too: the docstring's list of the places the "
            "test is stricter would no longer be true.",
            file=sys.stderr,
        )
        return 1
    print(f"monitor boundary canary: {len(fixtures)} fixtures agree with the installed tach")
    return 0


if __name__ == "__main__":
    sys.exit(main())
