# Writing tests

otto tests are plain pytest: `Test`-prefixed classes and `test_`-prefixed
functions in `test_*.py` files, collected by pytest's own rules. There is no
base class to inherit and nothing to register. otto adds fixtures, markers
and a command line. This page is how to write tests; for running them, see
{doc}`../../cli/test/index`.

## An example repo

A **repo** is a directory with a `.otto/settings.toml` at its root. The
examples on this page and the other authoring pages use one repo, `acme`:

```text
acme/
├── .otto/
│   └── settings.toml         # tests = ["tests"], libs = ["pylib"], init = ["acme_instructions"]
├── pylib/
│   ├── acme_options.py       # options classes: RepoOptions, DeviceTestOptions
│   └── acme_instructions/
│       └── __init__.py       # the init module: registers the options classes
└── tests/
    ├── conftest.py           # fixtures shared by the tests below it
    └── test_device.py
```

- `tests` lists the repo's test directories.
- `libs` directories are put on `sys.path`, so a test imports
  `from acme_options import DeviceTestOptions`.
- `init` names the **init modules**, imported at startup for every command;
  registrations go there.

{doc}`../../configuration/settings` explains every setting, and
{doc}`../../cli/init` scaffolds this shape. Commands on this page leave out
`--lab`, which a real run passes before `test`:
`otto --lab my_lab test TestDevice`.

## A test file

`tests/test_device.py`:

```python
import logging

import pytest

from acme_options import DeviceTestOptions

logger = logging.getLogger(__name__)


class TestDevice:
    """Validate device configuration and connectivity."""

    async def test_device_reachable(self, ctx) -> None:
        """Verify the device responds to basic connectivity checks."""
        logger.info(f"firmware={ctx.options(DeviceTestOptions).firmware!r}")

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, ctx) -> None:
        """Fail if this takes longer than 30 seconds."""

    @pytest.mark.retry(2)
    async def test_management_plane(self) -> None:
        """Verify management-plane access (two attempts)."""

    @pytest.mark.integration
    async def test_interface_state(self, ctx) -> None:
        """Verify all expected interfaces are up (requires live device)."""
        if not ctx.options(DeviceTestOptions).check_interfaces:
            pytest.skip("Interface check disabled via --no-check-interfaces")

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Parametrized: runs once per interface name."""


def test_version_string_format() -> None:
    """A plain function is a test too."""
    assert "2.1".count(".") == 1
```

`otto test TestDevice` runs the class, `otto test test_interface_up` runs
every test of that name in any class or module, and
`otto test TestDevice --firmware 2.1` passes a flag.
{doc}`../../cli/test/selection` lists the name forms, and
[Markers](../../cli/test/index.md#markers) lists `timeout`, `retry` and the
other markers otto adds, and where your own markers such as `integration`
are declared.

`DeviceTestOptions` is an options class in `pylib/acme_options.py` with a
`firmware` field and a `bool` field, `check_interfaces`, which becomes the
pair `--check-interfaces/--no-check-interfaces`. The init module registers it
for the `test` **verb**, which is what puts its flags on `otto test`; a verb
is one of the two subcommands that take registered flags, `otto run` and
`otto test`. A test reads the values with `ctx.options(DeviceTestOptions)`.
Declaring, validating, registering and sharing options classes is
{doc}`options-classes`.

**Logging.** Put `logger = logging.getLogger(__name__)` at the top of the
file. Everything that logs during a run (your tests, otto, any library
either imports) reaches otto's console and log files; there is nothing to
register. Known-noisy libraries are quieted by default; the floor is
per-logger configurable in {ref}`[logging.levels] <logging-levels>`.

## Where otto looks

pytest collects each of the repo's test directories the way it always does:
every `test_*.py` file at any depth, each directory's `conftest.py`, and
`norecursedirs` honored. Nested test directories need no extra
configuration.

Test files and conftests are imported only inside `otto test`'s pytest
session, when it collects and runs tests, so a test file or conftest
registers **nothing**: an instruction, an options class or anything else it
tries to register is refused. The rule and the error are in
[Registering a class for a verb](options-classes.md#registering-a-class-for-a-verb).
A test file may still *import* from an init module.

## What every test gets

What otto adds arrives the way pytest delivers everything, as fixtures, and
nothing otto-specific lives on `self`. It applies to test classes and plain
functions alike.

| | free: runs for every test | on request: name it in the signature |
| --- | --- | --- |
| whole run | one event loop; the hosts it opened are closed when the run ends | `ctx` |
| per module | | `module_dir` |
| per test | the `ensure` marker's converge; a start banner in the log; monitor start/end events under `--monitor`; `expect` failures failing the test | `test_dir`, `expect`, `monitor` |

- `ctx`: the active {class}`~otto.context.OttoContext`.
  - `ctx.options(Cls)` is this run's instance of a registered options class
    ({doc}`options-classes`).
  - `ctx.cov` is whether this run collects coverage. Branch on it when a
    teardown would otherwise erase the counters; the example is in
    {ref}`coverage-awareness`.
- `module_dir`: a `Path` shared by every test in the module, for artifacts
  that belong to the whole file. A class that wants a shared space of its own
  makes a subdirectory.
- `test_dir`: a `Path` that is this test's own. Parametrized tests get unique
  names, and a repeating run adds one level per iteration.

  Both directories are created when first requested, like `tmp_path`, so a
  test that never names them leaves nothing behind. Where they sit under the
  run's output directory is in
  [Where a run's files go](../../cli/test/index.md#where-a-runs-files-go).
- `expect`: non-fatal assertions. `expect(cond, "why")` records a failure
  and keeps the test running; the test fails at the end with every failure
  listed, in the call phase like any other failure. A hard `assert` in the
  body still wins. See the
  [expect recipe](../test-recipes.md#non-fatal-assertions-with-expect).
- `monitor`: a per-test metrics monitor. `await monitor.start(hosts=[...])`
  starts collection and a live dashboard and returns its URL;
  `await monitor.event("label")` marks the timeline; `monitor.results()` and
  `monitor.events()` read what was collected, also after
  `await monitor.stop()`. The fixture stops a started monitor for you when
  the test ends. See the
  [monitoring recipe](../test-recipes.md#monitoring-from-a-test).

Hosts don't come from `ctx`: a test or fixture calls `get_host(id)`
(`from otto.config import get_host`) with the id of a host in the lab, as in
the fixtures below. {doc}`../host-scopes` covers how widely one connection
is shared.

## Event loops

otto runs pytest-asyncio in auto mode, so an `async def` test or fixture
needs no `@pytest.mark.asyncio`. Every test and every async fixture runs on
**one** event loop for the whole run unless it pins a narrower one with
`@pytest.mark.asyncio(loop_scope=...)`: a host connects on first use, every
later test reuses that connection, and otto closes it when the run ends.
{doc}`../host-scopes` is the home for loops: the pytest-asyncio settings otto
applies, when and how to pin, and the rules that come with a pin.

## Declaring lab state: the `ensure` marker

```python
@pytest.mark.ensure("clean", "installed")  # every test: cleanup, then a fresh install
class TestWidget:
    async def test_fresh_install_boots(self) -> None: ...

    @pytest.mark.ensure("installed")  # this test: one status sweep
    async def test_service_answers(self) -> None: ...

    @pytest.mark.ensure("none")  # this test: touch nothing
    async def test_reads_only(self) -> None: ...
```

The marker's arguments are a **path**: steps that run in the written order
before the test body. Each step does only the work the lab's state needs:

- `installed` runs each repo's `install` unless the lab is already
  installed; a partly installed lab is uninstalled first, then installed.
- `uninstalled` runs `uninstall` unless the lab is already fully
  uninstalled.
- `clean` runs `cleanup` unless the lab is already clean, which is stricter
  than uninstalled: dev tools and toolchain tools count too.
- `none`, the only step in its path, converges nothing; use it to exempt one
  test from its class's marker.

The closest marker wins outright (test, then class, then the module's
`pytestmark`) and nothing merges: a class path of `("clean", "installed")`
under a test marked `("installed")` gives that test `("installed")` alone. An
unmarked test converges nothing. Each step calls the same `otto.project`
function `otto run <verb> --ensure` calls, on the same event loop as the test
body; a convergence that fails **errors the test with the failing host
named**, never a skip ({class}`~otto.errors.EnsureStateError`). A misspelled
step stops the run at collection. What each project instruction does, and how
a repo customizes it, is {doc}`../../cli/run/defaults`; which of `otto test`'s
flags reach an install body is in
[Which flags reach an install body under `otto test`](options-classes.md#which-flags-reach-an-install-body-under-otto-test).

## Setup and teardown as fixtures

One shape, pytest's own: code before `yield` is setup, code after is
teardown, `scope` says how often it runs, `autouse` says whether every test
gets it or only the tests that ask.

```python
import logging

import pytest
import pytest_asyncio

from otto.config import get_host

logger = logging.getLogger(__name__)


@pytest.mark.ensure("installed")
class TestRouter:
    @pytest_asyncio.fixture(scope="class", autouse=True)
    @classmethod
    async def dut(cls, module_dir):
        host = get_host("dut1")  # once per class
        (module_dir / "boot.log").write_text((await host.run("dmesg")).only.value)
        yield host  # tests take it by name
        await host.close()  # optional: without it, otto closes it when the run ends

    @pytest_asyncio.fixture(autouse=True)
    async def _reset_counters(self, dut):  # before and after every test
        await dut.run("counters clear")
        yield
        await dut.run("counters dump")

    async def test_uplink(self, dut, expect, test_dir) -> None:
        result = (await dut.run("show uplink")).only
        expect("up" in result.value, "uplink down")
        (test_dir / "uplink.txt").write_text(result.value)

    @pytest.mark.ensure("clean", "installed")
    async def test_first_boot(self, dut) -> None: ...
```

`host.run(...)` returns a {class}`~otto.result.Results`, one entry per
command; `.only` asserts exactly one command ran and returns it, and `.value`
is its output ({doc}`../../architecture/utilities/results`).

- **Async fixtures use `@pytest_asyncio.fixture`.** A plain `@pytest.fixture`
  on an `async def` also works, but it takes no `loop_scope`, so it can't
  follow a pinned loop ({doc}`../host-scopes`).
- **A class-scoped fixture defined on the test class is a `@classmethod`**
  (fixture decorator on top, `classmethod` beneath). pytest gives the
  instance form a throwaway `self` whose attributes never reach the tests. A
  conftest fixture is a plain function.
- **`autouse` vs named.** `autouse=True` means "runs for every test whether
  or not it mentions it"; that is `setup_class`/`setup_method`. Leave it off
  for setup only some tests need; they request it by name. A fixture can be
  both, as `dut` is: every test gets it, and the ones that want the host
  name it.
- **Values travel as return values.** `cls.x = …` in a class fixture does
  reach the tests, but it is shared mutable state; `yield host` and
  `def test(self, dut)` is the idiom.
- **Depending on otto.** Any fixture may request `ctx`, `module_dir`,
  `test_dir`, `expect` or `monitor`; pytest orders by dependency. A
  class-scoped fixture can't request `test_dir`, `expect` or `monitor`,
  which are per test.
- **Ordering you may rely on:** pytest runs class-scoped fixtures before
  function-scoped ones and, within a scope, autouse fixtures before requested
  ones. The `ensure` converge is a function-scoped autouse fixture of otto's,
  so it runs **after** your class-scoped fixtures and **before** your
  function-scoped ones. A class-scoped fixture of yours therefore can't rely
  on the marker's converge having run; converge there yourself, or let the
  test do it. Beyond that, a fixture that needs another requests it.
- **Where fixtures live:** class-local ones as methods on the class; shared
  ones in `conftest.py`, or on a base class whose name does not start with
  `Test`, which pytest doesn't collect and whose subclasses inherit its
  fixtures.
- **Overriding:** a subclass redefines a fixture by name; a single test opts
  in with `@pytest.mark.usefixtures("name")`; `ensure` overrides at the
  closest marker.
- **Failure phases.** A fixture raising before `yield` → `ERROR` at setup,
  the body never runs, that fixture's teardown does not run either (guard
  partial setup with `try`/`finally`, as in plain pytest). After `yield` →
  `ERROR` at teardown alongside the body's own verdict. `expect` failures →
  `FAILED`.

pytest still honours `setup_method`/`setup_class` on any class. They are
synchronous and cannot request fixtures, so they are the second choice.

## Coming from unittest

If you have written `unittest`-style tests, the ideas map one to one; only
the spelling changes.

| you wrote | write instead |
| --- | --- |
| `class TestX(unittest.TestCase)` | `class TestX:`, no base class |
| `setup_class(cls)` / `teardown_class(cls)` | a class-scoped, autouse, `@classmethod` yield fixture: before / after `yield` |
| `setup_method(self)` / `teardown_method(self)` | a function-scoped autouse yield fixture (`self` is the test's instance) |
| `self.assertEqual(a, b)` | `assert a == b` |
