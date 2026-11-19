"""The dependency-floor lanes are wired where #1181 put them.

Every release smokes the wheel with each direct dependency pinned to its
declared floor (``wheel_smoke_floors``, run by ``make wheel-smoke``), and the
hostless unit suite runs at those floors every night (``tests_unit_floors``).
Both sessions read the floors through one resolution, so a floor one of them
tests is the floor the other tests.
"""

import ast
import re

import pytest
import yaml

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

NIGHTLY = CHECKOUT_ROOT / ".github" / "workflows" / "nightly.yml"
NOXFILE = CHECKOUT_ROOT / "noxfile.py"
MAKEFILE = CHECKOUT_ROOT / "Makefile"


def _session(name: str) -> ast.FunctionDef:
    tree = ast.parse(NOXFILE.read_text(encoding="utf-8"))
    (fn,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    return fn


def _calls(fn: ast.FunctionDef) -> "set[str]":
    return {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_both_floor_sessions_resolve_the_floors_through_one_helper():
    for name in ("wheel_smoke_floors", "tests_unit_floors"):
        assert "_floor_requirements" in _calls(_session(name)), name


def test_make_wheel_smoke_runs_the_locked_and_the_floor_smoke():
    recipe = MAKEFILE.read_text(encoding="utf-8").split("\nwheel-smoke:", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"\bnox -s wheel_smoke wheel_smoke_floors\b", recipe), recipe


def test_the_unit_floor_lane_runs_nightly_in_a_blocking_reported_job():
    jobs = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]
    job = jobs["unit-floors"]
    runs = re.compile(r"\bnox -s tests_unit_floors\b")
    (step,) = [s for s in job["steps"] if "run" in s and runs.search(s["run"])]
    assert "continue-on-error" not in job
    assert "continue-on-error" not in step
    assert "if" not in step
    assert "unit-floors" in jobs["report-failure"]["needs"]


def test_no_session_takes_noxs_checkout_local_scratch():
    """``create_tmp`` puts TMPDIR under ``.nox/`` in the checkout; every scratch is ``_scratch``'s.

    A pytester run there read the checkout's pyproject.toml and git work tree
    (about 110 of the first floor nightly's 203 failures), and the wheel
    smoke ran from inside the checkout it must not see.
    """
    tree = ast.parse(NOXFILE.read_text(encoding="utf-8"))
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "create_tmp"
    ]
    assert calls == []


def test_a_scratch_directory_is_fresh_private_and_outside_the_checkout(tmp_path, monkeypatch):
    import tempfile
    import types

    import noxfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(noxfile, "_SCRATCH_ROOTS", {})
    session = types.SimpleNamespace(name="tests_unit_floors-3.10", env={})
    first = noxfile._scratch(session, "floors")
    (first / "left-over").write_text("x")
    assert noxfile._scratch(session, "floors") == first
    assert list(first.iterdir()) == []
    assert first.resolve().is_relative_to(tmp_path.resolve())
    assert not first.resolve().is_relative_to(CHECKOUT_ROOT.resolve())
    dev = noxfile._scratch(session, "dev")
    assert dev != first
    assert dev.parent == first.parent, "one invocation's session shares one root"
    assert session.env == {}, "TMPDIR stays the host's"

    # Another nox invocation of the same session, running at the same time.
    (first / "in-use").write_text("x")
    monkeypatch.setattr(noxfile, "_SCRATCH_ROOTS", {})
    other = noxfile._scratch(session, "floors")
    assert other.parent != first.parent
    assert (first / "in-use").exists(), "a concurrent run never empties another's scratch"


class _ReusedSession:
    """A ``-R`` / ``--no-install`` session: installs are skipped, so their files never appear."""

    name = "tests_unit_floors-3.10"
    python = "3.10"

    def __init__(self) -> None:
        self.posargs: "list[str]" = []
        self.env: "dict[str, str]" = {}
        self.ran: "list[tuple[str, ...]]" = []

    def run_install(self, *args, **kwargs):
        return None

    def run(self, *args, **kwargs):
        self.ran.append(tuple(str(a) for a in args))
        return "pydantic==2.10.4\n" if "--constraints" in args else None

    def install(self, *args, **kwargs):
        raise AssertionError("no install under -R")

    def error(self, message):
        raise AssertionError(message)


def test_the_unit_floor_suite_reuses_its_venv_under_no_install(tmp_path, monkeypatch):
    """``-R`` skips the compile and the export; the suite still runs, not a FileNotFoundError."""
    import tempfile

    import noxfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(noxfile, "_SCRATCH_ROOTS", {})
    session = _ReusedSession()
    noxfile.tests_unit_floors.func(session)
    assert [args[0] for args in session.ran if args[0] == "pytest"] == ["pytest", "pytest"]
