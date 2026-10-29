"""``otto init --all`` produces a project that otto immediately accepts end-to-end.

Runs the real ``otto`` binary via the shared subprocess harness
(:mod:`tests.e2e._otto_subprocess`) against a freshly scaffolded project — no
mocking, no hand-authored fixture project. This is the durable proof that the
scaffolded settings.toml / lab.json / tests / instructions module are
all mutually consistent with what otto's bootstrap actually expects, closing
out narrative-only "it works" claims from earlier tasks.
"""

from pathlib import Path

import pytest

from otto import layout
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless


def test_init_then_full_verification_flow(tmp_path: Path) -> None:
    """``otto init --all`` then every command its own "Next steps" panel suggests."""
    project = tmp_path / "widget"
    project.mkdir()
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    # otto init is lab_free and project-free: it operates purely on --path, never
    # on OTTOPATH/get_projects, so no `lab=` / `roots=` is needed here.
    r = run_otto(["init", "--all", "--name", "widget", "--path", str(project)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "settings.toml" not in r.stderr  # sanity: no accidental error path

    # Every subsequent command needs OTTOPATH pointing at the scaffolded
    # project (init itself needed none) and --lab (otto test / otto run are NOT
    # lab_free — see otto.cli.invoke.ensure_lab_context — even though the
    # scaffolded tests/instruction never touch a real host).
    r = run_otto(["test", "--list-tests"], xdir=xdir, roots=project, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TestExample" in r.stdout, r.stdout + r.stderr

    r = run_otto(["--lab", "example_lab", "--list-hosts"], xdir=xdir, roots=project)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "example-device" in r.stdout, r.stdout + r.stderr

    # Hostless tests exercised via the project-root conftest.py fixture
    # (project_marker) — proves the scaffolded conftest is discovered, not just
    # present on disk.
    r = run_otto(["test", "TestExample"], xdir=xdir, roots=project, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    # The first run a new user sees must not open with pytest deprecation
    # blocks from otto's OWN fixtures (pytest 10 turns them into errors).
    assert "PytestRemovedIn10Warning" not in (r.stdout + r.stderr), r.stdout + r.stderr

    r = run_otto(["test", "test_example_function"], xdir=xdir, roots=project, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr

    # Closes the gap left by Task B2: the scaffolded pylib/<name>_instructions
    # module registers a `smoke` instruction via @instruction() — assert it is
    # actually importable and runnable through `otto run`, not just present on
    # disk. `otto run` is not lab_free either, so --lab is required here too.
    r = run_otto(["run", "smoke"], xdir=xdir, roots=project, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hello from widget" in (r.stdout + r.stderr)

    # The project-wide ProjectOptions flag rides BOTH verbs (the init module
    # registers it for run and test): an unknown flag would exit 2 at parse
    # time, and the test writes the value it read via ctx.options into its
    # test_dir, so the flag demonstrably reached the test.
    r = run_otto(
        ["test", "TestExample", "--message", "hi-from-e2e"],
        xdir=xdir,
        roots=project,
        lab="example_lab",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    notes = sorted(
        layout.command_output_dir(xdir, "test").glob(
            "*/test_example/TestExample/test_expect_and_artifacts/message.txt"
        )
    )
    assert notes, sorted(
        p.relative_to(xdir) for p in layout.command_output_dir(xdir, "test").rglob("*.txt")
    )
    assert notes[-1].read_text() == "hi-from-e2e"

    r = run_otto(
        ["run", "smoke", "--message", "hi-from-e2e"], xdir=xdir, roots=project, lab="example_lab"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hi-from-e2e" in (r.stdout + r.stderr)

    r = run_otto(["run", "smoke", "--loud"], xdir=xdir, roots=project, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HELLO FROM WIDGET" in (r.stdout + r.stderr)

    # Second init run: everything detected, doctor green (exit 0) — including
    # schema freshness, since the same otto generated them moments ago.
    r = run_otto(["init", "--all", "--name", "widget", "--path", str(project)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr

    # The scaffolded lab.json carries $schema and still loads (tolerance is
    # in the runtime loader, proven by --list-hosts above; sanity-check disk).
    assert (project / ".otto" / "schemas" / "lab.schema.json").is_file()
    assert (project / ".vscode" / "settings.json").is_file()


@pytest.mark.parametrize("area", ["--tests", "--instructions"])
def test_a_single_area_scaffold_runs(tmp_path: Path, area: str) -> None:
    """Every ``otto init`` flag alone leaves a project whose sample runs.

    The example tests read ``ProjectOptions`` through ``ctx.options``, which only
    the instructions (init) module registers, so ``--tests`` scaffolds that
    module too. A run needs a lab, which neither area writes, so ``--lab`` is
    scaffolded afterwards as a separate step — it adds no code.
    """
    project = tmp_path / "widget"
    project.mkdir()
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    r = run_otto(["init", area, "--name", "widget", "--path", str(project)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (project / "pylib" / "widget_instructions" / "__init__.py").is_file()
    r = run_otto(["init", "--lab", "--path", str(project)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr

    if area == "--tests":
        r = run_otto(
            ["test", "TestExample", "--message", "tests-alone"],
            xdir=xdir,
            roots=project,
            lab="example_lab",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        [note] = layout.command_output_dir(xdir, "test").glob(
            "*/test_example/TestExample/test_expect_and_artifacts/message.txt"
        )
        assert note.read_text() == "tests-alone"
    else:
        assert not (project / "tests" / "test_example.py").exists()
        r = run_otto(
            ["run", "smoke", "--message", "instructions-alone"],
            xdir=xdir,
            roots=project,
            lab="example_lab",
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "instructions-alone" in (r.stdout + r.stderr)
