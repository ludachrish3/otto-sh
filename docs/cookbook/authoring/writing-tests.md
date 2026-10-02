# Writing tests

otto tests are pytest tests. Everything pytest documents about writing tests
applies to them unchanged, so pytest's documentation is the reference for
pytest itself. This page covers only what otto adds: its fixtures, the
`ensure` marker, the `timeout` and `retry` markers, per-test monitoring,
artifact directories and options. For running tests, see
{doc}`../../cli/test/index`.

## pytest's terms

These pages use pytest's words for the parts of a test:

| Term | In an otto repo | pytest docs |
| --- | --- | --- |
| test function | a `test_`-prefixed function or method; pytest runs each one as a test | {external+pytest:ref}`Test discovery <test discovery>` |
| test class | a `Test`-prefixed class that groups test methods; other frameworks call this a suite | [Group tests in a class](https://docs.pytest.org/en/stable/getting-started.html#group-multiple-tests-in-a-class) |
| test module | a `test_*.py` file | {external+pytest:ref}`Test discovery <test discovery>` |
| test directory | a directory the repo's `tests` setting lists; pytest collects the test modules in it at any depth | {external+pytest:ref}`Test discovery <test discovery>` |
| `conftest.py` | a file whose fixtures every test in its directory, and below, can use | {external+pytest:ref}`conftest.py <conftest.py>` |
| fixture | setup and teardown a test requests by naming it as a parameter | {external+pytest:doc}`how-to/fixtures`, {external+pytest:doc}`reference/fixtures` |
| marker | a `@pytest.mark.NAME` label on a test, class or module; `-m` selects by it | {external+pytest:doc}`how-to/mark` |
| parametrize | `@pytest.mark.parametrize`, which runs one test once per value | {external+pytest:doc}`how-to/parametrize` |

Tests written with `unittest.TestCase` run too, the way pytest runs them:
{external+pytest:doc}`how-to/unittest`.

## An example repo

A **repo** is a directory with a `.otto/settings.toml` at its root. The
examples on this page and the other authoring pages use one repo, `acme`:

```text
acme/
├── .otto/
│   └── settings.toml         # tests = ["tests"], libs = ["pylib"], init = ["acme_instructions"]
├── pylib/
│   └── acme_instructions/
│       └── __init__.py       # the init module: options classes and instructions
└── tests/
    ├── conftest.py           # fixtures shared by the tests below it
    └── test_device.py
```

- `tests` lists the repo's test directories.
- `libs` directories are put on `sys.path`, so a test imports
  `from acme_instructions import DeviceTestOptions`.
- `init` names the **init modules**, which otto imports at startup for every
  command. Anything you register with otto (an options class, an
  instruction) is registered there.

{doc}`../../configuration/settings` explains every setting, and
{doc}`../../cli/init` scaffolds this shape. Commands on this page leave out
`--lab`, which a real run passes before `test`:
`otto --lab my_lab test TestDevice`.

## A test module

`tests/test_device.py`:

```python
import logging

import pytest

from acme_instructions import DeviceTestOptions

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
    """A test function outside any class."""
    assert "2.1".count(".") == 1
```

`otto test TestDevice` runs the class, `otto test test_interface_up` runs
every test of that name in any class or module, and
`otto test TestDevice --firmware 2.1` passes a flag.
{doc}`../../cli/test/selection` lists the name forms.
[Markers](../../cli/test/index.md#markers) is the home for `timeout`,
`retry` and the other markers otto runs with, and for where your own
markers, such as `integration`, are declared.

**Logging.** Put `logger = logging.getLogger(__name__)` at the top of the
file. Everything that logs during a run (your tests, otto, any library
either imports) reaches otto's console and log files. Known-noisy libraries
are quieted by default; the floor is per-logger configurable in
{ref}`[logging.levels] <logging-levels>`.

## Options

An **options class** turns its fields into flags on `otto test`. Declare it
in the init module, and name in the decorator each **verb** whose flags it
joins: `test` for `otto test`, `run` for every `otto run` command.

```python
# pylib/acme_instructions/__init__.py, the init module
from typing import Annotated

import typer

import otto


@otto.options(verbs=["test"])
class DeviceTestOptions:
    firmware: Annotated[str, typer.Option(help="Firmware version to validate.")] = "latest"
    check_interfaces: Annotated[
        bool, typer.Option(help="Also check every interface's link state.")
    ] = True
```

`otto test` now takes `--firmware` and the pair
`--check-interfaces/--no-check-interfaces`, and `otto test --help` lists
them. A test imports the class from the init module and reads this run's
values with `ctx.options(DeviceTestOptions)`, as the test module above does.

**The one rule:** the class must be defined in an init module, or in a
module an init module imports; a test module or `conftest.py` cannot register
one (why:
[Registering a class for a verb](options-classes.md#registering-a-class-for-a-verb)).
Validating fields, the lazy `register_options` form, sharing fields and
the rest are in {doc}`options-classes`.

## What every test gets

otto's additions arrive as fixtures: a test, or another fixture, names one
in its signature. They work the same in a test class and in a test function.

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
  starts collection (no dashboard; archive with `db_path=` and review it with
  `otto monitor <file>.db`);
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

Setup and teardown are pytest fixtures ({external+pytest:doc}`how-to/fixtures`):
code before `yield` is setup, code after it is teardown, `scope` says how
often it runs, and `autouse` says whether every test gets it or only the
tests that name it. The example below uses otto's fixtures and a lab host
inside its own:

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
  (fixture decorator on top, `classmethod` beneath); the instance-method
  form is
  {external+pytest:ref}`deprecated <class-scoped-fixture-as-instance-method>`.
  A fixture in `conftest.py` is a plain function.
- **Any fixture may request otto's.** `ctx`, `module_dir`, `test_dir`,
  `expect` and `monitor` are fixtures like any other. A class-scoped fixture
  can't request `test_dir`, `expect` or `monitor`, which are per test.
- **The `ensure` converge runs between your class-scoped and your
  function-scoped fixtures.** It is a function-scoped autouse fixture, and
  pytest runs class-scoped fixtures first and, within a scope, autouse
  fixtures before requested ones ({external+pytest:ref}`fixture order`). A
  class-scoped fixture of yours therefore can't rely on the marker's
  converge having run; converge there yourself, or let the test do it.
