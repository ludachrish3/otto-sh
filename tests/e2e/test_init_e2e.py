"""``otto init --all`` produces a repo that otto immediately accepts end-to-end.

Runs the real ``otto`` binary via the shared subprocess harness
(:mod:`tests.e2e._otto_subprocess`) against a freshly scaffolded repo — no
mocking, no hand-authored fixture repo. This is the durable proof that the
scaffolded settings.toml / hosts.json / test suite / instructions module are
all mutually consistent with what otto's bootstrap actually expects, closing
out narrative-only "it works" claims from earlier tasks.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless


def test_init_then_full_verification_flow(tmp_path: Path) -> None:
    """``otto init --all`` then every command its own "Next steps" banner suggests."""
    repo = tmp_path / "widget"
    repo.mkdir()
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    # otto init is lab_free and repo-free: it operates purely on --path, never
    # on OTTO_SUT_DIRS/get_repos, so no `lab=` / `sut_dirs=` is needed here.
    r = run_otto(["init", "--all", "--name", "widget", "--path", str(repo)], xdir=xdir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "settings.toml" not in r.stderr  # sanity: no accidental error path

    # Every subsequent command needs OTTO_SUT_DIRS pointing at the scaffolded
    # repo (init itself needed none) and --lab (otto test / otto run are NOT
    # lab_free — see otto.cli.invoke.ensure_lab_context — even though the
    # scaffolded suite/instruction never touch a real host).
    r = run_otto(["test", "--list-suites"], xdir=xdir, sut_dirs=repo, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TestExample" in r.stdout, r.stdout + r.stderr

    r = run_otto(["--lab", "example_lab", "--list-hosts"], xdir=xdir, sut_dirs=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "example-device" in r.stdout, r.stdout + r.stderr

    # Hostless suite exercised via the repo-root conftest.py fixture
    # (repo_marker) — proves the scaffolded conftest is discovered, not just
    # present on disk.
    r = run_otto(["test", "TestExample"], xdir=xdir, sut_dirs=repo, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr

    r = run_otto(
        ["test", "--tests", "test_example_function"], xdir=xdir, sut_dirs=repo, lab="example_lab"
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # Closes the gap left by Task B2: the scaffolded pylib/<name>_instructions
    # module registers a `smoke` instruction via @instruction() — assert it is
    # actually importable and runnable through `otto run`, not just present on
    # disk. `otto run` is not lab_free either, so --lab is required here too.
    r = run_otto(["run", "smoke"], xdir=xdir, sut_dirs=repo, lab="example_lab")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hello from widget" in (r.stdout + r.stderr)
