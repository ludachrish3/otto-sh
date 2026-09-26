# Sharing host connections across tests

Opening a host connection costs a login: an SSH handshake, plus one more for
every hop or console server in the way. A suite that opens a connection once
and reuses it across many tests pays that cost once. This page shows how to
share a host for one test, one class, one module or the whole run under
`otto test`, and the rule that decides which of those works.

## The rule: a connection lives on one event loop

`get_host()` returns a host without connecting to it. The connection opens on
the host's first command, and it belongs to the event loop that ran that
command. Only code running on that loop can use it afterwards.

Under `otto test`, each test class runs on its own loop, and plain test
functions share one loop per module (see
[One event loop per suite](authoring/writing-suites.md#one-event-loop-per-suite)).
A test is **pinned** to a wider loop with the marker
`@pytest.mark.asyncio(loop_scope="module")` or `loop_scope="session"`, and an
async fixture is pinned with the same `loop_scope=` parameter on its
decorator. So a host can be shared exactly as widely as the tests that use it
share a loop:

| Share one connection across | Fixture | Tests run on |
| --- | --- | --- |
| one test | function-scoped | their class loop, the default |
| one class | class-scoped | their class loop, the default |
| one module | module-scoped, `loop_scope="module"` | the module loop, pinned for the file with `pytestmark` |
| the whole run | session-scoped, `loop_scope="session"` | the session loop, pinned for every test by a conftest hook |

**Every async fixture a test uses must run on that test's loop.** That
includes function-scoped fixtures. Otto puts async fixtures on the class loop
by default, so once you pin tests to a module or session loop, pin each async
fixture they use to the same loop.

Write async fixtures with `@pytest_asyncio.fixture`. A plain
`@pytest.fixture` on an `async def` also works under `otto test`, but it has
no `loop_scope` parameter, so it can't be pinned.

In every example below, a test uses the host by naming the fixture:
`async def test_uplink(self, dut)` in a suite class, or
`async def test_uplink(dut)` as a plain function. A fixture can live on the
class, at module level or in `conftest.py`. To run a file that mixes several
classes and plain functions, select its tests by name with `--tests` (see
{doc}`../cli/test/selection`).

## One connection per test

A function-scoped fixture connects before each test and disconnects after it.
Nothing carries over between tests, which makes this the most isolated and the
slowest choice.

```python
import pytest_asyncio

from otto.config import get_host


@pytest_asyncio.fixture
async def dut():
    host = get_host("dut1")
    yield host
    await host.close()
```

## One connection per class

This is the default shape: a class-scoped fixture on the suite class,
closed after the class's last test. The next class connects again on its own
loop. The full example, with setup and teardown around it, is in
[Setup and teardown as fixtures](authoring/writing-suites.md#setup-and-teardown-as-fixtures).

```python
import pytest_asyncio

from otto.config import get_host
from otto.suite import OttoSuite


class TestRouter(OttoSuite):
    @pytest_asyncio.fixture(scope="class")
    @classmethod
    async def dut(cls):
        host = get_host("dut1")
        yield host
        await host.close()

    async def test_uplink(self, dut) -> None: ...
```

Close the host in the fixture's teardown. A host that one class used and left
open fails in the next class that uses it (see
[Symptoms of a mismatch](#symptoms-of-a-mismatch)).

## One connection per module

Pin every test in the file to the module's loop with `pytestmark`, and give
the fixture the same `loop_scope`. Every class and plain function in the file
then uses one connection.

```python
import pytest
import pytest_asyncio

from otto.config import get_host

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def dut():
    host = get_host("dut1")
    await host.run("uname -a")  # connect now, on the module loop
    yield host
    await host.close()
```

Running one command in the fixture makes the connection belong to the
fixture's loop from the start. A fixture that only calls `get_host` and
yields leaves the connection to whichever test runs a command first, so a
test that isn't pinned can appear to work and fail later, once the fixture
grows some setup of its own.

## One connection for the whole run

Put the fixture in the `conftest.py` at the root of your tests directory, and
pin every async test to the session loop from the same file:

```python
import pytest
import pytest_asyncio
from pytest_asyncio import is_async_test  # True for async test items

from otto.config import get_host


def pytest_collection_modifyitems(items):
    session_loop = pytest.mark.asyncio(loop_scope="session")
    for item in items:
        if is_async_test(item):
            item.add_marker(session_loop, append=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def dut():
    host = get_host("dut1")
    await host.run("uname -a")  # connect now, on the session loop
    yield host
    await host.close()
```

Every test in the run, across modules and classes, now shares one connection
to `dut1`.

:::{warning}
This hook pins **every** async test under the conftest, not only the ones
that use `dut`, and it overrides a module's `pytestmark` pin. Every
class-scoped and module-scoped async fixture in that tree, including the
per-class and per-module shapes above, must then also say
`loop_scope="session"`. Each one that doesn't hangs at teardown until the
test times out. To mix scopes in one tree, narrow the hook, for example to
items whose `item.path` is under one directory.
:::

## Pinning your other async fixtures

Under a module or session pin, every async fixture a test uses needs the same
`loop_scope` as the tests, whatever its own scope. A function-scoped fixture
still runs once per test:

```python
@pytest_asyncio.fixture(loop_scope="session")  # still runs once per test
async def clean_counters(dut):
    await dut.run("counters clear")
    yield
    await dut.run("counters dump")
```

Sync fixtures have no loop and need nothing.

## Shared connections share shell state

A shared host keeps one shell session, so a `cd` or an exported variable in one
test is still there in the next. When tests must not see each other's shell
state, you have three options:

- **Reset it** in a function-scoped fixture, the `clean_counters` shape above.
- **Run stateless commands** with `exec`, as in {doc}`async-patterns`.
- **Give a test its own named session,** as in {doc}`sessions`.

## Symptoms of a mismatch

- **`RuntimeError: ... got Future <Future pending> attached to a different
  loop`.** A later class used a host that an earlier class opened and left
  open. Close the host in a class fixture's teardown, or share it at module or
  session scope as shown above.
- **A trivial command returns `Command timed out after 30.0s` as its
  output.** Nothing raises: the command returns after the host's 30-second
  timeout, and your assertion on its output fails. A module- or
  session-scoped fixture's host was used from tests that are still on their
  class loop. Pin the tests with `pytestmark` or the conftest hook.
- **The test passes, then the item is reported `ERROR` at teardown with
  `Failed: Timeout (>N s) from pytest-timeout`.** The stacks pytest-timeout
  prints show no test or fixture frames. An async fixture that isn't pinned
  closed a host that a pinned test used. Pin the fixture's `loop_scope` to
  match the tests.
- **`ScopeMismatch: You tried to access the class scoped fixture
  _class_scoped_runner with a module scoped request object`** (or `session
  scoped`), at setup. A module- or session-scoped async fixture is missing
  its `loop_scope`. Give it `loop_scope` equal to its `scope`, as the rule in
  [One event loop per suite](authoring/writing-suites.md#one-event-loop-per-suite)
  says.

## Current limitation: `ensure` and pinned tests

The [`ensure` marker](authoring/writing-suites.md#declaring-lab-state-the-ensure-marker)
converges the lab from an otto fixture that always runs on the class loop,
even when the test is pinned to a module or session loop. The hosts it
connects then belong to a different loop from the test. Don't combine
`ensure` with module- or session-pinned tests yet.
