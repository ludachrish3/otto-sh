"""check_docs_examples: every docs example project validates without a lab."""

import os
import stat
import sys
from pathlib import Path

import pytest

from scripts import check_docs_examples as cde
from tests._fixtures.project_scaffold import make_project

pytestmark = pytest.mark.interpreter_agnostic


def _fake_otto(tmp_path: Path, fail_on: str = "") -> str:
    """An `otto` stand-in that exits 3 when its argv contains *fail_on*."""
    script = tmp_path / "otto"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"fail_on = {fail_on!r}\n"
        "if '--list-labs' in sys.argv: print('  \u2022 bench')\n"
        "sys.exit(3 if fail_on and fail_on in ' '.join(sys.argv[1:]) else 0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_every_project_with_settings_is_an_example(tmp_path):
    make_project(tmp_path / "b")
    make_project(tmp_path / "a")
    (tmp_path / "captures").mkdir()
    assert [p.name for p in cde.examples(tmp_path)] == ["a", "b"]


def test_a_passing_example_reports_nothing(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    assert cde.check(example, root, otto=_fake_otto(tmp_path)) == []


def test_a_failing_command_names_the_example_and_the_command(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    failures = cde.check(example, root, otto=_fake_otto(tmp_path, fail_on="run install"))
    assert len(failures) == 1
    assert failures[0].startswith("a: ")
    assert "`otto -n run install`" in failures[0]
    assert "exit 3" in failures[0]


def test_every_example_is_checked_even_after_a_failure(tmp_path, monkeypatch, capsys):
    root = tmp_path / "examples"
    make_project(root / "a")
    make_project(root / "b")
    otto = _fake_otto(tmp_path, fail_on="--list-products")
    monkeypatch.setattr(cde, "EXAMPLES", root)
    monkeypatch.setattr(cde, "_otto", lambda: otto)
    assert cde.main([]) == 1
    out = capsys.readouterr().out
    assert "a: FAIL" in out
    assert "b: FAIL" in out
    assert "checked 2 example project(s), 2 failure(s)" in out


def test_the_check_never_writes_into_the_source_tree(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    cde.check(example, root, otto=_fake_otto(tmp_path))
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_a_cmake_example_builds_and_runs_its_tests(tmp_path, monkeypatch):
    root = tmp_path / "examples"
    example = make_project(root / "a", files={"CMakeLists.txt": "project(a)\n"})
    log = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("cmake", "ctest"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert cde.check(example, root, otto=_fake_otto(tmp_path)) == []
    calls = log.read_text().splitlines()
    assert [call.split()[:2] for call in calls] == [
        ["cmake", "-S"],
        ["cmake", "--build"],
        ["ctest", "--test-dir"],
    ]


def test_the_otto_commands_run_with_the_example_as_its_only_project(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    seen = tmp_path / "env"
    otto = tmp_path / "otto"
    otto.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"open({str(seen)!r}, 'a').write(os.environ.get('OTTOPATH', '') + '\\n')\n"
        "if '--list-labs' in sys.argv: print('  \u2022 bench')\n"
    )
    otto.chmod(0o755)
    cde.check(example, root, otto=str(otto))
    paths = seen.read_text().splitlines()
    assert len(paths) == 3  # --list-labs, --list-products, -n run install
    assert all(Path(p).name == "a" and Path(p) != example for p in paths)


def _recording_otto(tmp_path: Path, labs: "list[str]") -> "tuple[str, Path]":
    """An `otto` stand-in that logs its argv and lists *labs* for --list-labs."""
    log = tmp_path / "argv"
    otto = tmp_path / "otto-rec"
    listing = "".join(f"  \u2022 {lab}\n" for lab in labs)
    otto.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"if '--list-labs' in sys.argv: sys.stdout.write({listing!r})\n"
    )
    otto.chmod(0o755)
    return str(otto), log


def test_the_install_preview_targets_the_project_s_own_lab(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a", extra='[project]\nlab_patterns = ["bench"]\n')
    otto, log = _recording_otto(tmp_path, ["bench", "other"])
    assert cde.check(example, root, otto=otto) == []
    assert "--lab bench -n run install" in log.read_text().splitlines()


def test_a_project_without_a_literal_lab_uses_the_only_lab_listed(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    otto, log = _recording_otto(tmp_path, ["unix"])
    assert cde.check(example, root, otto=otto) == []
    assert "--lab unix -n run install" in log.read_text().splitlines()


def test_an_example_whose_lab_cannot_be_told_fails_and_says_why(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a", extra='[project]\nlab_patterns = ["b.*"]\n')
    otto, _ = _recording_otto(tmp_path, ["bench", "other"])
    failures = cde.check(example, root, otto=otto)
    assert failures == [f"a: {cde.NO_LAB}"]
    assert "literal lab name" in cde.NO_LAB


def test_an_example_s_own_build_output_is_not_copied(tmp_path, monkeypatch):
    root = tmp_path / "examples"
    example = make_project(
        root / "a",
        files={
            "CMakeLists.txt": "project(a)\n",
            "build/CMakeCache.txt": "CMAKE_HOME_DIRECTORY=/x\n",
        },
    )
    seen = tmp_path / "seen"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("cmake", "ctest"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\nls "$PWD/build" >> "{seen}" 2>&1 || true\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert cde.check(example, root, otto=_fake_otto(tmp_path)) == []
    assert "CMakeCache.txt" not in seen.read_text()


def test_ctest_fails_an_example_that_registers_no_tests(tmp_path, monkeypatch):
    root = tmp_path / "examples"
    example = make_project(root / "a", files={"CMakeLists.txt": "project(a)\n"})
    log = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("cmake", "ctest"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    cde.check(example, root, otto=_fake_otto(tmp_path))
    (ctest,) = [line for line in log.read_text().splitlines() if line.startswith("ctest")]
    assert "--no-tests=error" in ctest.split()


def test_unreadable_settings_fail_that_example_and_the_rest_are_still_checked(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "examples"
    make_project(root / "a", extra="[project\n")
    make_project(root / "b")
    otto = _fake_otto(tmp_path)
    monkeypatch.setattr(cde, "EXAMPLES", root)
    monkeypatch.setattr(cde, "_otto", lambda: otto)
    assert cde.main([]) == 1
    out = capsys.readouterr().out
    assert "a: FAIL" in out
    assert "b: ok" in out
    assert "settings.toml" in out


def test_a_failing_lab_listing_is_reported_as_that_command(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a")
    failures = cde.check(example, root, otto=_fake_otto(tmp_path, fail_on="--list-labs"))
    assert len(failures) == 1
    assert "`otto --list-labs` failed (exit 3" in failures[0]


def test_a_missing_tool_fails_that_example_without_aborting(tmp_path, monkeypatch):
    root = tmp_path / "examples"
    example = make_project(root / "a", files={"CMakeLists.txt": "project(a)\n"})
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    failures = cde.check(example, root, otto=_fake_otto(tmp_path))
    assert any("`cmake -S` failed" in f and "not found" in f for f in failures)


# One use-case declared by two fragments; settings name only compose handles they declare.
_DOCKER = (
    '[project]\nlab_patterns = ["bench"]\n'
    "[docker]\n"
    '[[docker.composes]]\nname = "c"\npath = "c.yml"\n'
    '[[docker.composes]]\nname = "d"\npath = "d.yml"\n'
    '[[docker.use_cases]]\nname = "uc"\ncomposes = ["c"]\n'
    '[[docker.use_cases]]\nname = "uc"\ncomposes = ["d"]\n'
)


def test_a_docker_example_previews_its_compose_build_and_up(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a", extra=_DOCKER)
    otto, log = _recording_otto(tmp_path, ["bench"])
    assert cde.check(example, root, otto=otto) == []
    assert log.read_text().splitlines()[-2:] == [
        "--lab bench -n docker compose build uc",
        "--lab bench -n docker compose up uc",
    ]


def test_an_example_without_docker_previews_no_compose(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a", extra='[project]\nlab_patterns = ["bench"]\n')
    otto, log = _recording_otto(tmp_path, ["bench"])
    cde.check(example, root, otto=otto)
    assert not [c for c in log.read_text().splitlines() if "docker" in c]


def test_a_failing_compose_preview_names_the_command(tmp_path):
    root = tmp_path / "examples"
    example = make_project(root / "a", extra=_DOCKER)
    failures = cde.check(example, root, otto=_fake_otto(tmp_path, fail_on="compose up"))
    assert failures == ["a: `otto -n docker compose up uc` failed (exit 3: )"]


def test_settings_that_do_not_validate_fail_in_one_line_without_their_values(tmp_path):
    root = tmp_path / "examples"
    bad = '[[docker.use_cases]]\nname = "uc"\ncomposes = ["undeclared"]\n'
    example = make_project(root / "a", extra=bad)
    [failure] = cde.check(example, root, otto=_fake_otto(tmp_path))
    assert failure.startswith("a: cannot load .otto/settings.toml: ")
    assert "undeclared" in failure
    assert "\n" not in failure
    assert "input_value" not in failure
