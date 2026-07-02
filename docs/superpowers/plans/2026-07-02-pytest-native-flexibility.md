# pytest-Native Flexibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repo-wide `conftest.py` support (confcutdir at repo root), suite auto-registration replacing `@register_suite()`, and suite-less test runs (`--tests a,b` / `-m` alone).

**Architecture:** Three surgical changes to the pytest-wrapping layer: (1) `run_suite` computes `--confcutdir` from the suite file's owning `Repo.sut_dir`; (2) `OttoSuite.__init_subclass__` calls the registration logic extracted from the deleted decorator; (3) a new `run_selection()` shares `run_suite`'s arg-assembly core, resolves `--tests` names → nodeids via the existing `collect_tests()` pass, and runs one pytest session per repo. Spec: `docs/superpowers/specs/2026-07-02-pytest-native-flexibility-design.md`.

**Tech Stack:** Python 3.10+, typer 0.26 (vendored click fork — never catch real `click.*`), pytest (in-process `pytest.main`), pydantic dataclasses (`@options`).

## Global Constraints

- Work on the dedicated worktree (created via superpowers:using-git-worktrees). Run `uv sync` first — fresh worktrees have no `.venv` and `ty`/docs gates show phantom errors without it.
- Committing on the worktree is allowed. Commit per task. End commit messages with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- NEVER `from __future__ import annotations` (breaks the Sphinx nitpicky docs gate). Real 3.10+ annotations, module-top imports.
- NEVER `git add -u` — always add exact paths.
- After any `ruff format`, re-run `ruff check .` (format is not lint-neutral). `nox` lint = `ruff check` + `ruff format --check`.
- Per-task gate: `make coverage` (single `-n auto` pass, never loops). `ty` runs only at `nox -s typecheck` — budget a typecheck round after src edits. Full gate before hand-off: `make coverage` + `nox` + typecheck + docs.
- Tests write only under `tmp_path` — never `Path(".")` or anything inside the repo.
- A new runtime host field would need a `models/host.py` mirror (drift guard) — this plan adds none.
- `otto` CLI subprocess tests: reuse the helpers in `tests/e2e/_otto_subprocess.py` (hostless infra) — read that file before writing e2e tests.

---

### Task 1: `--confcutdir` at the SUT repo root

**Files:**
- Modify: `src/otto/cli/test.py:257-287` (base_args in `run_suite`)
- Test: `tests/unit/cli/test_confcutdir.py` (new)
- Test: `tests/e2e/test_repo_wide_conftest.py` (new)

**Interfaces:**
- Produces: `_repo_confcutdir(suite_file: str, repos: list[Repo]) -> Path` in `src/otto/cli/test.py` — returns the owning repo's `sut_dir`, else `Path(suite_file).resolve().parent`. Task 4 reuses it.

- [ ] **Step 1: Write the failing unit test**

```python
# tests/unit/cli/test_confcutdir.py
"""_repo_confcutdir maps a suite file to its owning repo root."""

from pathlib import Path
from types import SimpleNamespace

from otto.cli.test import _repo_confcutdir


def test_file_inside_repo_maps_to_sut_dir(tmp_path: Path) -> None:
    repo = SimpleNamespace(sut_dir=tmp_path / "repo_a")
    suite = repo.sut_dir / "tests" / "sub" / "test_x.py"
    suite.parent.mkdir(parents=True)
    suite.touch()
    assert _repo_confcutdir(str(suite), [repo]) == repo.sut_dir  # type: ignore[arg-type]


def test_file_outside_all_repos_falls_back_to_parent(tmp_path: Path) -> None:
    repo = SimpleNamespace(sut_dir=tmp_path / "repo_a")
    stray = tmp_path / "elsewhere" / "test_y.py"
    stray.parent.mkdir(parents=True)
    stray.touch()
    assert _repo_confcutdir(str(stray), [repo]) == stray.parent  # type: ignore[arg-type]


def test_first_matching_repo_wins(tmp_path: Path) -> None:
    outer = SimpleNamespace(sut_dir=tmp_path)
    inner = SimpleNamespace(sut_dir=tmp_path / "nested")
    suite = inner.sut_dir / "tests" / "test_z.py"
    suite.parent.mkdir(parents=True)
    suite.touch()
    # repos are checked in order; list inner first for the tighter root
    assert _repo_confcutdir(str(suite), [inner, outer]) == inner.sut_dir  # type: ignore[arg-type]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/cli/test_confcutdir.py -v`
Expected: FAIL — `ImportError: cannot import name '_repo_confcutdir'`

- [ ] **Step 3: Implement the helper and switch base_args to it**

In `src/otto/cli/test.py`, add above `run_suite` (near `resolve_suite`, line ~181):

```python
def _repo_confcutdir(suite_file: str, repos: "list[Repo]") -> Path:
    """Root for pytest's --confcutdir: the suite file's owning repo.

    Cutting at the SUT repo root (the directory holding ``.otto/``) loads the
    user repo's FULL conftest hierarchy — root, ``tests/``, per-subdir — while
    still excluding otto's own ``tests/conftest.py`` for the in-tree example
    repos (it sits above ``tests/repoN/``). Fallback for a file outside every
    repo: the file's parent (the historical behavior).
    """
    resolved = Path(suite_file).resolve()
    for repo in repos:
        if resolved.is_relative_to(repo.sut_dir):
            return repo.sut_dir
    return resolved.parent
```

Then replace the confcutdir line in `base_args` (currently `f"--confcutdir={Path(suite_file).resolve().parent}",` at line 280, together with the comment block above it at 276-279):

```python
        # Cut conftest loading at the suite's repo root: the user repo's whole
        # conftest hierarchy loads; otto's own tests/conftest.py (which resets
        # logging management state) stays excluded for in-tree example repos
        # because it lives above their sut_dir.
        f"--confcutdir={_repo_confcutdir(suite_file, repos)}",
```

(`repos` is already in scope: `repos = get_repos()` at line 236.)

- [ ] **Step 4: Run the unit test — verify PASS**

Run: `uv run pytest tests/unit/cli/test_confcutdir.py -v`
Expected: 3 passed

- [ ] **Step 5: Write the failing e2e test (repo-root fixture reaches a subdir suite)**

First read `tests/e2e/_otto_subprocess.py` and one existing consumer (e.g. `grep -rl _otto_subprocess tests/e2e/ | head -3`) to copy the invocation idiom exactly. Then create a self-contained tmp repo — the suite lives in `tests/sub/` so its *parent* dir has no conftest; only the repo-root cut makes the fixture visible:

```python
# tests/e2e/test_repo_wide_conftest.py
"""otto test loads conftest.py from the repo root, not just the suite's dir."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hostless  # adjust to the marker _otto_subprocess consumers use

SETTINGS = """\
name = "confrepo"
version = "0.1.0"
tests = ["${sut_dir}/tests"]
"""

ROOT_CONFTEST = """\
import pytest

@pytest.fixture
def root_marker() -> str:
    return "from-repo-root"
"""

SUITE = """\
from otto.suite import OttoSuite


class TestConfcut(OttoSuite):
    async def test_sees_root_fixture(self, root_marker: str) -> None:
        assert root_marker == "from-repo-root"
"""


def _make_repo(root: Path) -> None:
    (root / ".otto").mkdir(parents=True)
    (root / ".otto" / "settings.toml").write_text(SETTINGS)
    (root / "conftest.py").write_text(ROOT_CONFTEST)
    sub = root / "tests" / "sub"
    sub.mkdir(parents=True)
    (sub / "test_confcut.py").write_text(SUITE)


def test_suite_in_subdir_sees_repo_root_fixture(tmp_path: Path) -> None:
    repo = tmp_path / "confrepo"
    _make_repo(repo)
    env = os.environ | {"OTTO_SUT_DIRS": str(repo), "OTTO_XDIR": str(tmp_path / "xdir")}
    result = subprocess.run(
        [sys.executable, "-m", "otto", "test", "TestConfcut"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

NOTE: the suite class carries **no** `@register_suite()` — if Task 2 has not landed yet in your worktree, add the decorator + import temporarily and remove it in Task 2's sweep. Adapt the invocation (`python -m otto` vs a console-script path, lab flags, markers) to whatever `tests/e2e/_otto_subprocess.py` consumers actually do — the helper exists precisely so e2e tests don't hand-roll this; prefer calling the helper over `subprocess.run` if it accepts an env override.

- [ ] **Step 6: Run e2e — verify it FAILS before your change / PASSES after**

Run: `git stash && uv run pytest tests/e2e/test_repo_wide_conftest.py -v; git stash pop`
Expected while stashed: FAIL — `fixture 'root_marker' not found` (old confcutdir = `tests/sub`). After `git stash pop` (change restored): PASS.

- [ ] **Step 7: Gate + commit**

Run: `make coverage`
Expected: green (repo1's own root conftest.py now loads in suite e2e runs — it is a sys.path shim and must stay harmless; if any e2e regresses here, that repo1 conftest interaction is the first suspect).

```bash
git add src/otto/cli/test.py tests/unit/cli/test_confcutdir.py tests/e2e/test_repo_wide_conftest.py
git commit -m "fix(test): cut conftest loading at the repo root, not the suite dir

Repo-wide conftest.py fixtures now reach suites in subdirectories, as the
OttoSuite docs always promised. otto's own tests/conftest.py stays excluded
for in-tree example repos (it sits above their sut_dir).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Auto-registration via `__init_subclass__`; delete `@register_suite()`

**Files:**
- Modify: `src/otto/suite/register.py` (extract `register_suite_class`, delete `register_suite`)
- Modify: `src/otto/suite/suite.py:39` (`OttoSuite.__init_subclass__`; docstring examples)
- Modify: `src/otto/suite/__init__.py`, possibly `src/otto/__init__.py` (drop the export — check with grep)
- Modify: every `@register_suite()` call site (sweep, Step 5)
- Test: `tests/unit/suite/test_auto_registration.py` (new)

**Interfaces:**
- Produces: `register_suite_class(suite_class: type) -> None` in `otto/suite/register.py` — module-level function with the exact body of today's inner `decorator()` minus the decorator shell and `*args/**kwargs` pass-through (the sub-app command is registered plainly: `sub_app.command(suite_class.__name__)(runner)`).
- Produces: `OttoSuite.__init_subclass__` — registers any subclass whose `__name__.startswith("Test")`.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write the failing unit tests**

Classes are defined *inside test functions* so otto's own pytest collection never sees them. Look at the existing decorator tests first (`grep -rln register_suite tests/unit/suite/`) and mirror their SUITES-isolation fixture (they must already snapshot/restore the registry; reuse that fixture, do not invent a new one).

```python
# tests/unit/suite/test_auto_registration.py
"""OttoSuite subclasses named Test* auto-register into SUITES."""

import pytest

from otto.suite import OttoSuite
from otto.suite.register import SUITES


def test_test_named_subclass_registers() -> None:
    class TestAutoReg(OttoSuite):
        async def test_something(self) -> None: ...

    assert "TestAutoReg" in SUITES
    assert SUITES.get("TestAutoReg").name == "TestAutoReg"


def test_non_test_named_base_does_not_register() -> None:
    class SharedSuiteBase(OttoSuite):
        pass

    assert "SharedSuiteBase" not in SUITES


def test_subclass_of_shared_base_registers() -> None:
    class BaseForReg(OttoSuite):
        pass

    class TestFromBase(BaseForReg):
        pass

    assert "BaseForReg" not in SUITES
    assert "TestFromBase" in SUITES


def test_options_inner_class_is_captured() -> None:
    from otto import options

    @options
    class _Opts:
        retries: int = 3

    class TestWithOpts(OttoSuite[_Opts]):
        Options = _Opts

    entry = SUITES.get("TestWithOpts")
    # the sub-app carries the synthesized --retries flag
    import typer.main

    cmd = typer.main.get_command(entry.sub_app)
    leaf = cmd.commands["TestWithOpts"] if hasattr(cmd, "commands") else cmd
    assert any("--retries" in (p.opts or []) for p in leaf.params)


def test_same_name_from_different_file_still_collides() -> None:
    class TestCollide(OttoSuite):
        pass

    # simulate a re-registration from a DIFFERENT file: entry.file differs
    import dataclasses

    from otto.suite.register import register_suite_class

    entry = SUITES.get("TestCollide")
    SUITES.register(
        "TestCollide",
        dataclasses.replace(entry, file="/somewhere/else/test_other.py"),
        origin="elsewhere",
        overwrite=True,
    )
    with pytest.raises(Exception, match="TestCollide"):
        register_suite_class(TestCollide)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/suite/test_auto_registration.py -v`
Expected: FAIL — `Test*` classes not in SUITES / `register_suite_class` not importable

- [ ] **Step 3: Implement**

`src/otto/suite/register.py` — replace `register_suite()` (lines 62-152) with a module-level function. The body is the *existing* decorator body verbatim except: no closure shell, no `*args/**kwargs` on the sub-app command, and the docstring below. Keep `SuiteEntry`, `SUITES`, `_options_params`, and the same-file-overwrite comment block exactly as they are:

```python
def register_suite_class(suite_class: type) -> None:
    """Register an OttoSuite subclass as an ``otto test`` subcommand.

    Called automatically by ``OttoSuite.__init_subclass__`` for every subclass
    whose name matches pytest's own collection rule (``Test*``). Builds a Typer
    sub-app from the class's ``Options`` inner class (if present) and registers
    it into :data:`SUITES`; ``cli/test.py``'s ``suite_app`` resolves it lazily
    by name through its ``RegistryBackedGroup``.
    """
    opts_cls = getattr(suite_class, "Options", None)
    suite_file = inspect.getfile(suite_class)
    ...  # lines 92-148 of the current file, unchanged, dedented one level;
    #     sub_app.command(suite_class.__name__)(runner) — no *args/**kwargs
```

`src/otto/suite/suite.py` — inside `class OttoSuite(Generic[TOptions])`, first method:

```python
    def __init_subclass__(cls, **kwargs: object) -> None:
        """Auto-register ``Test*``-named subclasses as ``otto test`` subcommands.

        Matches pytest's own ``python_classes = Test*`` collection rule, so a
        shared base class (``BaseSomething(OttoSuite)``) is naturally skipped.
        pytest re-imports a suite file under its own module name when a suite
        runs; the registry's same-file overwrite rule absorbs that re-fire.
        """
        super().__init_subclass__(**kwargs)
        if cls.__name__.startswith("Test"):
            from otto.suite.register import register_suite_class

            register_suite_class(cls)
```

(Local import: `suite.py` must stay importable without pulling typer at class-definition time only when no `Test*` subclass exists — and it avoids any module-cycle risk. `register.py` never imports `suite.py`; verify with `grep -n "import" src/otto/suite/register.py`.)

Also update the `OttoSuite` docstring (lines 42-46 and the two `@register_suite()` examples at lines 61-70 and 111-141): drop the decorator from both examples, and reword line 42-43 to "Subclass this with a ``Test*``-prefixed name and it is automatically registered as an ``otto test <ClassName>`` subcommand."

- [ ] **Step 4: Run the new unit tests — PASS**

Run: `uv run pytest tests/unit/suite/test_auto_registration.py -v`
Expected: 5 passed

- [ ] **Step 5: The deletion sweep**

Run: `grep -rn "register_suite" src/ tests/ docs/ --include="*.py" --include="*.md" --include="*.rst" -l`

For every hit: remove the `@register_suite()` line and its import; keep the class. Known sites (verify, the list may have grown): `src/otto/suite/__init__.py` (export), possibly `src/otto/__init__.py`, `tests/repo1/tests/test_device.py`, other `tests/repo*/tests/*.py` and `tests/repo_e2e/`, decorator unit tests under `tests/unit/suite/` (adapt to call `register_suite_class` or delete cases the new file already covers), `docs/guide/*.md`, `docs/getting-started.md`. The `SUITES` registry's `register_hint="@otto.register_suite()"` string (register.py:39) must change to `register_hint="subclass otto.suite.OttoSuite with a Test*-prefixed name"`.

Then: `grep -rn "register_suite" src/ tests/ docs/ | grep -v register_suite_class` — Expected: zero hits.

- [ ] **Step 6: Full unit + e2e gate**

Run: `make coverage`
Expected: green. Failures here are almost certainly a missed sweep site or a unit test that defined a module-level `Test*` OttoSuite subclass which now auto-registers — fix by moving such classes into function bodies or restoring the registry via the isolation fixture.

- [ ] **Step 7: Commit**

```bash
git add -A src/otto/suite src/otto/__init__.py tests/ docs/
git status --short   # review: ONLY intended files; never git add -u semantics beyond these paths
git commit -m "feat(suite): auto-register Test* OttoSuite subclasses; delete @register_suite

OttoSuite.__init_subclass__ now registers any Test*-named subclass (pytest's
own collection rule), so the decorator is boilerplate and is removed outright.
Registry keying, duplicate rules, per-suite Options synthesis, --list-suites,
and completion are unchanged mechanically.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `suite_options` becomes request-aware (default-construct per class)

**Files:**
- Modify: `src/otto/suite/pytest_plugin.py:26-29`
- Test: `tests/unit/suite/test_options_plugin.py` (new or extend existing — check `ls tests/unit/suite/`)

**Interfaces:**
- Consumes: `OttoOptionsPlugin(options)` constructor — unchanged.
- Produces: `suite_options` fixture, scope `"class"`: explicit instance if the plugin holds one, else per-class default construction of `request.cls.Options`, else `None`.

- [ ] **Step 1: Write the failing test (pytester-based)**

Check how `tests/unit/suite/test_plugin.py` drives inner pytest sessions and mirror it (it already solved the leaked-event-loop problem). Use pytest's `pytester` fixture:

```python
# tests/unit/suite/test_options_plugin.py
"""suite_options: CLI instance when provided, per-class defaults otherwise."""

import pytest

pytest_plugins = ["pytester"]

SUITE_SRC = """\
from typing import Annotated
import typer
from otto import options
from otto.suite import OttoSuite

@options
class _Defaulted:
    retries: Annotated[int, typer.Option(help="n")] = 3

class TestDefaulted(OttoSuite[_Defaulted]):
    Options = _Defaulted
    def test_gets_defaults(self, suite_options):
        assert suite_options.retries == 3

@options
class _Required:
    firmware: Annotated[str, typer.Option(help="fw")]

class TestRequired(OttoSuite[_Required]):
    Options = _Required
    def test_never_runs(self, suite_options):
        raise AssertionError("should have failed at fixture setup")
"""


def test_defaulted_options_are_constructed(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k", "TestDefaulted", "-p", "no:cacheprovider", plugins=[OttoOptionsPlugin(None)]
    )
    result.assert_outcomes(passed=1)


def test_required_options_fail_with_suite_hint(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k", "TestRequired", "-p", "no:cacheprovider", plugins=[OttoOptionsPlugin(None)]
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*required options*otto test TestRequired*"])


def test_explicit_instance_still_wins(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    class _Sentinel:
        retries = 99

    pytester.makepyfile(test_inner=SUITE_SRC.replace("== 3", "== 99"))
    result = pytester.runpytest_inprocess(
        "-k", "TestDefaulted", "-p", "no:cacheprovider", plugins=[OttoOptionsPlugin(_Sentinel())]
    )
    result.assert_outcomes(passed=1)
```

(If auto-registration (Task 2) makes the inner classes double-register through the pytester import, wrap the inner file's SUITES state with the same isolation fixture used in Task 2's tests — same-file overwrite should already absorb it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/suite/test_options_plugin.py -v`
Expected: `test_defaulted_options_are_constructed` FAILS (fixture returns None → `None.retries` AttributeError inside the inner run)

- [ ] **Step 3: Implement**

Replace the fixture in `src/otto/suite/pytest_plugin.py` (lines 26-29):

```python
    @pytest.fixture(scope="class")
    def suite_options(self, request: pytest.FixtureRequest) -> Any:
        """The suite's Options instance.

        Single-suite runs (``otto test <SuiteName> --flags``) pass the
        CLI-built instance in — returned as-is. Selection runs
        (``otto test --tests ...`` / ``-m ...``) span suites, so each suite's
        ``Options`` is default-constructed once per class; required fields
        make the suite's tests fail with a pointer at the single-suite form.
        """
        if self.options is not None:
            return self.options
        cls = getattr(request, "cls", None)
        opts_cls = getattr(cls, "Options", None) if cls is not None else None
        if opts_cls is None:
            return None
        try:
            return opts_cls()
        except Exception as exc:  # pydantic ValidationError, TypeError, ...
            pytest.fail(
                f"suite {cls.__name__!r} has required options — "
                f"run `otto test {cls.__name__} ...` to pass them ({exc})",
                pytrace=False,
            )
```

(Scope session→class is a pure narrowing: function-scoped requesters are unaffected.)

- [ ] **Step 4: Run — PASS, then gate + commit**

Run: `uv run pytest tests/unit/suite/test_options_plugin.py -v` → 3 passed. Then `make coverage` → green.

```bash
git add src/otto/suite/pytest_plugin.py tests/unit/suite/test_options_plugin.py
git commit -m "feat(suite): default-construct suite Options per class in multi-suite runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Selection runs — `--tests a,b`, `-m` alone, per-repo sessions

**Files:**
- Modify: `src/otto/cli/test.py` — `TestRunOptions`, `run_suite` (extract shared core), new `_resolve_selection` + `run_selection`, callback (`--tests` option; wiring at lines 680-683)
- Test: `tests/unit/cli/test_selection_resolve.py` (new)
- Test: `tests/e2e/test_selection_runs.py` (new)

**Interfaces:**
- Consumes: `_repo_confcutdir` (Task 1); `Repo.collect_tests(markers=, suite=, tests=)` returning `CollectedTest(nodeid, name, path, cls_name)`; `command_preamble` / `CLI_COMMANDS` from `cli/invoke.py` / `cli/registry.py`.
- Produces:
  - `TestRunOptions.tests: str = ""` (new field).
  - `_resolve_selection(repos: list[Repo], names: list[str], markers: str) -> list[tuple[Repo, list[str]]]` — per-repo nodeid lists, repos with no matches omitted; raises `typer.BadParameter` listing unknown names with did-you-mean suggestions.
  - `run_selection(ctx: typer.Context) -> None` — reads `TestRunOptions` from `ctx.meta`, runs one pytest session per matched repo, exits with the worst return code.

- [ ] **Step 1: Write the failing resolution unit test**

```python
# tests/unit/cli/test_selection_resolve.py
"""--tests name resolution: exact names, Class::name, did-you-mean."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from otto.cli.test import _resolve_selection


def _repo_with(collected: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        name="fixture-repo",
        collect_tests=lambda markers=None, suite=None, tests=None: collected,
    )


def _item(nodeid: str, name: str, cls_name: str | None) -> SimpleNamespace:
    return SimpleNamespace(nodeid=nodeid, name=name, path=Path("t.py"), cls_name=cls_name)


ITEMS = [
    _item("tests/t.py::TestA::test_login", "test_login", "TestA"),
    _item("tests/t.py::TestB::test_login", "test_login", "TestB"),
    _item("tests/t.py::test_plain", "test_plain", None),
    _item("tests/t.py::TestA::test_param[a]", "test_param[a]", "TestA"),
    _item("tests/t.py::TestA::test_param[b]", "test_param[b]", "TestA"),
]


def test_bare_name_matches_every_suite() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_login"], "")
    assert nodeids == ["tests/t.py::TestA::test_login", "tests/t.py::TestB::test_login"]


def test_bare_name_matches_all_parametrizations() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_param"], "")
    assert nodeids == ["tests/t.py::TestA::test_param[a]", "tests/t.py::TestA::test_param[b]"]


def test_plain_function_is_selectable() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["test_plain"], "")
    assert nodeids == ["tests/t.py::test_plain"]


def test_qualified_name_disambiguates() -> None:
    [(_, nodeids)] = _resolve_selection([_repo_with(ITEMS)], ["TestB::test_login"], "")
    assert nodeids == ["tests/t.py::TestB::test_login"]


def test_unknown_name_raises_with_suggestion() -> None:
    with pytest.raises(typer.BadParameter, match="test_login"):
        _resolve_selection([_repo_with(ITEMS)], ["test_logon"], "")


def test_repo_without_matches_is_omitted() -> None:
    empty = _repo_with([])
    full = _repo_with(ITEMS)
    resolved = _resolve_selection([empty, full], ["test_plain"], "")
    assert len(resolved) == 1 and resolved[0][0] is full
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/cli/test_selection_resolve.py -v`
Expected: FAIL — `_resolve_selection` not defined

- [ ] **Step 3: Implement resolution + the run path**

In `src/otto/cli/test.py`:

(a) `TestRunOptions` gains `tests: str = ""` (after `markers`, line 158).

(b) Resolution (place after `_repo_confcutdir`):

```python
def _base_test_name(name: str) -> str:
    """``test_param[a-b]`` → ``test_param`` (parametrization-insensitive match)."""
    return name.partition("[")[0]


def _resolve_selection(
    repos: "list[Repo]", names: list[str], markers: str
) -> "list[tuple[Repo, list[str]]]":
    """Resolve --tests names to exact nodeids, one entry per matching repo.

    A bare name matches every collected test with that function name (all
    parametrizations); ``Class::name`` restricts to one suite. Unknown names
    raise ``typer.BadParameter`` with did-you-mean suggestions — never a
    silent empty run.
    """
    import difflib

    per_repo: list[tuple[Repo, list[str]]] = []
    matched: set[str] = set()
    seen_names: set[str] = set()
    for repo in repos:
        items = repo.collect_tests(markers=markers or None)
        nodeids: list[str] = []
        for item in items:
            base = _base_test_name(item.name)
            seen_names.add(base)
            if item.cls_name:
                seen_names.add(f"{item.cls_name}::{base}")
            for wanted in names:
                cls_part, _, name_part = wanted.rpartition("::")
                if base == name_part and (not cls_part or item.cls_name == cls_part):
                    nodeids.append(item.nodeid)
                    matched.add(wanted)
                    break
        if nodeids:
            per_repo.append((repo, nodeids))

    unknown = [n for n in names if n not in matched]
    if unknown:
        hints = []
        for n in unknown:
            close = difflib.get_close_matches(n, sorted(seen_names), n=3)
            hint = f" (did you mean: {', '.join(close)}?)" if close else ""
            hints.append(f"{n!r}{hint}")
        raise typer.BadParameter(
            f"no collected test matches: {'; '.join(hints)}", param_hint="--tests"
        )
    return per_repo
```

(c) Extract the session core from `run_suite`. Split the current body so both paths share it — signature:

```python
def _run_pytest_session(
    targets: list[str],
    keyword: str | None,
    confcutdir: Path,
    opts: TestRunOptions,
    opts_instance: object | None,
    results_path: str,
    sut_test_dirs: list[Path],
    log_dir: Path,
    label: str,
) -> int:
    """One inner pytest session: base args + plugins + stability report. Returns rc."""
```

Move lines 257-322 of `run_suite` into it: `base_args` starts from `[*targets]`, adds `["-k", keyword]` only when `keyword`, uses `f"--confcutdir={confcutdir}"`, and the stability block passes `label` (was `suite_class.__name__`) to `_print_stability_report`. `run_suite` becomes: unpack `TestRunOptions` → cov-clean block (lines 244-255, unchanged, shared: move it into a small `_pre_run_cov_clean(repos, opts)` helper) → `rc = _run_pytest_session([suite_file], suite_class.__name__, _repo_confcutdir(suite_file, repos), opts, opts_instance, results_path, sut_test_dirs, log_dir, suite_class.__name__)` → post-run cov/report block (lines 324-349, move into `_post_run_coverage(repos, log_dir, opts)`) → the rc!=0 exit (lines 351-358).

(d) The selection entry point:

```python
def run_selection(ctx: typer.Context) -> None:
    """Run a suite-less selection (--tests and/or -m) — one session per repo."""
    stored = ctx.meta.get(RUN_OPTIONS_KEY)
    opts = stored if isinstance(stored, TestRunOptions) else TestRunOptions()

    repos = get_repos()
    names = [n.strip() for n in opts.tests.split(",") if n.strip()]
    if names:
        per_repo = _resolve_selection(repos, names, opts.markers)
    else:  # -m alone: marker expression over each repo's test dirs
        per_repo = [(r, [str(d) for d in r.tests if d.exists()]) for r in repos]
        per_repo = [(r, t) for r, t in per_repo if t]

    if not per_repo:
        rprint("[red]No tests matched the selection.[/red]")
        raise typer.Exit(code=1)

    _log_dir = get_context().output_dir
    if _log_dir is None:
        raise RuntimeError("output_dir is not set; command_preamble must run before run_selection")
    log_dir: Path = _log_dir
    _pre_run_cov_clean(repos, opts)

    worst = 0
    multi = len(per_repo) > 1
    for repo, targets in per_repo:
        default_junit = log_dir / (f"junit_{repo.name}.xml" if multi else "junit.xml")
        results_path = opts.results or str(default_junit)
        sut_test_dirs = [p for r in repos for p in r.tests]
        rc = _run_pytest_session(
            targets,
            None,
            repo.sut_dir,
            opts,
            None,  # no per-suite Options instance: Task 3's fixture default-constructs
            results_path,
            sut_test_dirs,
            log_dir,
            label=f"selection:{repo.name}",
        )
        worst = max(worst, int(rc))

    _post_run_coverage(repos, log_dir, opts)
    if worst != 0:
        raise typer.Exit(code=worst)
```

(e) Callback: add the option (after `markers`, line 485):

```python
    tests: Annotated[
        str,
        typer.Option(
            "--tests",
            metavar="NAME[,NAME...]",
            help=(
                "Run specific tests by exact name, across all suites and repos — "
                "no suite subcommand needed. Comma-separated; TestClass::name "
                "disambiguates. Combine with --markers to narrow."
            ),
        ),
    ] = "",
```

Thread `tests=tests` into the `TestRunOptions(...)` construction (line 659). Replace the Phase-1 block (lines 680-683):

```python
    if ctx.invoked_subcommand is None:
        if tests or markers:
            # The group callback is not a wrapped leaf, so the leaf-invoke
            # preamble (session/lab/output-dir/gate) has not run — stamp the
            # `test` spec and run it here before executing the selection.
            from .invoke import command_preamble
            from .registry import CLI_COMMANDS

            ctx.meta.setdefault("_otto_command_spec", CLI_COMMANDS.get("test"))
            command_preamble(ctx)
            run_selection(ctx)
            raise typer.Exit
        rprint(ctx.get_help())
        raise typer.Exit
```

Also update `--markers` help (line 483) to mention it runs suite-less when no suite is named, and the module docstring (lines 19-47 + examples at 102-119) with the new forms: `otto test --tests test_login`, `otto test --tests TestB::test_login,test_plain`, `otto test -m slow`.

- [ ] **Step 4: Run resolution tests — PASS**

Run: `uv run pytest tests/unit/cli/test_selection_resolve.py tests/unit/cli/ -v`
Expected: new tests pass; existing `cli` unit tests still green (run_suite refactor is behavior-preserving).

- [ ] **Step 5: Write the e2e tests**

Same tmp-repo idiom as Task 1 Step 5 (factor a shared `_make_repo` helper into `tests/e2e/_selection_fixtures.py` if both files want it). Fixture repo content — two suites sharing a marker + a plain function; `SUITE_SRC`:

```python
SUITE_SRC = """\
import pytest
from otto.suite import OttoSuite


class TestAlpha(OttoSuite):
    @pytest.mark.shared
    async def test_alpha_one(self) -> None:
        assert True

    async def test_alpha_two(self) -> None:
        assert True


class TestBeta(OttoSuite):
    @pytest.mark.shared
    async def test_beta_one(self) -> None:
        assert True


def test_plain_function() -> None:
    assert True
"""
```

(`pytest.ini`-style marker registration: add `markers = ["shared"]`-equivalent via a repo `pyproject.toml` or conftest `pytest_configure` — mirror whatever the repo1 fixtures do; check `grep -rn "markers" tests/repo1/`.)

Test cases (each a subprocess run; assert on the JUnit XML written to `OTTO_XDIR` — parse with `xml.etree`, count `<testcase>` elements):

```python
def test_tests_flag_runs_named_tests_across_suites(...):
    # otto test --tests test_alpha_one,test_beta_one → rc 0, junit has exactly 2 testcases

def test_plain_function_runs_via_tests_flag(...):
    # otto test --tests test_plain_function → rc 0, 1 testcase

def test_qualified_name_selects_one_suite(...):
    # otto test --tests TestAlpha::test_alpha_one → 1 testcase

def test_marker_alone_runs_both_suites(...):
    # otto test -m shared → 2 testcases (one per suite)

def test_unknown_name_is_loud_with_suggestion(...):
    # otto test --tests test_alpha_won → rc != 0, stderr/stdout contains "did you mean" and "test_alpha_one"

def test_bare_otto_test_still_shows_help(...):
    # otto test → rc 0, output contains "Usage", junit NOT written

def test_stability_mode_works_on_selection(...):
    # otto test -i 2 --tests test_plain_function → rc 0, "Stability Results" in output

def test_multi_repo_selection_runs_one_session_per_repo(...):
    # two tmp repos in OTTO_SUT_DIRS (comma-separated), each with a test_plain_function;
    # otto test --tests test_plain_function → rc 0, junit_<repoA>.xml AND junit_<repoB>.xml
    # both exist in OTTO_XDIR's run dir with 1 testcase each

def test_multi_repo_worst_exit_code_wins(...):
    # same two repos but repo B's function asserts False →
    # rc != 0, repo A's junit still written (sessions run sequentially)
```

- [ ] **Step 6: Run e2e — iterate to green**

Run: `uv run pytest tests/e2e/test_selection_runs.py -v`
Expected: all pass. Most likely first failure: the preamble path (missing `_otto_root_options` when the callback runs) — that only happens if the root callback didn't run, i.e. only in direct-`suite_app` CliRunner invocations, which these subprocess tests avoid by design.

- [ ] **Step 7: Gate + commit**

Run: `make coverage`, then `uv run nox -s typecheck` (three tasks of src edits accumulated — fix ty findings now).

```bash
git add src/otto/cli/test.py tests/unit/cli/test_selection_resolve.py tests/e2e/test_selection_runs.py tests/e2e/_selection_fixtures.py
git commit -m "feat(test): suite-less selection runs — --tests names and -m alone

otto test --tests a,b resolves exact test names (Class::name to
disambiguate, did-you-mean on unknowns) via the collection pass and runs
them across suites and repos, one pytest session per repo with confcutdir
at that repo's root. -m EXPR with no suite runs the marker selection the
same way. Plain pytest functions are now first-class runnable.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Docs + completion sanity + full gate

**Files:**
- Modify: `docs/guide/test.md`, `docs/guide/repo-setup.md`, `docs/getting-started.md`, `docs/guide/cli-reference.md` (check which document `otto test`: `grep -rln "list-suites\|register_suite" docs/`)
- Test: extend `tests/unit/configmodule/` completion-cache test (locate: `grep -rln collect_current_commands tests/unit/`)

**Interfaces:** none new.

- [ ] **Step 1: Completion-cache regression test** — assert an auto-registered suite (no decorator) appears in `collect_current_commands()` output with its options serialized. Extend the existing completion-cache unit test file with one case: define a `Test*` OttoSuite subclass (inside the test function, registry-isolated), call `collect_current_commands()`, assert the suite name + option kinds appear. Run it; it should pass without code changes (the cache reads `SUITES`) — it exists to pin that.

- [ ] **Step 2: Docs sweep** — update every page found by the Step "Files" grep: selection-run syntax (`--tests NAME[,NAME...]`, `-m` alone), the options rule ("suite-specific options need the suite subcommand; selection runs use each suite's defaults"), the `tests` settings key described as **"defines where test discovery happens"** (repo-setup.md), decorator-less suite examples everywhere. Keep MyST xrefs intact — grep the whole tree for links to any anchor you rename.

- [ ] **Step 3: Full gate**

Run: `make coverage && uv run nox && uv run nox -s typecheck && make docs`
Expected: all green. (`make docs` must be checked by exit code, not eyeballed tail output.)

- [ ] **Step 4: Commit**

```bash
git add docs/ tests/
git commit -m "docs(test): selection-run syntax, discovery-scope tests key, decorator-less suites

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes (already applied)

- Spec §2.1→Task 1, §2.2→Task 2, §2.4→Task 3, §2.3→Task 4, §5+cache→Task 5. Spec §3 error handling: zero-match exit (Task 4d `run_selection`), options failure (Task 3), loud collection failure (pre-existing Phase 1 behavior, exercised by Task 4 e2e).
- Type consistency: `_repo_confcutdir(str, list[Repo]) -> Path` used in Tasks 1 and 4c; `_resolve_selection` returns `list[tuple[Repo, list[str]]]` consumed by `run_selection`; `TestRunOptions.tests: str` written by callback (4e), read by `run_selection` (4d).
- Known intentional deferral: `--list-tests` does not honor `--tests` narrowing (spec §2.5 YAGNI).
