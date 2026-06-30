# Selection-Based `otto test` — Phase 1 (Listing + `--list-suites` Bugfix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `otto test` list suites from the suite registry (fixing the broken `--list-suites`), harden the test-collection path, and add a selector-aware `--list-tests` (plus optional `--list-markers`), all with real non-mocked tests.

**Architecture:** `--list-suites` switches from a fragile inner-pytest collection to the authoritative `_SUITE_REGISTRY` (with a companion name→file map for per-repo attribution). `Repo.collect_tests()` — still needed by `--list-tests` — is hardened against three latent defects and gains `markers`/`suite`/`tests` selector params. `--list-tests` is a list-mode flag on the `otto test` callback that collects the resolved selection and exits without running.

**Tech Stack:** Python 3.10+, Typer/click, pytest (inner `pytest.main()` for collection), Rich panels, pydantic dataclasses.

## Global Constraints

- Python **3.10+** real annotations only. **Never** add `from __future__ import annotations` (trips the Sphinx-nitpicky `-W` docs gate).
- **Stage only — do NOT commit.** Each task's "Commit" step means *stage* (`git add`) and provide the message; the human runs `git commit`. (The prepare-commit-msg hook needs `/dev/tty`.)
- Per-task gate: `make coverage` (there is no `make test`). Full gate before hand-off: `make coverage` + `make typecheck` + `make docs` + `nox`. Single `-n auto` passes only — **never** looped/over-subscribed xdist on this VM.
- Lint is `select=["ALL"]` minus a deny-list; prefer enforce-and-fix over `noqa`. Run `ruff check .` (covers `scripts/`+`docs/`) and re-run after any `ruff format`.
- Reproduce/run the CLI locally with `source ./project_env` (sets `OTTO_LAB=veggies`, `OTTO_SUT_DIRS=tests/repo1,tests/repo2`). The binary is `.venv/bin/otto`.
- Test tiers: `tests/unit` (no marker), `tests/integration`, `tests/e2e`. Listing tests are unit/integration (no live bed).

---

## Task 1: Suite-file attribution (`_SUITE_FILES` + `Repo.registered_suites()`)

Give the suite registry enough information to attribute each registered suite to a repo, **without** changing the `(name, sub_app)` tuple shape (12+ consumers unpack it as a 2-tuple).

**Files:**
- Modify: `src/otto/suite/register.py` (registry section ~L24-27; decorator append ~L117)
- Modify: `src/otto/configmodule/repo.py` (add `registered_suites()` method to `Repo`)
- Test: `tests/unit/suite/test_register.py` (companion map), `tests/unit/cli/test_listing.py` (`registered_suites`)

**Interfaces:**
- Produces: `otto.suite.register._SUITE_FILES: dict[str, str]` (suite class name → absolute source file). Populated in `register_suite`'s decorator alongside `_SUITE_REGISTRY.append(...)`.
- Produces: `Repo.registered_suites() -> list[str]` — names of registered suites whose source file resolves under `self.sut_dir`, in registry order.

- [ ] **Step 1: Write the failing test for the companion map**

In `tests/unit/suite/test_register.py`, add to the existing suite-registration test class (uses the existing `register_suite` import + the registry-snapshot fixture pattern already in that file):

```python
def test_register_suite_records_source_file(self):
    from otto.suite.register import _SUITE_FILES, register_suite

    @register_suite()
    class _SuiteFileProbe:
        pass

    assert _SUITE_FILES["_SuiteFileProbe"] == __file__
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/suite/test_register.py::*::test_register_suite_records_source_file -v`
Expected: FAIL — `ImportError`/`AttributeError`: `_SUITE_FILES` does not exist.

- [ ] **Step 3: Add the companion map in `register.py`**

At `src/otto/suite/register.py` registry section (after L27):

```python
_SUITE_REGISTRY: list[tuple[str, typer.Typer]] = []

# Companion map: suite class name -> absolute source file. Populated alongside
# _SUITE_REGISTRY so suites can be attributed to a repo (by sut_dir) for
# `otto test --list-suites` without changing the (name, sub_app) tuple shape
# that many consumers unpack.
_SUITE_FILES: dict[str, str] = {}
```

In the decorator, change the append (currently `_SUITE_REGISTRY.append((suite_class.__name__, sub_app))`) to also record the file (`suite_file` is already computed at the top of `decorator`):

```python
        _SUITE_REGISTRY.append((suite_class.__name__, sub_app))
        _SUITE_FILES[suite_class.__name__] = suite_file
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `.venv/bin/python -m pytest tests/unit/suite/test_register.py::*::test_register_suite_records_source_file -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `Repo.registered_suites()`**

In `tests/unit/cli/test_listing.py` (helpers `_make_sut` already exist), add:

```python
class TestRegisteredSuites:
    def test_attributes_suites_under_sut_dir(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        suite_file = str((sut_dir / "tests" / "test_thing.py").resolve())
        # Seed the registry + companion map directly (no real import needed).
        reg._SUITE_REGISTRY.append(("TestThing", __import__("typer").Typer()))
        reg._SUITE_FILES["TestThing"] = suite_file
        try:
            assert repo.registered_suites() == ["TestThing"]
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "TestThing"]
            reg._SUITE_FILES.pop("TestThing", None)

    def test_excludes_suites_outside_sut_dir(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        reg._SUITE_REGISTRY.append(("Foreign", __import__("typer").Typer()))
        reg._SUITE_FILES["Foreign"] = str((tmp_path / "other" / "test_x.py").resolve())
        try:
            assert repo.registered_suites() == []
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "Foreign"]
            reg._SUITE_FILES.pop("Foreign", None)
```

- [ ] **Step 6: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestRegisteredSuites -v`
Expected: FAIL — `AttributeError: 'Repo' object has no attribute 'registered_suites'`.

- [ ] **Step 7: Implement `Repo.registered_suites()`**

In `src/otto/configmodule/repo.py`, add this method to `Repo` (next to `get_test_suites_panel`). Note `self.sut_dir` is a `Path`:

```python
    def registered_suites(self) -> list[str]:
        """Names of ``@register_suite`` suites whose source file is under this repo.

        Reads ``otto.suite.register._SUITE_FILES`` (populated at suite import
        time) and returns the registered suite names — the exact subcommand
        names ``otto test <name>`` accepts — for suites defined under this
        repo's ``sut_dir``, preserving registration order.
        """
        from ..suite.register import _SUITE_FILES, _SUITE_REGISTRY

        sut_root = self.sut_dir.resolve()
        names: list[str] = []
        for name, _sub_app in _SUITE_REGISTRY:
            src = _SUITE_FILES.get(name)
            if src is None:
                continue
            try:
                Path(src).resolve().relative_to(sut_root)
            except ValueError:
                continue
            names.append(name)
        return names
```

- [ ] **Step 8: Run the tests to confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestRegisteredSuites tests/unit/suite/test_register.py -v`
Expected: PASS

- [ ] **Step 9: Stage**

```bash
git add src/otto/suite/register.py src/otto/configmodule/repo.py tests/unit/suite/test_register.py tests/unit/cli/test_listing.py
# commit message (human runs git commit):
# feat(test): record suite source files + Repo.registered_suites() for registry-based listing
```

---

## Task 2: `--list-suites` from the registry (no collection)

Switch suite listing off `collect_tests()` onto `registered_suites()`. This eliminates bugs A/B/C on the `--list-suites` path.

**Files:**
- Modify: `src/otto/configmodule/repo.py` (`get_test_suites_panel` ~L409-431)
- Modify: `src/otto/cli/test.py` (`list_suites_callback` ~L396-401)
- Test: `tests/unit/cli/test_listing.py` (`TestGetTestSuitesPanel`, `TestListCallbacks`)

**Interfaces:**
- Consumes: `Repo.registered_suites()` (Task 1).
- Produces: `Repo.get_test_suites_panel() -> Panel` — **no `items` argument** now; renders registry suite names. `list_suites_callback` calls it without collecting.

- [ ] **Step 1: Update the panel test to the registry-driven signature**

Replace `TestGetTestSuitesPanel` in `tests/unit/cli/test_listing.py` with a version that seeds the registry and calls the no-arg panel:

```python
class TestGetTestSuitesPanel:
    def _seed(self, sut_dir, *names):
        from otto.suite import register as reg

        for n in names:
            reg._SUITE_REGISTRY.append((n, __import__("typer").Typer()))
            reg._SUITE_FILES[n] = str((sut_dir / "tests" / f"{n}.py").resolve())

    def _cleanup(self, *names):
        from otto.suite import register as reg

        reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] not in names]
        for n in names:
            reg._SUITE_FILES.pop(n, None)

    def test_lists_registered_suite_names(self, tmp_path):
        sut_dir = _make_sut(tmp_path)
        repo = Repo(sut_dir=sut_dir)
        self._seed(sut_dir, "TestAlpha", "TestBeta")
        try:
            text = _render(repo.get_test_suites_panel())
        finally:
            self._cleanup("TestAlpha", "TestBeta")
        assert "TestAlpha" in text
        assert "TestBeta" in text

    def test_empty_when_no_suites(self, tmp_path):
        repo = Repo(sut_dir=_make_sut(tmp_path))
        assert "no tests found" in _render(repo.get_test_suites_panel())
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestGetTestSuitesPanel -v`
Expected: FAIL — `get_test_suites_panel()` still requires an `items` argument.

- [ ] **Step 3: Rewrite `get_test_suites_panel`**

In `src/otto/configmodule/repo.py`, replace the body of `get_test_suites_panel` (drop the `items` parameter):

```python
    def get_test_suites_panel(self) -> "Panel":
        """Rich panel listing this repo's runnable suite names.

        Sourced from the suite registry (``registered_suites``) — the exact
        ``otto test <name>`` subcommands — not from a pytest collection.
        """
        from rich.text import Text

        names = self.registered_suites()
        lines = [f"• {n}" for n in names]
        content = Text("\n".join(lines)) if lines else Text("(no tests found)", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)
```

- [ ] **Step 4: Point `list_suites_callback` at the registry path**

In `src/otto/cli/test.py`, `_list_tests_display` currently does `getattr(repo, panel_method)(repo.collect_tests())`. The suites path must not collect. Change `list_suites_callback` to render directly:

```python
def list_suites_callback(value: bool) -> None:
    """Print all available test suites (one panel per repo) and exit when the flag is set."""
    if not value:
        return
    panels = [repo.get_test_suites_panel() for repo in get_repos()]
    _render_panels(panels)
    raise typer.Exit
```

Add a small shared renderer `_render_panels` (extract the table-building tail currently inside `_list_tests_display`) so both suites and tests listing reuse it:

```python
def _render_panels(panels: list["Panel"]) -> None:
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)
```

(After this change `list_suites_callback` no longer calls `_list_tests_display`; that helper becomes unused once Task 4 lands its own inline rendering and is deleted in Task 5. Add `from rich.panel import Panel` under TYPE_CHECKING if needed for the annotation, or quote it.)

- [ ] **Step 5: Replace the mocked `--list-suites` CLI test with a real one**

In `tests/unit/cli/test_listing.py`, replace `TestListCallbacks.test_list_suites_calls_correct_panel` (which mocked `collect_tests`) with a real, non-mocked test:

```python
class TestListCallbacks:
    def test_list_suites_renders_registry_names(self, tmp_path):
        from otto.suite import register as reg

        sut_dir = _make_sut(tmp_path)
        reg._SUITE_REGISTRY.append(("TestRealSuite", __import__("typer").Typer()))
        reg._SUITE_FILES["TestRealSuite"] = str((sut_dir / "tests" / "test_real.py").resolve())
        try:
            with patch("otto.cli.test.get_repos", return_value=[Repo(sut_dir=sut_dir)]):
                result = runner.invoke(suite_app, ["--list-suites"])
        finally:
            reg._SUITE_REGISTRY[:] = [e for e in reg._SUITE_REGISTRY if e[0] != "TestRealSuite"]
            reg._SUITE_FILES.pop("TestRealSuite", None)
        assert result.exit_code == 0
        assert "TestRealSuite" in result.stdout
```

- [ ] **Step 6: Run the listing tests**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py -v`
Expected: PASS (the suites tests no longer call `collect_tests`).

- [ ] **Step 7: Verify the real bug is fixed end-to-end**

Run:
```bash
source ./project_env >/dev/null 2>&1 && .venv/bin/otto test --list-suites
```
Expected: exit 0; repo1 panel lists `TestCoverageProduct`, `TestDevice`, `TestStabilityFixture` (no traceback, not "no tests found").

- [ ] **Step 8: Stage**

```bash
git add src/otto/configmodule/repo.py src/otto/cli/test.py tests/unit/cli/test_listing.py
# fix(test): list --list-suites from the suite registry (kills the collect_tests traceback)
```

---

## Task 3: Harden `collect_tests()` (bugs A/B/C) + selector params

Make the inner collection robust and selector-aware (needed by `--list-tests`).

**Files:**
- Modify: `src/otto/configmodule/repo.py` (`collect_tests` ~L273-342)
- Test: `tests/unit/configmodule/test_repo.py` (new `TestCollectTestsHardening` class)

**Interfaces:**
- Produces: `Repo.collect_tests(markers: str | None = None, suite: str | None = None, tests: str | None = None) -> list[CollectedTest]`. On a failed inner collection it logs a clear per-repo error (does not return `[]` silently). `markers` → `-m`, `tests` → `-k`, `suite` → restrict to that registered suite's file.

- [ ] **Step 1: Write the failing regression test for bug B (StringIO/fileno) + bug A (pytest_asyncio)**

In `tests/unit/configmodule/test_repo.py`, add (the inner pytest auto-loads any parent conftest; a `faulthandler.register` in a fixture conftest reproduces B, and importing `pytest_asyncio` in-process reproduces A):

```python
class TestCollectTestsHardening:
    def _make_repo(self, tmp_path, test_body="def test_ok():\n    assert True\n"):
        from otto.configmodule.repo import Repo

        sut = tmp_path / "sut"
        (sut / ".otto").mkdir(parents=True)
        (sut / ".otto" / "settings.toml").write_text(
            'name = "sut"\nversion = "1.0.0"\ntests = ["${sut_dir}/tests"]\n'
        )
        (sut / "tests").mkdir()
        (sut / "tests" / "test_a.py").write_text(test_body)
        return Repo(sut_dir=sut)

    def test_collects_with_fileno_dependent_conftest_and_pytest_asyncio(self, tmp_path):
        import pytest_asyncio  # noqa: F401 — reproduce the parent-import precondition (bug A)

        repo = self._make_repo(tmp_path)
        # A conftest that needs a real stdout fd (reproduces bug B under StringIO).
        (repo.sut_dir / "tests" / "conftest.py").write_text(
            "import faulthandler, signal, sys\n"
            "def pytest_configure(config):\n"
            "    faulthandler.register(signal.SIGUSR1, file=sys.stderr)\n"
        )
        items = repo.collect_tests()
        assert len(items) == 1
        assert items[0].name == "test_ok"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest "tests/unit/configmodule/test_repo.py::TestCollectTestsHardening::test_collects_with_fileno_dependent_conftest_and_pytest_asyncio" -v`
Expected: FAIL — collects 0 items (the `StringIO` redirect crashes the `faulthandler.register` conftest under `INTERNALERROR`).

- [ ] **Step 3: Implement the hardened collection block**

In `src/otto/configmodule/repo.py`, replace the `try:`/`pytest.main(...)` block (the `with contextlib.redirect_stdout(io.StringIO())…` through the `pytest.main([...], plugins=[collector])` call). Use a real-fd sink, clear `filterwarnings`, capture the return code, and add selector args. The method signature changes to accept selectors:

```python
    def collect_tests(
        self,
        markers: str | None = None,
        suite: str | None = None,
        tests: str | None = None,
    ) -> list[CollectedTest]:
```

Selector args are assembled before the `pytest.main` call:

```python
            selector_args: list[str] = []
            if markers:
                selector_args += ["-m", markers]
            if tests:
                selector_args += ["-k", tests]
            if suite:
                from ..suite.register import _SUITE_FILES

                suite_file = _SUITE_FILES.get(suite)
                if suite_file is not None:
                    paths = [suite_file]
                # -k narrows to the class within that file
                selector_args += ["-k", suite]
```

And the run block becomes (real-fd sink + cleared warnings + rc check):

```python
            import os

            with (
                open(os.devnull, "w") as sink_out,
                open(os.devnull, "w") as sink_err,
                contextlib.redirect_stdout(sink_out),
                contextlib.redirect_stderr(sink_err),
            ):
                rc = pytest.main(
                    [
                        *paths,
                        "--collect-only",
                        "-p",
                        "no:terminal",
                        "-p",
                        "no:cov",
                        "--override-ini",
                        "addopts=",
                        "--override-ini",
                        "filterwarnings=",
                        "-o",
                        "asyncio_default_fixture_loop_scope=function",
                        *selector_args,
                    ],
                    plugins=[collector],
                )
            # Surface a real collection failure instead of returning [] silently.
            if int(rc) not in (0, 5):  # 0 = OK, 5 = no tests collected
                logger.error(
                    "Test collection failed for repo %r (pytest exit %s); "
                    "see above. Listing may be incomplete.",
                    self.name,
                    int(rc),
                )
```

(`logger` is the module logger already imported in `repo.py`. `os` import may be added at module top instead of inline if the module already imports `os` — check and prefer the existing top-level import.)

- [ ] **Step 4: Run the bug A/B test to confirm it passes**

Run: `.venv/bin/python -m pytest "tests/unit/configmodule/test_repo.py::TestCollectTestsHardening::test_collects_with_fileno_dependent_conftest_and_pytest_asyncio" -v`
Expected: PASS (collects 1 item).

- [ ] **Step 5: Write + run the bug-C test (surface failure, not silent `[]`)**

```python
    def test_collection_failure_is_logged_not_silent(self, tmp_path, caplog):
        import logging

        repo = self._make_repo(tmp_path)
        # A conftest that raises at collection time -> pytest INTERNAL/usage error.
        (repo.sut_dir / "tests" / "conftest.py").write_text("raise RuntimeError('boom')\n")
        with caplog.at_level(logging.ERROR):
            repo.collect_tests()
        assert any("collection failed" in r.message.lower() for r in caplog.records)
```

Run: `.venv/bin/python -m pytest "tests/unit/configmodule/test_repo.py::TestCollectTestsHardening::test_collection_failure_is_logged_not_silent" -v`
Expected: PASS.

- [ ] **Step 6: Write + run the selector test**

```python
    def test_markers_and_tests_selectors_narrow_results(self, tmp_path):
        body = (
            "import pytest\n"
            "def test_keep():\n    assert True\n"
            "@pytest.mark.slow\ndef test_slow():\n    assert True\n"
        )
        repo = self._make_repo(tmp_path, test_body=body)
        (repo.sut_dir / "tests" / "conftest.py").write_text(
            "def pytest_configure(config):\n    config.addinivalue_line('markers','slow: x')\n"
        )
        all_names = {t.name for t in repo.collect_tests()}
        slow_names = {t.name for t in repo.collect_tests(markers="slow")}
        kw_names = {t.name for t in repo.collect_tests(tests="test_keep")}
        assert {"test_keep", "test_slow"} <= all_names
        assert slow_names == {"test_slow"}
        assert kw_names == {"test_keep"}
```

Run: `.venv/bin/python -m pytest "tests/unit/configmodule/test_repo.py::TestCollectTestsHardening" -v`
Expected: PASS (all three).

- [ ] **Step 7: Stage**

```bash
git add src/otto/configmodule/repo.py tests/unit/configmodule/test_repo.py
# fix(test): harden collect_tests (filterwarnings, real-fd sink, surface failures) + selectors
```

---

## Task 4: `--list-tests` flag (selector-aware, exits without running)

Add a list-mode flag to the `otto test` callback that lists the resolved selection (suite + markers) and exits.

**Files:**
- Modify: `src/otto/cli/test.py` (`suite_app` decl ~L408; `main` callback decl ~L417-419 and body ~L588-624; `_list_tests_display` ~L387-393)
- Test: `tests/unit/cli/test_listing.py` (new `TestListTests` class)

**Interfaces:**
- Consumes: `Repo.collect_tests(markers=…, suite=…)` (Task 3); `Repo.get_tests_panel(items)` (existing); `_render_panels` (Task 2).
- Produces: CLI flag `--list-tests` on `otto test`. With it set, the callback lists and `raise typer.Exit` before any suite runs.

- [ ] **Step 1: Write the failing CLI tests**

In `tests/unit/cli/test_listing.py`:

```python
class TestListTests:
    def _repo_with_tests(self, tmp_path):
        sut = _make_sut(tmp_path)
        _add_test_file(
            sut,
            "test_device.py",
            "import pytest\n"
            "class TestDevice:\n"
            "    def test_alpha(self):\n        assert True\n"
            "    @pytest.mark.slow\n    def test_beta(self):\n        assert True\n",
        )
        (sut / "tests" / "conftest.py").write_text(
            "def pytest_configure(config):\n    config.addinivalue_line('markers','slow: x')\n"
        )
        return Repo(sut_dir=sut)

    def test_list_tests_lists_all_and_exits(self, tmp_path):
        repo = self._repo_with_tests(tmp_path)
        with patch("otto.cli.test.get_repos", return_value=[repo]):
            result = runner.invoke(suite_app, ["--list-tests"])
        assert result.exit_code == 0
        assert "test_alpha" in result.stdout
        assert "test_beta" in result.stdout

    def test_list_tests_filters_by_marker(self, tmp_path):
        repo = self._repo_with_tests(tmp_path)
        with patch("otto.cli.test.get_repos", return_value=[repo]):
            result = runner.invoke(suite_app, ["--list-tests", "--markers", "slow"])
        assert result.exit_code == 0
        assert "test_beta" in result.stdout
        assert "test_alpha" not in result.stdout
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestListTests -v`
Expected: FAIL — `--list-tests` is not a known option (usage error, exit code 2).

- [ ] **Step 3: Allow the callback to run without a subcommand**

In `src/otto/cli/test.py`, the `suite_app` Typer is created with `no_args_is_help=True`. Add `invoke_without_command=True` so `otto test --list-tests` (no suite) reaches the callback:

```python
suite_app = typer.Typer(
    name="test",
    no_args_is_help=True,
    invoke_without_command=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)
```

- [ ] **Step 4: Add the `--list-tests` option to the `main` callback**

In the `main` callback signature (after the `list_suites` option ~L420-428), add:

```python
    list_tests: Annotated[
        bool,
        typer.Option(
            "--list-tests",
            help="List the selected tests (optionally narrowed by a suite name / --markers) and exit.",
        ),
    ] = False,
```

- [ ] **Step 5: Handle `--list-tests` in the callback body**

In `main`, right after the `if ctx.resilient_parsing: return` guard (so it short-circuits before output-dir/reservation setup), add:

```python
    if list_tests:
        suite = ctx.invoked_subcommand
        panels = [
            repo.get_tests_panel(repo.collect_tests(markers=markers or None, suite=suite))
            for repo in get_repos()
        ]
        _render_panels(panels)
        raise typer.Exit
```

- [ ] **Step 6: Run the CLI tests to confirm they pass**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestListTests -v`
Expected: PASS.

- [ ] **Step 7: Guard the no-subcommand non-list path**

`invoke_without_command=True` means `otto test` with selectors but no subcommand and no `--list-tests` now reaches the body and would fall through (Phase 2 will run the selection here). For Phase 1, keep today's behavior: if there is no subcommand and no list flag, show help. Add at the end of the body (after the existing `ctx.meta[...] = TestRunOptions(...)` and the `if ctx.invoked_subcommand is not None:` block):

```python
    if ctx.invoked_subcommand is None:
        # Phase 1: no run-by-selector yet; mirror the previous no-args behavior.
        rprint(ctx.get_help())
        raise typer.Exit
```

Add a test:

```python
    def test_no_subcommand_no_flags_shows_help(self):
        result = runner.invoke(suite_app, [])
        assert result.exit_code == 0
        assert "Usage" in result.stdout or "Commands" in result.stdout
```

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestListTests -v`
Expected: PASS.

- [ ] **Step 8: Verify end-to-end**

Run:
```bash
source ./project_env >/dev/null 2>&1
.venv/bin/otto test --list-tests | head -20
.venv/bin/otto test --list-tests --markers slow | head -20
```
Expected: lists individual tests; the marker form narrows them; both exit 0 without running a suite.

- [ ] **Step 9: Stage**

```bash
git add src/otto/cli/test.py tests/unit/cli/test_listing.py
# feat(test): add selector-aware `otto test --list-tests` (lists + exits, no run)
```

---

## Task 5: Delete the unused `get_test_files_panel`

**Files:**
- Modify: `src/otto/configmodule/repo.py` (delete `get_test_files_panel` ~L388-407)
- Modify: `tests/unit/cli/test_listing.py` (remove any `get_test_files_panel` references)

**Interfaces:**
- Removes: `Repo.get_test_files_panel`. No `src/` consumer exists (only the deleted listing path).

- [ ] **Step 1: Confirm there are no remaining consumers**

Run: `grep -rn "get_test_files_panel" src/ tests/ docs/`
Expected: only the method definition + any tests asserting it. (If `src/` shows another consumer, stop — the spec assumed none.)

- [ ] **Step 2: Delete the method and its tests**

Remove `get_test_files_panel` from `src/otto/configmodule/repo.py`, and delete any `test_listing.py` test(s) that render it (e.g. a `TestGetTestFilesPanel` class, if present).

- [ ] **Step 3: Run the listing suite**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py -v`
Expected: PASS, no references to the removed method.

- [ ] **Step 4: Stage**

```bash
git add src/otto/configmodule/repo.py tests/unit/cli/test_listing.py
# refactor(test): drop unused get_test_files_panel (files aren't otto-test-addressable)
```

---

## Task 6 (optional): `--list-markers`

A discovery helper listing the markers a user can pass to `--markers`. **Optional** — defer to a follow-up if it balloons. Sourced statically from each repo's pytest config (no inner collection).

**Files:**
- Modify: `src/otto/configmodule/repo.py` (add `configured_markers()`)
- Modify: `src/otto/cli/test.py` (add `--list-markers` eager option + callback)
- Test: `tests/unit/cli/test_listing.py` (`TestListMarkers`)

**Interfaces:**
- Produces: `Repo.configured_markers() -> list[str]` — marker names from `sut_dir/pyproject.toml [tool.pytest.ini_options].markers` (and `pytest.ini`/`setup.cfg` `[pytest] markers` if present), each reduced to the token before `:` or `(`.
- Produces: CLI flag `--list-markers` (eager, exits).

- [ ] **Step 1: Write the failing `configured_markers` test**

```python
class TestConfiguredMarkers:
    def test_reads_pyproject_markers(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\n'
            'markers = ["slow: heavy", "smoke: quick"]\n'
        )
        repo = Repo(sut_dir=sut)
        assert repo.configured_markers() == ["slow", "smoke"]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestConfiguredMarkers -v`
Expected: FAIL — no `configured_markers`.

- [ ] **Step 3: Implement `configured_markers`**

In `src/otto/configmodule/repo.py`:

```python
    def configured_markers(self) -> list[str]:
        """Marker names declared in this repo's pytest config (for --list-markers).

        Reads ``pyproject.toml [tool.pytest.ini_options].markers``. Each entry is
        reduced to the token before ``:`` or ``(``. Static read — no collection.
        """
        import tomllib

        pyproject = self.sut_dir / "pyproject.toml"
        if not pyproject.is_file():
            return []
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return []
        raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
        out: list[str] = []
        for entry in raw:
            token = str(entry).split(":", 1)[0].split("(", 1)[0].strip()
            if token:
                out.append(token)
        return out
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestConfiguredMarkers -v`
Expected: PASS.

- [ ] **Step 5: Wire `--list-markers`**

In `src/otto/cli/test.py`, add a callback + eager option (mirrors `list_suites_callback`):

```python
def list_markers_callback(value: bool) -> None:
    """Print the markers available to --markers (one panel per repo) and exit."""
    if not value:
        return
    panels = []
    for repo in get_repos():
        from rich.text import Text

        markers = repo.configured_markers()
        lines = [f"• {m}" for m in markers]
        content = Text("\n".join(lines)) if lines else Text("(no markers configured)", style="dim")
        panels.append(repo._make_test_panel(f"{repo.name} {repo.version}", content))  # noqa: SLF001
    _render_panels(panels)
    raise typer.Exit
```

Add the eager option to the `main` callback signature (next to `list_suites`):

```python
    list_markers: Annotated[
        bool,
        typer.Option(
            "--list-markers",
            callback=list_markers_callback,
            is_eager=True,
            help="List the markers available to --markers and exit.",
        ),
    ] = False,
```

- [ ] **Step 6: Write + run the CLI test**

```python
class TestListMarkers:
    def test_list_markers_renders_configured(self, tmp_path):
        sut = _make_sut(tmp_path)
        (sut / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\nmarkers = ["smoke: quick"]\n'
        )
        with patch("otto.cli.test.get_repos", return_value=[Repo(sut_dir=sut)]):
            result = runner.invoke(suite_app, ["--list-markers"])
        assert result.exit_code == 0
        assert "smoke" in result.stdout
```

Run: `.venv/bin/python -m pytest tests/unit/cli/test_listing.py::TestListMarkers tests/unit/cli/test_listing.py::TestConfiguredMarkers -v`
Expected: PASS.

- [ ] **Step 7: Stage**

```bash
git add src/otto/configmodule/repo.py src/otto/cli/test.py tests/unit/cli/test_listing.py
# feat(test): add `otto test --list-markers` (config-sourced)
```

---

## Task 7: Update docs + full gate

**Files:**
- Modify: `src/otto/cli/test.py` (module docstring ~L21 lists `--list-suites`; add `--list-tests`/`--list-markers`)
- Check: `docs/` for any `otto test` flag reference / `--list-suites` mention.

- [ ] **Step 1: Update the `otto test` module docstring + help references**

In `src/otto/cli/test.py` docstring (the block listing `--list-suites` ~L21), add lines for `--list-tests` and `--list-markers`. Grep docs for an `otto test` options table and add the new flags if one exists:

Run: `grep -rn "list-suites\|otto test" docs/ | grep -i "list\|options"`

- [ ] **Step 2: Run the full gate**

Run, one at a time (single passes — no looping):
```bash
make coverage
make typecheck
make docs
```
Expected: all green. Investigate any listing-related coverage regressions.

- [ ] **Step 3: Run nox (cross-version)**

Run: `nox`
Expected: green across py3.10–3.14.

- [ ] **Step 4: Stage docs**

```bash
git add src/otto/cli/test.py docs/
# docs(test): document --list-tests / --list-markers on otto test
```

---

## Self-Review notes (author)

- **Spec coverage:** §4.1 → Tasks 1-2; §4.2 → Task 3; §4.3 → Task 4; §4.4 → Task 6; §4.5 → Task 5; §4.6 testing → tests embedded in every task. Phase 2 (run-by-selector) is intentionally **not** in this plan.
- **Open item to resolve during Task 3:** confirm the `suite=` path-restriction interaction with `-k <ClassName>` collects exactly the named suite on the real fixture; adjust `selector_args` if `-k` alone is sufficient (it likely is, given class names are unique).
- **Watch:** Task 4 Step 7's `invoke_without_command=True` changes `otto test` (no args) routing — the help-fallback test guards it. Verify no existing `tests/unit/cli/test_test.py` test asserts the old "Missing command" error; if so, update it in Task 4.
