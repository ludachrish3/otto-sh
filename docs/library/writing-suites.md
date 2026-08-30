# Writing test suites

A **suite** is a `Test`-prefixed subclass of
{class}`~otto.suite.suite.OttoSuite`, which registers itself and becomes an
`otto test` subcommand. This page is how to write one. For running suites,
see {doc}`../guide/cli/test/index`.

## Defining a test suite

Create a `test_*.py` file in one of your repo's `tests` directories:

```python
import logging
from typing import Annotated

import pytest
import typer

from otto import options
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@options
class _Options:
    firmware: Annotated[
        str,
        typer.Option(
            help="Firmware version to validate against.",
        ),
    ] = "latest"

    check_interfaces: Annotated[
        bool,
        typer.Option(
            help="When True, verify all expected interfaces are up.",
        ),
    ] = True


class TestDevice(OttoSuite):
    """Validate device configuration and connectivity."""

    Options = _Options

    async def test_device_reachable(self, suite_options: _Options) -> None:
        """Verify the device responds to basic connectivity checks."""
        logger.info(f"firmware={suite_options.firmware!r}")
        assert True

    @pytest.mark.timeout(30)
    async def test_firmware_version(self, suite_options: _Options) -> None:
        """Verify the running firmware matches the expected version."""
        assert True

    @pytest.mark.retry(2)
    async def test_management_plane(self) -> None:
        """Verify management-plane access (retried up to 2 times)."""
        assert True

    @pytest.mark.integration
    async def test_interface_state(self, suite_options: _Options) -> None:
        """Verify all expected interfaces are up (requires live device)."""
        if not suite_options.check_interfaces:
            pytest.skip("Interface check disabled via --no-check-interfaces")
        assert True

    @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
    async def test_interface_up(self, interface: str) -> None:
        """Parametrized -- runs once per interface name."""
        assert True
```

## Suite registration

`OttoSuite.__init_subclass__` auto-registers any subclass whose name starts
with `Test` (matching pytest's own `python_classes = Test*` collection
rule). Registration:

1. Reads the inner `Options` dataclass
2. Converts each field into a Typer CLI parameter
3. Creates a runner function with the matching signature
4. Adds the suite as a subcommand of `otto test`

This all happens at import time, and *where* otto looks is narrower than
where pytest does — deliberately.

:::{important}
otto registers suites from the **top level** of each directory in `tests`:
`tests/test_device.py` yes, `tests/device/test_device.py` no. To nest, add
the subdirectory to the list — `tests` is a top-level key in
`.otto/settings.toml` and takes several paths:

```toml
# .otto/settings.toml
tests = ["tests", "tests/device"]
```

The reason is blast radius, not speed. These files are **executed** at
bootstrap on every otto command so that `__init_subclass__` fires, and a
failure in any of them exits non-zero for *every* command — so one broken
test file stops `otto host list`. Listing the directories keeps that surface
one you chose.

This bounds *registration* only. `otto test` hands the same directories to
pytest, which recurses as usual, so a nested `test_*` function still runs and
still completes under `--tests` — including the methods of a nested `Test*`
`OttoSuite`. Only the `otto test <Suite>` subcommand needs the file at the
top level of a listed directory.
:::

Auto-registration is one seam among many; see
{doc}`Extension points <../architecture/subsystems/extension-points>` for the
registry machinery behind this and every other way otto can be extended.

## Options classes

A suite's inner `Options` class is expanded into `otto test <Suite>` flags and
handed to each test method as `suite_options` — the test-suite stage of
otto's options lifecycle. See {doc}`options-classes` for how to define,
validate, and share an options class (including inheriting a repo-wide base
across suites).

## What every suite gets

A suite is a plain pytest class. What otto adds arrives the way pytest
delivers everything — as fixtures — and nothing otto-specific lives on
`self`.

| | free — runs for every test | on request — name it in the signature |
| --- | --- | --- |
| suite-wide | one event loop per suite; host connections released at class end under `--cov` | `suite_options` (class scope), `suite_dir` (class scope) |
| per test | the `ensure` marker's converge path; a start banner in the log; monitor start/end events; `expect` failures failing the test | `test_dir`, `expect`, `ctx` (session scope) |

- `suite_options` — the suite's `Options` instance ({doc}`options-classes`).
- `suite_dir` — `<run output dir>/<ClassName>`, a `Path` created when first
  requested; suite-wide fixtures write here; a test function outside a class
  gets `<run output dir>/<module stem>`. `test_dir` — `suite_dir/<test
  name>` (parametrized names sanitized), created when requested, like
  `tmp_path` — see the [artifact recipe](suite-recipes.md#per-test-artifact-directories).
- `expect` — non-fatal assertions: `expect(cond, "why")` records a failure
  and keeps the test running; the test fails at the end with every failure
  listed, in the call phase like any other failure. A hard `assert` in the
  body still wins. See the [expect recipe](suite-recipes.md#non-fatal-assertions-with-expect).
- `ctx` — the active {class}`~otto.context.OttoContext`.

**Logging.** Put `logger = logging.getLogger(__name__)` at the top of the
file, as the instruction example does; `otto test` routes every collected
suite module's logger into its console and log files. The captured name is
the collected module's top-level name — the bare module stem in a plain
`tests/` directory, or the package name (`tests`) when `tests/__init__.py`
makes it a package, in which case every logger under that package reaches
otto's sinks.

### One event loop per suite

Under `otto test` a class's tests, its class-scoped fixtures and its
function-scoped fixtures all run on **one** event loop. A host session opened
once in a class fixture is live in every test and closed once after the last.
Two rules follow:

- never write `loop_scope=` on a class- or function-scoped async fixture — a
  pin moves it *off* the suite's loop;
- a **module- or session-scoped** async fixture must pin `loop_scope` equal to
  its scope (`@pytest_asyncio.fixture(scope="session", loop_scope="session")`)
  or pytest-asyncio errors at setup with `ScopeMismatch: You tried to access
  the class scoped fixture _class_scoped_runner with a session scoped request
  object`. That message means exactly this rule.

A suite that wants a fresh loop per test marks the class
`@pytest.mark.asyncio(loop_scope="function")`.

### Declaring lab state: the `ensure` marker

```python
@pytest.mark.ensure("clean", "installed")  # every test: cleanup, then a fresh install
class TestWidget(OttoSuite):
    async def test_fresh_install_boots(self) -> None: ...

    @pytest.mark.ensure("installed")  # this test: one status sweep
    async def test_service_answers(self) -> None: ...

    @pytest.mark.ensure("none")  # this test: touch nothing
    async def test_reads_only(self) -> None: ...
```

The marker's arguments are a **path**: converge steps run in the written
order before the test body — `installed`, `uninstalled`, `clean`, or the
single step `none`. The closest marker wins outright — test, then class, then
the module's `pytestmark` — and nothing merges: a class path of `("clean",
"installed")` under a test marked `("installed")` gives that test
`("installed")` alone. An unmarked test converges nothing. Each step calls
the same `otto.project` function `otto run <verb> --ensure` calls, so a
marker and the command can never diverge; a convergence that fails **errors
the test with the failing host named** — never a skip
({class}`~otto.errors.EnsureStateError`). A misspelled step stops the run at
collection. What each verb converges, and how a repo customizes it, is
{doc}`../guide/cli/run/defaults`.

## Setup and teardown as fixtures

One shape, pytest's own: code before `yield` is setup, code after is
teardown, `scope` says how often it runs, `autouse` says whether every test
gets it or only the tests that ask.

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
        host = get_host(suite_options.device)  # once per suite
        (suite_dir / "boot.log").write_text((await host.run("dmesg")).only.value)
        yield host  # tests take it by name
        await host.close()  # once, after the last test

    @pytest.fixture(autouse=True)
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

- **A class-scoped fixture defined on the suite class is a `@classmethod`**
  (fixture decorator on top, `classmethod` beneath). pytest gives the
  instance form a throwaway `self` whose attributes never reach the tests. A
  conftest fixture is a plain function.
- **`autouse` vs named.** `autouse=True` means "runs for every test whether
  or not it mentions it" — that is `setup_class`/`setup_method`. Leave it off
  for setup only some tests need; they request it by name. A fixture can be
  both, as `dut` is: every test gets it, and the ones that want the host
  name it.
- **Values travel as return values.** `cls.x = …` in a class fixture does
  reach the tests, but it is shared mutable state; `yield host` and
  `def test(self, dut)` is the idiom.
- **Depending on otto.** Any fixture may request `suite_options`,
  `suite_dir`, `test_dir`, `expect` or `ctx`; pytest orders by dependency.
- **Ordering you may rely on:** class scope before function scope; within a
  scope, autouse before requested; and the `ensure` converge — otto's only
  plugin-level *function-scoped* autouse fixture — before any of yours. It is
  *function*-scoped, though, so that last promise covers your function-scoped
  fixtures only: a class-scoped fixture of yours runs **before** the converge,
  so never assume an installed lab in one — converge it there yourself, or let
  the test do it.
  Nothing finer. In particular otto's base-class autouse fixtures (the start
  banner, the monitor events, the connection release) carry **no** ordering
  promise relative to yours: pytest collects autouse fixtures along the node
  chain and alphabetically within each holder, so a conftest or class autouse
  fixture can run before them. A fixture that needs another requests it.
- **Where fixtures live:** suite-local ones as methods on the class; shared
  ones in `conftest.py` or on a base class whose name does not start with
  `Test` (`BaseRouter(OttoSuite)` — not registered, fixtures inherited).
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

If you have written `unittest`-style suites, the ideas map one to one; only
the spelling changes.

| you wrote | write instead |
| --- | --- |
| `setup_class(cls)` / `teardown_class(cls)` | a class-scoped, autouse, `@classmethod` yield fixture — before / after `yield` |
| `setup_method(self)` / `teardown_method(self)` | a function-scoped autouse yield fixture (`self` is the test's instance) |
| `self.assertEqual(a, b)` | `assert a == b` |

## Suite features

### Monitoring from a suite

Start the monitor during a test to collect metrics:

```python
async def test_performance(self) -> None:
    await self.start_monitor(hosts=[host1, host2])
    # ... run workload ...
    await self.add_monitor_event("workload started", color="blue")
    # ... wait for results ...
    await self.stop_monitor()
```
