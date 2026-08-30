# Suites go pytest-native — fixtures replace the xunit hooks

**Date:** 2026-08-30
**Status:** Designed 2026-08-29/30 with Chris, section by section; awaiting
implementation plan
**Depends on:** nothing outstanding. The Getting Started overhaul
(`2026-08-27-getting-started-docs-overhaul-design.md`) has landed on main, so
the scaffold and docs this spec rewrites are the current ones.

## 1. Goal

`OttoSuite` grew up as a `unittest`-shaped class: `setup_method`,
`teardown_method`, `setup_class`, `teardown_class` populate `self.suiteDir`,
`self.testDir`, `self.logger` and `self.expect`, and suite authors reach for
the same hooks for their own setup. Chris's direction: *embrace the pytest
ethos as fully as possible while adding usability*. Concretely:

- Everything a suite gets from otto arrives the way pytest delivers things —
  as fixtures — and nothing otto-specific rides on `self`.
- A suite author writes suite-wide and per-test setup/teardown as fixtures,
  never as hooks, and the docs say plainly what comes free, how to write those
  fixtures, and (lightly) how that maps for readers used to xunit.
- Suite-wide setup talks to hosts **once per suite**: every test in a class,
  every fixture it uses, and the class-scoped fixture that opened the sessions
  share one event loop.
- Declaring a test's required lab state is a marker that applies to a whole
  suite and overrides per test.
- Nothing is required boilerplate: a bare `OttoSuite` subclass with test
  methods is a complete suite.
- Hard cutover. No shims, no deprecation period; every removed spelling fails
  loudly where it is written (§10).

The refactor also closes the held tier-1 defect **#1** — `otto test --monitor`
writes an export with `metrics: 0` — as a direct consequence of the loop model
(§3.4), and folds in a second cleanup Chris asked for: a suite's options are
declared **one** way (§7).

### 1.1 Out of scope — follow-ups (§11)

Location-based logger capture; a suite-wide monitor fixture; the composite
lab's element-key rendering. Each is recorded with its reason in §11; none of
this spec waits on any of them.

## 2. Decisions at a glance

Each of these was settled with Chris during the design and is not re-opened by
the plan. The section named carries the detail.

| decision | section |
| --- | --- |
| One event loop per suite: `otto test` sets both pytest-asyncio loop-scope defaults to `class`; every `loop_scope` pin on otto's class- and function-scoped fixtures is removed | §3 |
| Wider-than-class async fixtures (module/session) must pin their `loop_scope` explicitly | §3.3 |
| `ensure` is a **marker** whose value is a **path** of converge steps; the closest marker replaces the whole path; the public `ensure_*` fixtures are removed | §4 |
| Public fixtures = what a test *requests* (`suite_options`, `ctx`, `suite_dir`, `test_dir`, `expect`); private autouse fixtures = what *acts* on a test; nothing otto-specific on `self` | §5 |
| `suiteDir`/`testDir` become the fixtures `suite_dir` (per suite) and `test_dir` (per test), public, created on request | §5.3 |
| `expect` is a public fixture; its failures fail the **call** phase | §5.4 |
| `self.logger` goes; a suite uses `logging.getLogger(__name__)` at module top; `otto test` captures each collected suite module's logger into otto's sinks | §5.5 |
| Suite-wide fixtures defined on the class are `@classmethod`s (pytest 9 requirement) | §6 |
| `ctx` becomes session-scoped | §5.2 |
| `Options = _Opts` is the only way to declare options; `OttoSuite` is no longer `Generic` | §7 |
| The monitor helpers (`start_monitor` …) stay instance methods, per test | §5.6, §11 |
| The xunit bridge in the docs covers only the hook rows and the assertion line | §8.2 |

## 3. One event loop per suite

### 3.1 Today

`otto test` (`src/otto/suite/run.py`, `base_args`) passes pytest only
`-o asyncio_mode=auto`. pytest-asyncio 1.4.0 then runs each **test** on a
function-scoped loop (its default), while otto's class-scoped fixtures pin
`loop_scope="class"`. A host session opened in a class fixture is bound to the
class loop; every test runs on a different one. That is the source of the
"talk to hosts once per suite" impossibility and of defect #1.

### 3.2 The change

`base_args` gains:

```text
-o asyncio_default_test_loop_scope=class
-o asyncio_default_fixture_loop_scope=class
```

and every explicit `loop_scope=` pin on otto's class- and function-scoped
fixtures is deleted: `ensure_*` (removed outright, §4), `_otto_release_connections`
(`src/otto/suite/suite.py`), `_otto_class_monitor_task`
(`src/otto/suite/plugin.py`). With the defaults in place, a pin is at best
redundant and at worst wrong: a fixture pinned `loop_scope="function"` lands
on a *different* loop from the test (verified, Appendix A.1).

The only fixture that keeps its pin is `_otto_session_monitor`
(`plugin.py`), which is session-scoped and already declares
`loop_scope="session"` — see §3.3 for why that is mandatory.

Verified behaviour under the two defaults (Appendix A.1):

- a class-scoped fixture, the tests of that class, and an unpinned
  function-scoped fixture share one loop; the class fixture's teardown runs
  on that same loop;
- synchronous test methods and fixtures are unaffected;
- a plain (module-level) test function runs on a module loop; an unpinned
  function-scoped fixture it requests shares that loop.

### 3.3 Wider-than-class async fixtures pin their loop scope

An async fixture of **module** or **session** scope that does not declare
`loop_scope` inherits the class default and fails at setup with
`ScopeMismatch: You tried to access the class scoped fixture
_class_scoped_runner with a session scoped request object` (verified,
Appendix A.4). The rule, stated in the docs and modelled by the scaffold:

> A class- or function-scoped async fixture never declares `loop_scope`. A
> module- or session-scoped async fixture always declares `loop_scope` equal
> to its scope.

Pinned module/session fixtures work from class-loop tests (Appendix A.4); the
object they yield lives on the wider loop, which is exactly the cross-loop
situation such fixtures are in today, so no behaviour is lost.

The docs' troubleshooting entry quotes the `_class_scoped_runner` message
verbatim so a search finds the rule.

### 3.4 Escape hatch, and defect #1

A suite that wants a fresh loop per test marks the class
`@pytest.mark.asyncio(loop_scope="function")`. Nothing in otto needs it.

Defect #1: `_otto_class_monitor_task` drives `collector.run()` on the class
loop; because tests ran on function loops, that task never ticked while a
test executed, and the session-end export held no metrics. The regression
test that exists (`tests/unit/suite/test_plugin.py::test_e2e_monitor_collects_metrics_under_class_loop_scope`)
is green today only because its fake suite carries
`@pytest.mark.asyncio(loop_scope="class")` — a real suite under `otto test`
never did. With §3.2 the tests run on the class loop, the task ticks, and the
export fills. §9 reshapes that test so it is red on today's main.

## 4. The `ensure` marker

### 4.1 Shape

```python
@pytest.mark.ensure("clean", "installed")   # converge clean, then installed
class TestWidget(OttoSuite):
    async def test_fresh_install_boots(self) -> None: ...

    @pytest.mark.ensure("installed")           # this test: one status sweep
    async def test_service_answers(self) -> None: ...

    @pytest.mark.ensure("none")                # this test: touch nothing
    async def test_reads_only(self) -> None: ...
```

- The marker's positional arguments are a **path**: converge steps run in the
  order written, before the test body, each through the same
  `otto.project` converge function `otto run <verb> --ensure` calls (today's
  `ensure_installed`/`ensure_uninstalled`/`ensure_clean` bodies in
  `src/otto/suite/pytest_plugin.py`), so a marker and the command never
  diverge.
- **Vocabulary:** `installed`, `uninstalled`, `clean`, `none`. `none` is a
  complete path meaning "converge nothing" and may not be combined with other
  steps. The vocabulary is a single table in the plugin so future converge
  verbs slot in.
- **Closest marker replaces the whole path.** Precedence is test method, then
  class, then module `pytestmark`. Nothing merges: a class path of
  `("clean", "installed")` and a test marked `("installed")` gives that test
  `("installed")` alone. Stacking is something the author writes inside one
  path, never something the framework infers.
- **Unmarked = nothing converged.** The scaffold must pass against a
  placeholder lab, and a suite that never asked for lab state must not touch
  the lab.
- Plain test functions honour the marker exactly as class tests do.

### 4.2 Implementation

- One private, autouse, function-scoped async fixture `_otto_ensure` on
  `OttoOptionsPlugin` (`pytest_plugin.py`) reads the closest `ensure` marker
  via `request.node.get_closest_marker("ensure")` and awaits each step. It has
  no `loop_scope` pin, so it runs on the suite's loop (§3).
- The marker is registered with pytest (`config.addinivalue_line("markers",
  ...)`) so `--strict-markers` runs accept it. `otto test --list-markers`
  reads the *repo's* `pyproject.toml` statically (`Repo.configured_markers`),
  so it cannot see a registration; otto's built-in markers (`ensure`, the
  existing `retry`) live in one table, `otto.suite.markers.OTTO_MARKERS`,
  which both the registration and a separate "otto (built in)" panel in
  `--list-markers` render. (Plan ruling R1.)
- **Validation at collection**, in `pytest_collection_modifyitems`: an unknown
  verb, a `none` combined with other steps, or an empty marker errors the run
  before any test executes, naming the offending node, the verb, and the
  vocabulary.
- Failure semantics are unchanged from today's fixtures: a convergence that
  fails **errors** the test with the failing host named, never a skip
  (`otto.errors.EnsureStateError`); a dry-run refusal propagates.
- The public fixtures `ensure_installed`, `ensure_uninstalled`, `ensure_clean`
  are **deleted**. Requesting one by name fails with pytest's "fixture not
  found" at setup. Chris's reason: two spellings of one action are a footgun,
  and a fixture reads as something a test *has*, not something done *to* it.

## 5. What every suite gets

### 5.1 The table

| | free — autouse, private, undocumented by name | on request — public fixture |
| --- | --- | --- |
| suite-wide | one event loop (§3); host-connection release at class end under `--cov` | `suite_options` (class), `suite_dir` (class) |
| per test | the `ensure` path (§4); the start banner; monitor start/end events; `expect` failures failing the call phase | `test_dir` (function), `expect` (function), `ctx` (session) |
| logging | the suite file's `logging.getLogger(__name__)` reaches otto's console and log files | — |

Naming rule: what **acts** on a test is an underscore-prefixed autouse
fixture and is described by behaviour, never by name; what a test
**requests** is public and documented. The public fixtures live on
`OttoOptionsPlugin` so classes and plain functions get them alike.

### 5.2 `suite_options` and `ctx`

Unchanged in meaning. `ctx` moves from function to **session** scope: it
returns the one active `OttoContext`, and at function scope a class-scoped
fixture could not request it (`ScopeMismatch`).

### 5.3 `suite_dir` and `test_dir`

- `suite_dir` — class-scoped `Path`, `<run output dir>/<ClassName>`. For a
  plain function (no class) it is `<run output dir>/<module stem>`; pytest
  resolves a class-scoped fixture requested outside a class per function, and
  the directory is simply re-asserted (`mkdir(parents=True, exist_ok=True)`).
- `test_dir` — function-scoped `Path`, `suite_dir / <sanitized node name>`
  (today's `_sanitize_node_name`), so parametrized tests keep unique names.
- Both are **created when requested**, like `tmp_path`: a test that never
  names `test_dir` leaves no directory behind.
- The run output dir is `get_context().output_dir` — the per-invocation
  `<xdir>/test/<timestamp>_<suite>` directory `create_output_dir` makes; a
  `None` there raises the same `RuntimeError` the hooks raise today.
- Layout change: today's `<run>/tests/<node>` (shared by every suite in the
  invocation, so two suites with a `test_boot` collide) becomes
  `<run>/<Suite>/<node>`; the `setupClass/` and `teardownClass/` directories
  are gone — a suite-wide fixture writes into `suite_dir`. Nothing in-tree
  reads the old paths.

### 5.4 `expect`

- A function-scoped public fixture returning a callable
  `otto.suite.expect.ExpectCollector` (the class gains `__call__` delegating
  to `expect`, so `expect(cond, "msg")` and `expect.failures` both work). The
  collector keeps today's report shape — source line, message, caller locals —
  and today's immediate `EXPECT FAILED` warning on the console.
- The fixture stores the collector in `request.node.stash` under a
  `StashKey[ExpectCollector]` owned by the plugin.
- A `pytest_pyfunc_call` **wrapper** (`@pytest.hookimpl(wrapper=True)`) on
  `OttoOptionsPlugin` resets the collector, lets the body run, then — if the
  body returned normally and failures were recorded — raises
  `pytest.fail(<summary>, pytrace=False)`. A body that raised keeps its own
  exception; the soft failures were already logged as they happened.
  `pytest_pyfunc_call` rather than `pytest_runtest_call` because
  `@pytest.mark.retry` and `--iterations` re-run the body through
  `item.runtest()`, which re-enters `pytest_pyfunc_call` but not
  `pytest_runtest_call`: each attempt starts from a reset collector and is
  judged alone. (Plan ruling R2; verified, Appendix A.3.)
- Consequence, and the defect this fixes: today `expect` failures are raised
  from a fixture's teardown, so pytest reports them as `1 passed, 1 error`.
  Now they are `1 failed`, in the call phase (verified, Appendix A.3). Plain
  functions may use `expect`.
- `OttoSuite.expect` and `_expect_failures` are removed; the doctest in the
  method's docstring moves to `ExpectCollector`.

### 5.5 Logging

`self.logger` is removed. A suite file writes `logger =
logging.getLogger(__name__)` at module top — the idiom the Getting Started
instruction example already uses.

Why that needs a plugin change: otto's handlers hang on the `otto` logger with
`propagate=False` (`src/otto/logger/management.py`, `init_cli_logging`), and
other loggers are admitted by name prefix — `Repo.product_log_prefixes()`
(`src/otto/config/repo.py`): `init` module roots, `libs` sub-packages,
`[logging] capture`. A suite module under `tests/` is imported by pytest, not
by `init`, so today it is not captured. `OttoOptionsPlugin.pytest_collection_modifyitems`
therefore calls `capture_external_loggers({item.module.__name__.split(".")[0]
for item in items})`. When no `QueueHandler` exists yet (library runs) that
function is a documented no-op. `tach.toml` gains the edge `otto.suite` →
`otto.logger` by hand (a downward edge, like `otto.cli`'s), never via
`tach sync`. (Plan ruling R3.)

Chris reviewed and **deferred** the general fix (a root-logger handler with a
pathname filter) as a follow-up (§11.1); this per-item capture is the agreed
scope.

### 5.6 Private autouse fixtures on the base class

- `_otto_log_test_start` (function) — the start banner, unchanged.
- `_otto_monitor_events` (function, async, no pin) — unchanged.
- `_otto_release_connections` (class, async, autouse, **`@classmethod`**, no
  pin) — closes host connections at class end under `--cov`, unchanged in
  behaviour. It must become a classmethod: pytest 9.1 deprecates class-scoped
  fixtures defined as instance methods (`PytestRemovedIn10Warning`; Appendix
  A.2), and the repo runs `filterwarnings = ["error"]`.
- The monitor slots `_monitor_collector`, `_monitor_server`, `_monitor_task`,
  `_monitor_db` become class-level `None` defaults (assignment creates the
  instance attribute, as today); `start_monitor`/`stop_monitor`/
  `add_monitor_event`/`get_monitor_*` stay instance methods with per-test
  state, exactly as now. Starting a monitor from a suite-wide fixture was not
  possible from `setup_class` either; it is follow-up §11.2.
- `setup_method`, `teardown_method`, `setup_class`, `teardown_class` are
  **deleted** from `OttoSuite`, together with the attributes they set.

`_otto_class_monitor_task` on `OttoPlugin` loses its `loop_scope` pin and is
otherwise unchanged.

## 6. Writing setup and teardown

One shape, pytest's own: code before `yield` is setup, code after is
teardown, `scope` says how often, `autouse` says whether every test gets it.

### 6.1 Suite-wide — the `setup_class` replacement

```python
import logging

import pytest
import pytest_asyncio

from otto.config import get_host
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@pytest.mark.ensure("installed")
class TestRouter(OttoSuite):
    Options = _Options

    @pytest_asyncio.fixture(scope="class", autouse=True)
    @classmethod
    async def dut(cls, suite_options: _Options, suite_dir):
        host = get_host(suite_options.device)          # once per suite
        (suite_dir / "boot.log").write_text((await host.run("dmesg")).only.value)
        yield host                                     # tests take it by name
        await host.close()                             # once, after the last test

    @pytest.fixture(autouse=True)
    async def _reset_counters(self, dut):             # setup_method replacement
        await dut.run("counters clear")
        yield
        await dut.run("counters dump")                # teardown_method replacement

    async def test_uplink(self, dut, expect, test_dir) -> None:
        result = (await dut.run("show uplink")).only
        expect("up" in result.value, "uplink down")
        (test_dir / "uplink.txt").write_text(result.value)

    @pytest.mark.ensure("clean", "installed")
    async def test_first_boot(self, dut) -> None: ...
```

- A class-scoped fixture **defined on the suite class** is a `@classmethod`
  (decorator order: fixture outermost, `classmethod` inside). pytest 9 gives
  the instance form a throwaway `self`, and attributes set there never reach
  the tests (Appendix A.2). A conftest fixture is a plain function — no
  `classmethod`.
- Async fixtures run on the suite's loop (§3): a session opened in `dut` is
  live in every test.

### 6.2 Conventions the docs state

- **`autouse` vs named.** `autouse=True` = "runs for every test whether or
  not it mentions it" — `setup_class`/`setup_method`. Leave it off for setup
  only some tests need; they request it by name. A fixture may be both.
- **Values travel as return values.** `cls.x = …` in a class fixture does
  reach the tests, but it is shared mutable state; `yield host` and
  `def test(self, dut)` is the idiom. Function-scoped fixtures may set
  `self.x` (pytest binds them to the test's instance); same advice.
- **Depending on otto:** any fixture may request `suite_options`,
  `suite_dir`, `test_dir`, `expect`, `ctx`; pytest orders by dependency.
- **Ordering promise:** class scope before function scope; within a scope,
  autouse before requested; otto's own autouse fixtures (registered at plugin
  level) before the author's. Nothing finer — a fixture that needs another
  requests it.
- **Where fixtures live:** suite-local ones as methods on the class; shared
  ones in `conftest.py` or on a non-`Test`-prefixed base class
  (`BaseRouter(OttoSuite)` — not registered, fixtures inherited).
- **Overriding:** a subclass redefines a fixture by name; a single test opts
  in with `@pytest.mark.usefixtures("name")`; `ensure` overrides at the
  closest marker (§4).
- **Failure phases:** a fixture raising before `yield` → `ERROR` at setup,
  body skipped, that fixture's teardown not run (guard partial setup with
  `try`/`finally` or a context manager, as in plain pytest); after `yield` →
  `ERROR` at teardown alongside the body's own verdict; `expect` failures →
  `FAILED` in the call phase.
- **Never pin `loop_scope` on a class- or function-scoped fixture** inside a
  suite (§3.2); module/session async fixtures always pin it (§3.3).

### 6.3 An author's own xunit hooks

pytest honours `setup_method`/`setup_class` on any class independently of
otto, so an author's own hooks keep running. They are not banned — that would
cut against the ethos — but the docs say they are synchronous, cannot request
fixtures, and are the second choice. The moment one touches otto state
(`cls.testDir`) it fails loudly.

## 7. One way to declare Options

Today the scaffold, the docs and the base-class docstring write both
`class TestExample(OttoSuite[_Options])` and `Options = _Options`. Otto reads
only `cls.Options` (`src/otto/suite/register.py`, `register_suite_class`;
`pytest_plugin.py`, `suite_options`). The generic parameter `TOptions` is
referenced nowhere else — present since the initial commit, never given a
consumer.

- `OttoSuite` stops subclassing `Generic[TOptions]`; `TOptions` is deleted.
- The base declares `Options: ClassVar[type[Any] | None] = None`, so the
  attribute is typed once, "no options" stays legal, and both readers use
  `cls.Options` instead of `getattr(cls, "Options", None)`.
- `register_suite_class(suite_class: type)` becomes
  `register_suite_class(suite_class: "type[OttoSuite]")` (import under
  `TYPE_CHECKING`); the `getattr` had hidden the loose parameter.
- `OttoSuite[_Options]` then raises `TypeError: 'type' object is not
  subscriptable` at class definition — the loud failure the cutover wants.
- Type-checking verified in a throwaway worktree (Appendix A.5): ty 0.0.73
  with `all = "error"` clean; pyright 1.1.413 strict on the touched files
  drops from 9 errors to 8 — the two `getattr`-induced
  `DataclassInstance | type[DataclassInstance]` errors disappear, the rest are
  pre-existing and unrelated — and a bare subclass with `Options = _Opts` and a
  test annotated `suite_options: _Opts` yields zero diagnostics under strict.

Occurrences to rewrite: 20 `OttoSuite[` sites across 18 files — the base-class
docstring, `src/otto/cli/init_templates.py`, `README.md`, seven docs pages
(`overview`, `getting-started/index`, `library/writing-suites`,
`library/suite-recipes`, `library/options-classes`,
`library/writing-instructions`, `library/custom-parsers`), the sample repos
(`tests/repo1/tests/test_device.py`, `test_stability_fixture.py`,
`test_coverage_product.py`; `tests/repo3/tests/test_embedded_coverage.py`;
`tests/repo_e2e/tests/test_minimal.py`), and
`tests/unit/suite/test_auto_registration.py`,
`tests/unit/suite/test_options_plugin.py`,
`tests/unit/config/test_completion_cache_unit.py`. The plan re-runs the grep
rather than trusting this list.

## 8. Scaffold and documentation

### 8.1 `otto init --tests`

The scaffold is the first suite a new user reads; it demonstrates each idea
once and stays hostless so it passes out of the box.

- `TEST_EXAMPLE_TEMPLATE` (`src/otto/cli/init_templates.py`): module-level
  `logger = logging.getLogger(__name__)`; `class TestExample(OttoSuite)` with
  `Options = _Options`; one class-scoped autouse `@classmethod` fixture
  (synchronous — nothing to await hostless) that logs once per suite and
  yields a value `test_logs_message` requests alongside `suite_options` and
  `repo_marker`; a second test that uses `expect` and writes to `test_dir`;
  the plain `test_example_function` stays. No `ensure` marker.
- `CONFTEST_TEMPLATE`: the commented `primary_host` block becomes the shape
  people copy — `@pytest_asyncio.fixture(scope="class")`, `yield host`,
  `await host.close()` — with the comment noting it is shared by every test in
  a suite, that a conftest fixture needs no `classmethod`, and that a
  session-scoped variant must add `loop_scope="session"`.

### 8.2 `docs/library/writing-suites.md` — the one home

"Defining a test suite", "Suite registration" and "Options classes" stay
(rewritten to the bare-subclass form). "Fixtures otto provides" and "Suite
features" are replaced by three headings:

1. **What every suite gets** — the §5.1 table; one paragraph on one loop per
   suite with its escape hatch and the §3.3 pin rule; the `ensure` marker
   (§4.1: path, closest-replaces, `none`, unmarked). What each verb converges
   stays in `guide/cli/run/defaults.md` and is linked, not restated.
2. **Setup and teardown as fixtures** — §6.1's example, §6.2's conventions,
   the failure-phase list, §6.3.
3. **Coming from unittest** — two sentences of framing and this table only:

   | you wrote | write instead |
   | --- | --- |
   | `setup_class(cls)` / `teardown_class(cls)` | class-scoped, autouse, `@classmethod` yield fixture — before / after `yield` |
   | `setup_method(self)` / `teardown_method(self)` | function-scoped autouse yield fixture (`self` is the test's instance) |
   | `self.assertEqual(a, b)` | `assert a == b` |

   The renames (`testDir` → `test_dir`, `self.logger`, `self.expect`) are
   cutover facts (§10), not xunit facts, and stay out of this table.

`docs/library/suite-recipes.md` keeps the worked examples (expect, artifact
directories, monitor) rewritten to the fixture spellings; writing-suites names
each in its table and links to the recipe. The `ExpectCollector` doctest stays
in the recipes as the library-use example. The recipe's artifact path becomes
`<run dir>/TestDevice/test_capture_logs/`.

A troubleshooting entry quotes the `ScopeMismatch … _class_scoped_runner`
message and states the §3.3 rule.

### 8.3 The sweep

The old spellings appear on roughly ten further pages — `getting-started/index.md`
(`self.logger` in the first-suite example), `README.md`, `overview.md`,
`library/writing-instructions.md`, `library/options-classes.md`,
`library/custom-parsers.md`, `architecture/subsystems/execution.md`,
`guide/cli/run/defaults.md` and `guide/cli/dry-run.md` (which cite the
`ensure_*` fixtures by name) — and in the `OttoSuite` class docstring, which
the API pages render. The exit criterion is a grep, not a page list (§9.5).
Any capture whose command runs a suite is retaken by
`scripts/refresh_docs_captures.py`, never hand-edited.

## 9. Testing

Hostless, unit lane. Where the guarantee is a property of a pytest *session*,
the test drives a `pytester` run or the run API with `otto test`'s real
`base_args`. Every test names the mutation it must go red under; a test that
cannot fail is a plan defect.

### 9.1 Loop model

1. `base_args` carries both loop-scope defaults; a bare `OttoSuite` with a
   class fixture, two tests and an unpinned function fixture records one loop
   id. Red if either `-o` is dropped.
2. **Defect #1, honestly shaped.** `test_e2e_monitor_collects_metrics_under_class_loop_scope`
   loses its `@pytest.mark.asyncio(loop_scope="class")` and runs through the
   real `base_args`, asserting metrics in the export. Red on today's main,
   green after §3.2.
3. `@pytest.mark.asyncio(loop_scope="function")` on a class → distinct loop
   ids per test.
4. A session-scoped async fixture pinned `loop_scope="session"` is usable from
   a class test; an unpinned one errors with `ScopeMismatch` (documents the
   §3.3 rule rather than hiding it).

### 9.2 `ensure` marker

The five `test_ensure_fixture_*` cases in
`tests/unit/suite/test_options_plugin.py` are ported to marker form
(converge spy, error-not-skip, skipped no-op, dry-run refusal, availability
to a suite), then:

5. Path order: `ensure("clean", "installed")` → spy sees `[clean, installed]`.
6. Closest wins: module `pytestmark = ensure("installed")`, class
   `ensure("clean")`, one test `ensure("none")` → `[clean]`, `[clean]`, `[]`.
   Red if the plugin merges.
7. Unknown verb / `none` with other steps / empty marker → collection error
   naming node, verb and vocabulary; red if validation is skipped.
8. Plain functions honour the marker; unmarked tests converge nothing.
9. Requesting `ensure_installed` by name → "fixture not found".
10. `otto test --list-markers` lists `ensure`.

### 9.3 Base class

11. `suite_dir`/`test_dir`: `TestOttoTestDir` in
    `tests/unit/suite/test_otto_suite.py` re-targeted to
    `<run>/<Suite>/<node>`, sanitized parametrize names, plain function →
    `<run>/<module stem>/<name>`, created on request only (a test that never
    names `test_dir` leaves no directory — red if created eagerly).
12. `expect`: the eight `TestExpect` cases re-targeted to the fixture, plus:
    outcome `failed` with `report.when == "call"` (a `pytest_runtest_logreport`
    recorder) — red if raised from teardown (`passed=1, errors=1`); a hard
    `assert` in the body wins and the report shows the `AssertionError`;
    `expect.failures` is inspectable; a plain function may use it.
13. Nothing otto-specific on the instance: `testDir`, `suiteDir`, `logger`,
    `expect` absent from a live instance; `setup_method`/`setup_class` absent
    from `OttoSuite.__dict__`; `OttoSuite[int]` raises `TypeError`. Replaces
    `test_teardown_method_called`, which tested the removed hook.
14. `_otto_release_connections` as a classmethod: the three cases in
    `tests/unit/suite/test_cov_connection_release.py` adapt, plus a `pytester`
    run under `-W error::pytest.PytestRemovedIn10Warning` — red on the
    instance-method form.
15. Monitor slots: `OttoSuite._monitor_collector is None` at class level;
    `_active_monitor_collector()` on a fresh instance without any hook (the
    three `TestActiveMonitorCollector` cases).
16. `ctx` at session scope: a class-scoped fixture requesting it works — red
    at function scope.
17. Logger capture: after collection every item's top-level module name
    reached `capture_external_loggers` (spy); red if the hook is dropped.

### 9.4 Options one-way

18. `OttoSuite[_Opts]` → `TypeError` at class definition; `Options = _Opts`
    registers; the samples in `test_auto_registration`, `test_options_plugin`,
    `test_completion_cache_unit` rewritten; `register_suite_class` typed
    `type[OttoSuite]`; `ty check` clean (the gate).

### 9.5 Scaffold, docs, exit criterion

19. The `otto init --tests` output runs green hostless via
    `otto test TestExample` and `otto test --tests test_example_function`
    (the existing init test on the new shape); `make docs` with `-W` green;
    suite-running captures retaken by the runner; the `expect` doctest moves
    with the code.
20. Plan step, not a test: outside `docs/superpowers/` and `docs/_build/`,

    ```text
    grep -rn 'self\.logger\|self\.expect\|testDir\|suiteDir\|ensure_installed\|ensure_clean\|ensure_uninstalled\|setup_class\|setup_method\|OttoSuite\[' src docs tests README.md
    ```

    returns nothing, with exactly two permitted classes of hit, both in
    `docs/library/writing-suites.md`: the `setup_class`/`setup_method` rows of
    the §8.2 bridge table and the §6.3 paragraph on an author's own hooks.
    The plan names those lines; anything else is a miss.

### 9.6 Bed-only surfaces

The sample repos `tests/repo1`, `tests/repo3`, `tests/repo_e2e` are rewritten;
the unit lanes that import them exercise the syntax, `make release`/nightly
their behaviour. The e2e modules that use `loop_scope`
(`tests/e2e/test_link_impair_e2e.py`, `tests/e2e/test_tunnel_e2e.py`) are
plain pytest under otto's own config, untouched by `otto test`'s defaults.
`tests/unit/suite/test_run_api.py` generates a suite that uses `self.testDir`
and passes `-o asyncio_default_fixture_loop_scope=function`; both move to the
new shape.

## 10. Cutover and landing order

Hard cutover. The rule: every removed spelling fails loudly where it is
written, or it is not removed.

| old | new | if left unmigrated |
| --- | --- | --- |
| `OttoSuite[_Opts]` | `Options = _Opts` on a bare subclass | `TypeError` at import |
| `self.testDir` / `self.suiteDir` | `test_dir` / `suite_dir` fixtures | `AttributeError` in the test |
| `self.logger` | module-level `logging.getLogger(__name__)` | `AttributeError` |
| `self.expect(…)` | the `expect` fixture | `AttributeError` |
| `ensure_installed` etc. as fixture arguments | `@pytest.mark.ensure("installed")` | "fixture not found" at setup |
| `super().setup_method()` in an override | fixtures | `AttributeError` |
| `<run>/tests/<node>`, `setupClass/`, `teardownClass/` | `<run>/<Suite>/<node>` | nothing in-tree reads the old paths |

Two things that stay quiet and are handled in the docs, not by a guard: an
author's own xunit hooks keep running (§6.3); a fixture pinned
`loop_scope="function"` lands on another loop (§3.2).

**Landing order** — five commits, each green on the full unit gate (a per-task
gate is the whole suite, never a selection), with `nox -s tests_hostless-3.14`
before the squash:

1. **Options one-way** (§7) — independent; its own sweep.
2. **Loop model** (§3) — `base_args`, every pin stripped, defect #1's
   regression test. Fixes #1 by itself.
3. **`ensure` marker** (§4) — public fixtures removed.
4. **Base class** (§5, §6) — `expect` fixture and call-phase wrapper,
   `suite_dir`/`test_dir`, hooks and attributes gone, classmethod release
   fixture, `ctx` session scope, logger capture.
5. **Scaffold, docs, captures, grep exit criterion** (§8).

Product commits and this spec commit separately.

## 11. Follow-ups (out of scope)

### 11.1 Location-based logger capture

The general fix for §5.5: a root-logger `QueueHandler` with a filter admitting
records whose `pathname` is under a configured repo root, or whose logger
matches `[logging] capture`, or whose level is `WARNING`+ (so third-party
warnings that reach stderr via `logging.lastResort` today do not go dark).
Chris's assessment (2026-08-29): complex, and it raises the root level so every
third-party logger constructs records down to the floor before the filter
drops them — a per-statement cost. Deferred; the per-item capture in §5.5 is
the agreed scope.

### 11.2 Suite-wide monitor fixture

`start_monitor` and friends are instance methods holding per-test state; a
class-scoped fixture cannot call them (no instance). A public class-scoped
`monitor` fixture, or a module-level API the helpers wrap, would let a suite
monitor across all its tests. Not a regression — `setup_class` could not do it
either.

### 11.3 Composite lab element-key rendering

`otto.labs.composite` keeps its own tuple element key and still prints
`('alt1', None)` in warnings after `c7169aa1` gave `ElementKey.__str__` the
bare-name form. It buckets element-less hosts under `("", None)`, so sharing
the rendering needs an empty-name decision first.

## Appendix A — verified facts

All spikes ran in a scratch scaffold against the repo's pinned toolchain:
pytest 9.1.1, pytest-asyncio 1.4.0, Python 3.10.

**A.1 Loop scopes.** `pytest.ini` with both `asyncio_default_*_loop_scope =
class`: class fixture, both tests and an unpinned function fixture reported one
loop id; the class fixture's teardown ran on it; a sync method ran; a plain
function got a module loop, and its unpinned function fixture shared it; a
fixture pinned `loop_scope="function"` reported a different loop from its test.

**A.2 Class-scoped fixture as instance method.** pytest 9.1.1 emits
`PytestRemovedIn10Warning: Class-scoped fixture defined as instance method is
deprecated. Instance attributes set in this fixture will NOT be visible to
test methods … Use @classmethod decorator and set attributes on cls instead.`
`id(self)` differed from the test's; `self.attr` set there was absent in the
test; `request.cls.attr` was present. The `@classmethod` form under
`@pytest_asyncio.fixture(scope="class", autouse=True)` ran with `-W error`
clean: `cls.shared` and the yielded value both reached the tests, on the same
loop as the fixture.

**A.3 Failure phase.** `pytest.fail` after a fixture's `yield` → `2 passed,
1 error` ("ERROR at teardown"). A wrapper raising after the body returned →
`FAILED` in the call phase for an async method, a sync method and a plain
function; a body raising `AssertionError` kept that error. Verified for both
`pytest_runtest_call` and `pytest_pyfunc_call` wrappers (the latter with a
per-attempt reset before the `yield`); the design uses `pytest_pyfunc_call`
(§5.4).

**A.4 Wider-scoped async fixtures.** Under the class fixture default, an
unpinned session-scoped async fixture failed at setup with `ScopeMismatch:
You tried to access the class scoped fixture _class_scoped_runner with a
session scoped request object`, naming the fixture and file. Pinned
`loop_scope="session"` and `loop_scope="module"` fixtures worked from class
tests and from a plain function (on their own loops).

**A.5 Type checking without the generic.** Throwaway worktree with §7
applied. `ty check` (0.0.73, `all = "error"`): clean. `npx -y pyright@1.1.413
--pythonpath .venv/bin/python` on `suite.py`, `register.py`,
`pytest_plugin.py`: 9 errors before, 8 after (the two at
`register.py:135/144` gone; the rest pre-existing `request.node` unknowns and
`__signature__` assignments). A probe module with a bare subclass,
`Options = _Opts`, `suite_options: _Opts`, and an options-less suite: zero
diagnostics.
