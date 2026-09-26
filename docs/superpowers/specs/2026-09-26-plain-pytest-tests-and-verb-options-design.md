# Tests become plain pytest; options register per verb — design

**Status:** approved in conversation 2026-09-25/26, one section at a time: the command line and options (§3, §4), removing `OttoSuite` (§5), and closing connections per event loop (§6). The migration and testing sections (§8, §9) are first presented here.
**Issue:** #457. Its two comments record the dispatch-startup interaction and the scoping probe that §6 relies on.
**Builds on:** `2026-09-25-dispatch-startup-cost-design.md`, landed on local main as `4ae54ca7`. Its lazy suite loading is deleted here along with the registry it loads (§5.6).
**Amends:** `2026-07-02-pytest-native-flexibility-design.md` §2.4, where selection runs default-construct suite options, and `2026-08-30-suite-pytest-native-design.md` §5 and §7, the fixtures `OttoSuite` delivers and `Options =` as the only way to declare options.
**Split out:** #469, respecting the repo's own pytest configuration and passing pytest flags through. Not in scope here.
**Order:** starts after #455 (`2026-09-26-per-verb-import-cost-design.md`) lands, decided 2026-09-26 and recorded in that spec's §9. This work reuses #455's `Ref` type for lazy `"module:attr"` registration and is measured against #455's file-operation ceilings (§8).

## 1. Problem

Otto's test suites started as `unittest`-style classes and have moved toward plain pytest. What still ties them to a base class is options:

- **Options are declared per suite.** A suite subclasses `OttoSuite` and sets `Options = _Opts`. `register_suite_class` turns that class into an `otto test <Suite>` subcommand with its own flags, and tests receive the instance through the `suite_options` fixture.
- **Repo-wide options travel by inheritance.** Every suite's `Options` subclasses the repo's shared base, which is also how `otto run` instructions get the same flags.
- **Selections can't carry options.** `otto test -m ...` and `otto test --tests ...` span suites, so they accept no suite flags. Each suite's `Options` is built from defaults, and a required field fails that suite's tests.

Pytest itself can't scope a flag to a suite. Its options are one flat argparse namespace: two conftests that add the same flag crash collection, and a nested conftest's flag parses only when its directory is named on the command line (verified on pytest 9.1.1).

Per-suite options aren't worth their cost. An option that only one suite reads is rare, and a flag that some suites ignore is harmless. What is worth keeping is otto's typer command line, with help and completion, and pydantic validation of options classes.

## 2. Decisions

| Decision | Section |
|---|---|
| `otto test` takes test names as positional arguments. Suite subcommands, `--tests` and `--list-suites` are removed. | §3 |
| Options classes register from init modules, naming the verbs they apply to: `register_options(Cls, verbs=["run", "test"])`, or `@options(verbs=[...])` at the definition | §4.1 |
| Each verb's flags are the union of its registered classes, under the project-instruction collision rule | §4.2 |
| Code gets options by class: `ctx.options(Cls)` in tests, an injected parameter in instructions. There is no `repo_options` fixture. | §4.4 |
| `OttoSuite`, the suite registry and automatic suite registration are deleted | §5 |
| `suite_dir` becomes a module-scoped `module_dir`, and `test_dir` mirrors the pytest test ID | §5.3 |
| A `monitor` fixture replaces the monitor helpers on `self` | §5.2 |
| Every pytest event loop closes the host connections it owns when it ends, whether or not `--cov` is set | §6 |
| Otto's own async fixtures run on the test's loop, and hosts fail fast when used from the wrong loop | §6.4, §6.5 |

## 3. The command line

```
otto test [NAMES...] [-m EXPR] [run flags] [verb-wide options]
```

`otto test` becomes a single command, not a group. Its parameters are otto's run flags, the verb-wide options registered for `test` (§4), and one variadic positional argument, `NAMES`.

**Name forms.** Each name is resolved against every configured repo's pytest collection:

| Form | Selects |
|---|---|
| `test_reboot` | every collected test whose base name is `test_reboot`, in any class, module or repo |
| `TestDevice` | every test in every collected class named `TestDevice` |
| `TestDevice::test_reboot` | that test in every class named `TestDevice` |

Parametrized variants collapse to their base name, as `--tests` does today. Matching a whole class by bare name is new: `resolve_selection` matches the class name, and the static parse (`scan_test_corpus`) emits class names alongside test names. An unknown name is a did-you-mean error, as today.

**When names are required.** A run needs at least one name or `-m`. With neither, otto exits 2 with a message saying so. `--help` and the `--list-*` flags are eager and exit before that check. `-m` with names narrows the named tests.

**Flag placement.** A single command lets options and positionals mix freely, so `otto test TestDevice --lab-env prod` and `otto test --lab-env prod TestDevice` both work. Today run flags must come before the suite name.

**Order.** With `--no-random`, tests run in pytest's collection order: file, then source order within the file. A naive-reader review saw a `--tests` selection under `--no-random` run in neither source nor shuffled order on `4ae54ca7`; the cause is not yet known. The plan reproduces it first, then pins collection order with a test.

**Removed:** the per-suite subcommands, `--tests`, and `--list-suites`. `--list-tests [NAMES...]` lists what the names and `-m` select, grouped by repo, module and class.

**Not included:** file paths or full pytest test IDs as names. They can be added later without breaking any form above.

## 4. Options

### 4.1 Registration

```python
from otto import options, register_options


@options
class RepoOptions:
    lab_env: Annotated[str, typer.Option(help="Lab environment to target.")] = "staging"


@options
class TestOptions:
    firmware: Annotated[str, typer.Option(help="Firmware version to validate.")] = "latest"


register_options(RepoOptions, verbs=["run", "test"])
register_options(TestOptions, verbs=["test"])
```

- **Signature:** `register_options(cls_or_path, *, verbs: list[str]) -> None`. The first argument is an options class or a lazy `"package.module:Attr"` string, resolved when the verb is dispatched. The string is held as #455's `Ref` value type, not a second lazy mechanism. The registry entry keeps the `Ref` together with its verbs, so the verbs are known without importing the class.
- **Verbs** must come from the set of verbs that accept registered options, `{"run", "test"}` today. An unknown verb, an empty list, or a repeated verb raises at registration. Another verb can opt in later without changing the call.
- **One registration per class.** Registering the same class twice raises. A class used by two verbs names both in one call.
- **Init modules only.** The registry records the registering module as its origin, like every otto registry. A registration made while test files load is refused (`RegistrationRefused`, §5.6).
- **Home:** `otto.params`, next to `options_params`, `build_options` and `OptionsSource`. It is re-exported lazily as `otto.register_options`, alongside `otto.options`.

- **Sugar at the definition site.** `@options(verbs=[...])` registers the class as it is defined, exactly as if `register_options(Cls, verbs=[...])` followed the class. The same verb checks and one-registration rule apply. Plain `@options` registers nothing and stays the way to declare an instruction's own options class or a shared base. There is no default verb list, because most options classes belong to one command. `otto.options` becomes a thin wrapper around pydantic's `dataclass` that passes pydantic's own arguments through. Registering at definition imports the class's module at startup, so the string form of `register_options` remains the lazy choice. A class decorated with `verbs=` in a test file or conftest is refused like any other registration made while test files load.

```python
@options(verbs=["test"])
class DeviceOptions:
    firmware: str = "latest"
```

`@instruction(options=...)` keeps its per-command flags unchanged. An instruction is its own command, unlike a suite. Verb-wide registrations add to it.

### 4.2 Merging

A verb's flag set is built after every repo's init modules have run, the same point where project instructions are published (`publish_project_instructions`):

- **The union of every class registered for the verb,** in registration order: otto first, then repos in dependency order.
- **The collision rule is `merged_option_params`':** a field that two classes inherit from one shared base is one flag. The same name introduced by two unrelated classes raises `OptionsCollisionError` at startup, naming both classes and their repos.
- **Otto's own flags take part.** For `test` the union includes the run flags (`--iterations`, `--cov`, `-m`, …). For `run` it includes each instruction's own `options=` class. Any clash is the same startup error.

`merged_option_params` is generalized to take a list of options classes with their origins, rather than a `ProjectInstruction`. The project-instruction path and both verbs call the same function.

### 4.3 Building and validation

At dispatch, otto builds every class registered for the dispatched verb from the parsed flags, through `OptionsSource.from_kwargs(...).build(cls)`. That is the path project instructions already use, so pydantic validation fails as a clean exit-2 command-line error before any test or instruction body runs. Classes registered only for other verbs are not built.

The built instances are stored on the invocation's `OttoContext`, keyed by class.

### 4.4 Delivery

- **Tests** call `ctx.options(Cls)` through the existing session-scoped `ctx` fixture and get back the typed instance. A repo that wants a short name writes a one-line conftest fixture:

  ```python
  @pytest.fixture(scope="session")
  def opts(ctx) -> TestOptions:
      return ctx.options(TestOptions)
  ```

- **Instructions** declare a parameter annotated with a registered class, and otto injects the instance. That is how `OttoContext` injection already works for instruction functions.
- **Errors.** `ctx.options(Cls)` for a class that isn't registered raises, naming the class. For a class registered only for other verbs, it raises naming the verbs the class is registered for, e.g. "RunOnly is registered for run, not test".
- **Multiple repos.** Each class belongs to one repo, so lookup by class needs no per-repo plumbing. Every repo's pytest session sees the same context.

`suite_options`, the `Options` class attribute and `OttoOptionsPlugin.suite_options` are removed.

### 4.5 The `ensure` marker

The converge behind `@pytest.mark.ensure(...)` calls the same `otto.project` functions as `otto run <verb> --ensure`. Today it builds each project instruction body's options from the suite's instance (`OptionsSource.from_instance`). After this change it builds them from the `test` verb's parsed flags (`OptionsSource.from_kwargs`).

The consequence is explicit, not silent: an install body can only see flags registered for `test`. A flag that an install instruction reads must be registered for both verbs if people need to set it from `otto test`. Otherwise it takes its default. The docs for `ensure` say so.

### 4.6 Completion and the cache

- **Flags.** The merged flag list for each verb is plain data in the completion cache's `names` section. Its key set already covers init modules, so editing a test file never changes any cached flag. The shim and flag completion never import an options module.
- **Names.** The `NAMES` positional completes from the `tests` section, the static parse, plus the pytest-collected set, as `--tests` does today. It is a variadic positional with no separator, so each word completes one name. The shim already models variadic positionals (`_consume_positional`), and its `tests` source learns to run without a separator.
- **Tree.** `otto test` has no subcommands, so a cache rebuild never reads the suite registry and never imports test files or pytest.
- **Schema.** The cache schema version is bumped. The change is documented on the completion-cache architecture page (`docs/architecture/subsystems/completion-cache.md`).
- **Import cost.** `otto test --help` and a cache rebuild stop importing test files and pytest, which should lower the `help_repo` surfaces. Name resolution for a run now runs a pytest collection, which may raise the `test` surface. §8 says how either is reported.

## 5. Removing `OttoSuite` and the suite registry

### 5.1 What is deleted

- `OttoSuite` and its `__init_subclass__` registration.
- The suite registry, `otto.suite.register` in full: `SUITES`, `SuiteEntry`, `register_suite_class` and `bound_test_names`.
- `find_suite`, `run_suite`, `run_selection` and `RunOptions.tests`.
- The per-suite Typer sub-apps, and the `make_registry_group(SUITES)` group class behind `otto test`.

A suite becomes a plain `class TestDevice:` or a set of plain test functions, collected by pytest's own rules.

### 5.2 What moves into the plugin

Each of these applies to every test, class or plain function, not only `OttoSuite` subclasses as today:

- **The start-of-test banner** in the log.
- **Monitor start and end events per test.** The session's monitor collector lives on `OttoPlugin`, not on the `OttoSuite` class attribute `_session_monitor_collector`. Plain test functions appear on the monitor timeline for the first time.
- **Closing host connections at loop end** (§6).

**The `monitor` fixture** replaces the per-test helpers on `self`. It is function-scoped:

| Today | After |
|---|---|
| `await self.start_monitor(hosts=..., interval=...)` | `await monitor.start(hosts=..., interval=...)`, same parameters, returns the dashboard URL |
| `await self.stop_monitor()` | `await monitor.stop()`, and the fixture's teardown calls it automatically |
| `self.add_monitor_event(label, ...)` | `monitor.event(label, ...)` |
| `self.get_monitor_results()` | `monitor.results()` |
| `self.get_monitor_events()` | `monitor.events()` |

The automatic stop fixes a trap: today a test that forgets `stop_monitor()` leaks the dashboard server and its task, and leaves the database's end time unwritten, so the archived run looks crashed. Suite-wide monitoring across a class stays out of scope, as in the 2026-08-30 spec §11.2.

**`OttoOptionsPlugin` becomes `OttoFixturesPlugin`**, since it no longer provides options. It provides `ctx`, `module_dir`, `test_dir`, `expect` and `monitor`, plus the `ensure` and `expect` hooks.

### 5.3 Artifact directories

The layout mirrors the pytest test ID:

```
<run output dir>/<module stem>/                            module_dir
<run output dir>/<module stem>/<test name>/                test_dir, plain function
<run output dir>/<module stem>/<ClassName>/<test name>/    test_dir, class test
```

- **`module_dir`** replaces `suite_dir` and is module-scoped. It is the module's directory for every test in it. A class that wants its own shared space makes a subdirectory.
- **`test_dir`** keeps its sanitized, parametrized names and its `iteration_N` level in stability runs.
- **Both are created when requested,** like pytest's `tmp_path`, never eagerly.
- **Multiple repos.** When more than one repo takes part in a run, a repo-name layer goes on top: `<run output dir>/<repo>/<module stem>/...`. That matches how JUnit files are already named per repo. A single-repo run keeps the shorter layout.

Within one repo the layout can't collide: pytest's default import mode refuses two test modules with the same basename.

### 5.4 Listing, dry run and the library API

- **`--list-tests`** groups by repo, module and class, since it is now the only listing.
- **The dry run** (`otto test -n NAMES`) lists what the names match from the static parse, without importing anything. As today, parametrizations are not expanded. A marker expression is shown but not evaluated, because evaluating it needs collection.
- **The library API** is one function in `otto.suite`:

  ```python
  def run_tests(
      names: list[str] | None = None,
      *,
      run_options: RunOptions = RunOptions(),
      options: list[object] | None = None,
      output_dir: Path | None = None,
  ) -> SuiteRunResult: ...
  ```

  `options` takes instances of registered classes. An instance of an unregistered class raises. A registered class that isn't passed is built from its defaults, as the command line would, and a required field raises. `run_tests` with no names and no `run_options.markers` raises `ValueError`, like `run_selection` today.

`otto.suite.__all__` becomes: `NoTestsMatchedError`, `OttoFixturesPlugin`, `RunOptions`, `SuiteRunResult`, `UnknownSelectionError`, `run_tests`. The package keeps its name; renaming it is out of scope.

### 5.5 Nested test directories

Otto has three readers of a repo's test directories. Only the suite registry's `Repo.iter_test_files` stops at top-level files, deliberately, because it executes test files outside pytest. Pytest collection and the static parse already recurse and honor `norecursedirs`, and pytest already loads nested conftests.

With the registry gone, only those two readers remain, so nested test directories and conftests work everywhere, with pytest deciding what counts as a test.

**Collection errors.** Name resolution collects the whole tree once. A file that fails to collect is logged as an error naming the file, and the remaining files still resolve. The run session then targets only the matched tests, so a broken, unrelated file doesn't stop the tests that were asked for. Plain pytest would abort the whole session. This behavior is kept and pinned by a test with a broken nested file.

### 5.6 Test-file loading after the registry

Dispatch-startup made test files load on demand, through a loader on `SUITES`, and refuses non-suite registrations while they load. With the registry gone:

- `load_test_suites`, `Repo.iter_test_files`, `Repo.import_test_file`, `Repo.import_test_files` and the `SUITES` loader are deleted. No otto code executes a test file outside a pytest session.
- The refusal stays. Otto sets the same test-load phase marker around its in-process pytest sessions, collection and run. A test file or conftest that registers anything from outside the `otto` package raises `RegistrationRefused`, which tells the user to put extensions in an init module. Registrations from otto's own modules are exempt, as today.
- The guard test that enumerates every `Registry` instance keeps its purpose. No registry is exempt any more, since `SUITES` is gone, so the `Registry(accepts_test_files=...)` constructor flag is deleted.

## 6. Closing connections per event loop

### 6.1 What happens today

Every otto command closes every host it handed out when the command's event loop ends, through the context's `HostScope`. Inside pytest, each test class runs on its own loop, and plain functions run on one loop per module. A connection belongs to the loop that opened it. When pytest closes that loop, otto can no longer close the connection gracefully. After `pytest.main()` returns, `HostScope.rebuild_connections` abandons it, and the operating system drops the socket at exit. Only `--cov` closes connections while the class loop is still alive, through `OttoSuite._otto_release_connections`, because a clean shutdown is what makes a device write its coverage data and frees a single-client console.

The probe recorded on #457, run under `otto test` against lab hosts, found:

| Pattern | Today |
|---|---|
| Two classes use one host without closing it | The second class fails at once: "attached to a different loop" |
| A session fixture's host used from tests on their class loop | Each command hangs until its 30 s timeout |
| An unpinned async fixture used by tests pinned to a wider loop | It runs on the class loop anyway, and its `close()` hangs |
| Module- or session-pinned tests with matching fixtures | Work, with one connection across classes |

### 6.2 One host scope per event loop

`HostScope` generalizes from one per command to one per event loop:

- **Registration follows the loop.** When a host opens a connection, it registers with the scope of the running loop. Today hosts register when `get_host` hands them out. The plan locates the connection seam, since a host handed out on one loop may connect on another.
- **Each loop closes what it owns, while it is still running.** `OttoPlugin` adds autouse cleanup fixtures at class, module and session scope, each with the matching `loop_scope`. The class fixture's teardown closes the hosts the class loop owns, before pytest-asyncio closes that loop. Plain functions use the module loop's fixture. A fixture pinned to a wider loop keeps its connection until that loop ends.
- **The existing sweep order carries over.** A host that another registered host names as its `parent` closes after its dependents, and closes within a rank run concurrently.
- **The command-level scope** is the scope of the command's own loop, so `run_command` behaves exactly as today.

A closed host reconnects on first use, on whatever loop uses it next, so the first probe row passes after this change.

### 6.3 Behavior

- **Always on.** The `--cov` condition and `_otto_release_connections` are removed.
- **A debug log line per loop end** names the hosts it closed, for example `closed 2 hosts at end of TestRouter's loop: dut1, dut2`. Leaving a host open is otto's normal idiom, so this is debug level, not a warning.
- **A failed close doesn't fail the test.** It is logged as a warning naming the host, and the remaining hosts still close.
- **Bounded time.** Closing stays within the existing teardown deadline (`_resolve_teardown_deadline`).
- **The backstop stays.** After `pytest.main()` returns, anything still registered is abandoned as today and logged at debug level. After this change that should never happen, and a test asserts it doesn't in the normal case.

### 6.4 Otto's own async fixtures follow the test's loop

`_otto_ensure` and the monitor-event fixture are unpinned async fixtures. Otto forces `asyncio_default_fixture_loop_scope=class`, so they run on the class loop even when the test is pinned to a module or session loop. The converge's hosts then belong to a different loop from the test.

After this change, otto's per-test async work runs on the test's own loop. The plan chooses the mechanism, subject to one constraint: whatever the test's `loop_scope` is, the converge and the monitor events run on the loop the test body runs on. Candidate mechanisms are running them inside the test's own coroutine from `pytest_pyfunc_call`, or giving the fixtures a loop scope chosen per item. A test pins it for each of the function, class, module and session loop scopes.

### 6.5 Hosts fail fast on the wrong loop

Once hosts record the loop that owns their connection, which §6.2 needs anyway, a call from a different loop raises at once. The error names the host, says which fixture scope opened it, and points to the host-scoping cookbook page. It replaces today's raw asyncio `RuntimeError` or silent 30-second hang. A connection whose owning loop has already closed is not an error: it is dropped and the host reconnects, as `rebuild_connections` does today.

## 7. Documentation

- **Rewritten:** `docs/cookbook/authoring/writing-suites.md` (plain classes, `module_dir`, the `monitor` fixture, `ctx.options`), `docs/cookbook/authoring/options-classes.md` (registration by verb replaces inheritance as the sharing mechanism, which becomes optional), and `docs/cookbook/host-scopes.md` (drop the `OttoSuite` import and the "`ensure` and pinned tests" limitation once §6.4 lands).
- **Updated:** `writing-instructions.md` (injected verb-wide options), `docs/cookbook/host-scopes.md` (its examples select tests by `--tests`, which becomes positional names), `suite-recipes.md`, the monitoring pages and `docs/architecture/subsystems/monitoring.md` (the `monitor` fixture), the `otto test` CLI page (positional names, removed flags, `--list-tests` grouping), and `docs/overview.md`.
- **Architecture:** `docs/architecture/subsystems/completion-cache.md` (the schema change, no suite tree), `docs/architecture/testing.md` if it describes suites, and the extension-points page (the new options registry, the suite registry removed).
- **Examples and scaffolds:** `src/otto/examples/options.py`, the `otto init` templates (`src/otto/cli/init_templates.py`), and `docs/examples/getting-started/`.
- **CHANGELOG:** generated from commit subjects. The commit subject carries `!`, since the command line and the library API break.

## 8. Migration

No backwards compatibility is kept, per `AGENTS.md`. Measured on `4ae54ca7`, the files that name each removed or renamed symbol:

| Symbol | src | tests | docs |
|---|---|---|---|
| `OttoSuite` | 15 | 40 | 19 |
| `SUITES` | 7 | 27 | 4 |
| `register_suite_class` | 2 | 13 | 2 |
| `run_suite` | 9 | 14 | 2 |
| `run_selection` | 6 | 4 | 1 |
| `suite_options` | 3 | 12 | 7 |
| `suite_dir` | 3 | 5 | 4 |
| `--tests` | 12 | 18 | 14 |
| `--list-suites` | 3 | 8 | 4 |
| monitor helpers on `self` | 6 | 3 | 7 |
| `iter_test_files` / `load_test_suites` | 8 | 6 | 4 |

- **In-tree repos** (`tests/repo1`, `repo3`, `repo5`, `repo_e2e`, and `tests/_fixtures/shim_repo.py`): suites become plain classes, `Options =` becomes `register_options` in each repo's init module, and `suite_options` becomes `ctx.options(...)`.
- **Otto's own tests** of the removed paths are deleted or ported. The registry snapshot fixtures `_isolate_suites` and the `SUITES` half of `_isolate_registries` go away.
- **Import budget:** measured against #455's file-operation ceilings. The final report gives, for every surface, the file operations before and after this change, with the package and child-process breakdown the harness prints. Any ceiling this change raises is presented to Chris for explicit approval, with that breakdown, before it becomes the baseline. A regression is accepted deliberately or fixed, never absorbed by a silent regeneration.

## 9. Testing

1. **Probe first.** Task 1 of the plan re-runs the §6.1 probe as a committed integration test on the unix lab, expected red where today's behavior is wrong, so each later part turns a known red green.
2. **Command line:** each name form, class-name matching, mixed flag and name order, collection order under `--no-random`, the no-names-no-`-m` error, eager flags, did-you-mean on an unknown name, `--list-tests` grouping, and the dry run without imports.
3. **Options:** registration errors (unknown verb, empty list, duplicate class), the lazy string form, the collision rule across two repos and against a run flag, exit-2 validation before any body runs, `ctx.options` for registered, unregistered and other-verb classes, instruction injection, and `ensure` converging from the parsed `test` flags.
4. **Completion:** the shim and full paths agree on `NAMES` and on the merged flags (`tests/unit/shim/test_differential.py`), and a cache rebuild imports no test file and no pytest, pinned by the import budget.
5. **Plugin:** the banner and monitor events for plain functions, the `monitor` fixture's automatic stop and its database end stamp, `module_dir` and `test_dir` for plain functions, classes, parametrized tests, stability iterations and a two-repo run.
6. **Connections:** ownership per loop (a class-owned host, a module fixture shared across classes, a session fixture shared across modules), dependents closing before parents, a failed close logging without failing the test, the debug line, `ensure` under each loop scope, and the fail-fast error for a cross-loop call.
7. **Collection:** a nested test directory with its own conftest, and a broken nested file that doesn't block the selected tests.
8. **Registration refusal:** a test file and a conftest that call `register_options` each fail with `RegistrationRefused`.
9. **Bed lanes:** a `--cov` run on the Zephyr bed confirms the single-client console still frees up for the coverage collector.

## 10. Out of scope

- **#469:** honoring the repo's own `addopts` and `filterwarnings`, and pytest flag passthrough. Also letting a repo choose its default loop scope, which otto forces today.
- **Renaming the `otto.suite` package.**
- **File paths or pytest test IDs as names** (§3).
- **Suite-wide monitoring** across a class.
- **Verb-wide options for verbs other than `run` and `test`.**
