"""Two roots of one project are refused; none of them loads.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Two roots of one project are refused). Every red here is reached through an
interface that exists before the refusal does: ``discover()``, the
``BootstrapError`` it records, and the ``otto`` CLI.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from otto.config.project import clear_discovery, discover
from otto.config.scope import BootstrapError
from tests._fixtures.project_scaffold import make_project

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]
_OTTO = [str(Path(sys.executable).parent / "otto")]  # the console script, as a TAB runs it
_REFUSED = "roots in OTTOPATH"

# Each pair normalizes alike (lowercase; a run of -, _ and . is one -), and the
# third column is that normalized name, which the message reports.
TWINS = [
    ("twin", "Twin", "twin"),
    ("twin", "TWIN", "twin"),
    ("t-win", "t_win", "t-win"),
    ("t-win", "T.Win", "t-win"),
    ("t-win", "t__win", "t-win"),
]


@pytest.fixture
def ottopath(monkeypatch):
    def set_roots(*roots):
        monkeypatch.setenv("OTTOPATH", os.pathsep.join(str(r) for r in roots))
        clear_discovery()

    yield set_roots
    clear_discovery()


@pytest.mark.parametrize(("first", "second", "normalized"), TWINS, ids=lambda v: v)
def test_two_roots_claiming_one_normalized_name_both_fail(
    tmp_path, ottopath, first, second, normalized
):
    one = make_project(tmp_path / "one", name=first)
    two = make_project(tmp_path / "two", name=second)
    other = make_project(tmp_path / "other", name="other")
    ottopath(one, two, other)
    result = discover()
    assert [p.name for p in result.projects] == ["other"]
    assert [(e.root, e.source) for e in result.errors] == [(one, "OTTOPATH"), (two, "OTTOPATH")]
    assert all(isinstance(e, BootstrapError) for e in result.errors)
    tail = f"project {normalized!r} has 2 roots in OTTOPATH: {one}, {two}; keep one"
    assert [str(e) for e in result.errors] == [
        f"project root {one}: {tail}",
        f"project root {two}: {tail}",
    ]


def test_three_roots_are_all_named_in_each_frame(tmp_path, ottopath):
    roots = [make_project(tmp_path / d, name=n) for d, n in (("a", "p"), ("b", "P"), ("c", "p"))]
    ottopath(*roots)
    result = discover()
    assert result.projects == []
    listed = ", ".join(str(r) for r in roots)
    assert [str(e) for e in result.errors] == [
        f"project root {r}: project 'p' has 3 roots in OTTOPATH: {listed}; keep one" for r in roots
    ]


def test_a_twin_whose_settings_fail_to_parse_is_not_a_duplicate(tmp_path, ottopath):
    good = make_project(tmp_path / "good", name="twin")
    bad = make_project(tmp_path / "bad", name="twin", extra="broken = [")
    ottopath(good, bad)
    result = discover()
    assert [p.root for p in result.projects] == [good]
    assert [e.root for e in result.errors] == [bad]
    assert _REFUSED not in str(result.errors[0])


def _run(tmp_path, roots, *argv, cmd=_ENTRY, extra=None):
    env = {k: v for k, v in os.environ.items() if not (k.startswith("OTTO_") or k == "OTTOPATH")}
    env.update(
        OTTOPATH=os.pathsep.join(str(r) for r in roots),
        OTTO_HOME=str(tmp_path / "home"),
        OTTO_XDIR=str(tmp_path / "xdir"),
        **(extra or {}),
    )
    return subprocess.run(
        [*cmd, *argv],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _twins_and_other(tmp_path):
    return [
        make_project(tmp_path / "a", name="my-proj"),
        make_project(tmp_path / "b", name="my.proj"),
        make_project(tmp_path / "c", name="other"),
    ]


def test_help_still_works_beside_a_refused_name(tmp_path):
    proc = _run(tmp_path, _twins_and_other(tmp_path), "--help")
    assert proc.returncode == 0, proc.stderr
    assert _REFUSED in proc.stderr  # the startup warning, one line per root


def test_completion_still_works_beside_a_refused_name(tmp_path):
    comp = {"_OTTO_COMPLETE": "complete_bash", "COMP_WORDS": "otto ", "COMP_CWORD": "1"}
    proc = _run(tmp_path, _twins_and_other(tmp_path), cmd=_OTTO, extra=comp)
    assert proc.returncode == 0, proc.stderr
    assert "run" in proc.stdout.split()


@pytest.mark.parametrize(
    "switches",
    [["-I", "My_Proj"], ["-E", "My_Proj"], ["-I", "other"]],
    ids=["include-the-twin", "exclude-the-twin", "include-an-unaffected-project"],
)
def test_a_real_command_fails_loudly_whatever_the_switches_say(tmp_path, switches):
    proc = _run(tmp_path, _twins_and_other(tmp_path), *switches, "--lab", "nolab", "run", "status")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert _REFUSED in out
    assert "no project" not in out  # a refused name is still a known name
    assert "fails to load" in out  # the load gate, before the lab is built


def _requiring(root: Path, name: str, needs: str) -> Path:
    return make_project(root, name=name, extra=f'[dependencies]\nrequired = ["{needs}"]')


def _bootstrap_messages() -> list[str]:
    from otto import bootstrap as bs

    bs.invalidate()
    try:
        return [str(e) for e in bs.bootstrap().errors]
    finally:
        bs.invalidate()


def test_a_dependency_on_a_refused_name_says_it_was_refused(tmp_path, ottopath):
    """Red before the fix: the error said "no project named 'twin' in OTTOPATH",
    false when two roots name it."""
    one = make_project(tmp_path / "one", name="twin")
    two = make_project(tmp_path / "two", name="Twin")
    app = _requiring(tmp_path / "app", "app", "twin")
    ottopath(one, two, app)
    messages = _bootstrap_messages()
    assert (
        f"project root {app}: dependency 'twin' is not satisfied: project 'twin' was "
        "refused because more than one root in OTTOPATH declares it"
    ) in messages, messages
    assert not any("no project named 'twin'" in m for m in messages), messages


def test_a_dependency_on_an_absent_name_still_says_no_project(tmp_path, ottopath):
    app = _requiring(tmp_path / "app", "app", "ghost")
    ottopath(app)
    assert (
        f"project root {app}: dependency 'ghost' is not satisfied: no project named "
        "'ghost' in OTTOPATH"
    ) in _bootstrap_messages()


def test_the_resolver_reports_a_refused_name_it_is_handed(tmp_path, ottopath):
    from otto.config.dependencies import resolve_dependencies

    app = _requiring(tmp_path / "app", "app", "Twin")
    ottopath(app)
    projects = discover().projects
    refused = resolve_dependencies(projects, refused=["twin"])
    absent = resolve_dependencies(projects)
    assert [str(e) for e in refused.errors] == [
        (
            f"project root {app}: dependency 'Twin' is not satisfied: project 'Twin' was "
            "refused because more than one root in OTTOPATH declares it"
        )
    ]
    assert [str(e) for e in absent.errors] == [
        (
            f"project root {app}: dependency 'Twin' is not satisfied: no project named "
            "'Twin' in OTTOPATH"
        )
    ]
