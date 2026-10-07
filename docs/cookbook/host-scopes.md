# Sharing host connections across tests

Opening a host connection costs a login: an SSH handshake, plus one more for
every hop or console server in the way. Tests that open a connection once
and reuse it pay that cost once. This page shows how widely a host is shared
under `otto test`, how to narrow that to one class or one test, and the rule
that decides which of those works.

## The rule: a connection lives on one event loop

A test gets a host from `get_host(id)` (`from otto.lab import get_host`).
The id is one of the hosts in the lab that `otto --lab <lab>` names;
`otto --lab my_lab --list-hosts` lists them. `get_host()` returns a host
without connecting to it. The connection opens on the host's first command,
and it belongs to the event loop that ran that command. Only code running on
that loop can use it afterwards. `get_host()`
returns the same host object every time it is asked for the same host, so
every test that uses `dut1` uses one connection, as long as they all run on
one loop.

Under `otto test`, every test and every async fixture runs on **one event
loop for the whole run**, unless it pins a narrower one. So by default a host
is shared across the whole run, and you write nothing to get that. otto
forces this with pytest-asyncio's settings `asyncio_mode=auto`,
`asyncio_default_test_loop_scope=session` and
`asyncio_default_fixture_loop_scope=session`, passed on pytest's command line,
so a repo's own pytest configuration can't change them; a pin, below, is the
way to get a narrower loop.

| Share one connection across | How |
| --- | --- |
| the whole run | nothing: the default |
| one class, then reconnect | a class-scoped fixture that closes the host after the class |
| one test, then reconnect | a function-scoped fixture that closes the host after the test |
| one class, on a loop of its own | pin the class to its own loop, and its fixtures with it |

A test uses a host from a fixture by naming the fixture:
`async def test_uplink(self, dut)` in a test class, or
`async def test_uplink(dut)` as a plain function. A fixture can live on the
class, at module level or in `conftest.py`.

Write async fixtures with `@pytest_asyncio.fixture`. A plain
`@pytest.fixture` on an `async def` also works under `otto test`, but it has
no `loop_scope` parameter, so it can't be pinned.

## One connection for the whole run

This is the default. The first test that runs a command on `dut1` connects,
every later test reuses that connection, and otto closes it when the run
ends. A session-scoped fixture in the `conftest.py` at the root of your tests
directory gives every test the same host by name:

```python
import pytest

from otto.lab import get_host


@pytest.fixture(scope="session")
def dut():
    return get_host("dut1")
```

The fixture doesn't close the host; otto does, at the end of the run (see
[Every loop closes the hosts it owns](#every-loop-closes-the-hosts-it-owns)).
A test may also call `get_host("dut1")` itself and get the same host.

## One connection per class or per test

To start each class, or each test, with a fresh connection, close the host at
the end of the fixture that hands it out. The next class or test reconnects
on first use. No pin is needed: the fixture and the tests all run on the
run's loop.

```python
import pytest_asyncio

from otto.lab import get_host


class TestRouter:
    @pytest_asyncio.fixture(scope="class")
    @classmethod
    async def dut(cls):
        host = get_host("dut1")
        yield host
        await host.close()  # the next class that uses dut1 reconnects

    async def test_uplink(self, dut) -> None: ...
```

A function-scoped fixture (`@pytest_asyncio.fixture` with no `scope`) that
closes the host after `yield` gives every test its own connection, which is
the most isolated choice and the slowest. The full class example, with setup
and teardown around it, is in
[Setup and teardown as fixtures](authoring/writing-tests.md#setup-and-teardown-as-fixtures).

## A loop of its own

A test, a class or a module can run on a loop of its own with the marker
`@pytest.mark.asyncio(loop_scope="class")`, or `"module"`, or `"function"`.
The hosts that loop connects belong to it, and otto closes them when that
loop ends. Closing a host after a class needs no pin (the section above
does it with a fixture). Pin when the code under test leaves things running
on the loop, such as background tasks, servers or callbacks, that must not
outlive the class or the test: when a pinned loop ends, whatever is still
running on it is cancelled with it.

```python
import pytest
import pytest_asyncio

from otto.lab import get_host


@pytest.mark.asyncio(loop_scope="class")
class TestFailover:
    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    @classmethod
    async def standby(cls):
        return get_host("dut2")

    async def test_takeover(self, standby) -> None: ...
```

otto's own fixtures (`ctx`, `module_dir`, `test_dir`, `expect`, `monitor`)
work in a pinned test as in any other. A pinned class's `ensure` marker
converges on the class's own loop, so the converge is subject to rule two
below: it fails on a host the run's loop already owns.

Two rules come with a pin:

- **Every async fixture that a pinned test uses should pin the same
  `loop_scope`.** That includes function-scoped fixtures. An unpinned one
  runs on the run's loop, and any host it connects belongs there, so the
  pinned test then fails on that host (rule two). A fixture pinned to a class
  loop must be class- or function-scoped: pytest-asyncio fails a module- or
  session-scoped fixture with `loop_scope="class"` at setup with
  `ScopeMismatch`. For a module pin, the fixtures take `loop_scope="module"`.
  Sync fixtures have no loop and need nothing.
- **A pinned test can only use hosts that nothing unpinned uses.** A host
  that any unpinned test or fixture has used stays owned by the run's loop
  until the run ends, and a pinned test that uses it fails at once with a
  {class}`~otto.host.loop_owner.HostLoopError`. Under otto's random test order,
  whether an unpinned test got there first changes from run to run, so give a
  pinned class hosts that only it uses. That includes the session-scoped
  `dut` fixture from [One connection for the whole run](#one-connection-for-the-whole-run):
  it is a sync fixture, so it returns the one shared host, and the host
  belongs to whichever loop first runs a command on it. If an unpinned test
  gets there first, a pinned class that uses `dut` fails with
  `HostLoopError`; if the pinned class goes first, it owns the host until its
  loop ends, closes it, and the next unpinned test reconnects.

A plain test function outside a class that is pinned to `"class"` or
`"function"` gets a loop of its own, since pytest has no class to share
there.

## Every loop closes the hosts it owns

When any loop ends, whether the run's, a module's, a class's or a test's,
otto closes every host that loop connected, while the loop is still running.
A host whose `parent` is another host closes before its parent, and hosts at
the same level close concurrently. A close that fails is logged as a warning
naming the host, and the rest still close; it never fails a test. With
`--log-level DEBUG`, a global option that goes before `test`
(`otto --log-level DEBUG --lab my_lab test TestRouter`), each loop logs what it
closed:

```text
closed 2 hosts at end of TestRouter's loop: dut1, dut2
```

So `await host.close()` in a fixture's teardown is optional. Write it when
you want the connection closed earlier than its loop ends: to reconnect
fresh for the next class, or to free a single-client console for someone
else.

## Shared connections share shell state

A shared host keeps one shell session, so a `cd` or an exported variable in
one test is still there in the next, and by default that is across the whole
run. When tests must not see each other's shell state, you have three
options:

- **Reset it** in a function-scoped fixture:

  ```python
  @pytest_asyncio.fixture
  async def clean_counters(dut):
      await dut.run("counters clear")
      yield
      await dut.run("counters dump")
  ```

- **Run stateless commands** with `exec` (`await dut.exec("ip -s link")`),
  as in {doc}`async-patterns`. Each call runs on a session the host doesn't
  keep, so no `cd` or exported variable carries over. A host with only a
  single console, such as an embedded device, is the exception: its `exec`
  shares that console.
- **Give a test its own named session,** as in {doc}`sessions`.

## Symptoms of a mismatch

- **`HostLoopError: host 'dut1' is connected on the session's loop but was
  used from TestFailover's loop.`** A pinned test, or a fixture pinned with
  it, used a host that another loop opened, here the run's. The message names
  both loops. Give the pinned tests hosts that only they use, or drop the pin.
  Between two narrower loops, close the host at the end of the scope that
  opened it.
- **`HostLoopError` at teardown.** A fixture closed a host from a different
  loop than the one that connected it, usually because the fixture isn't
  pinned like the tests that used the host. Pin the fixture's `loop_scope` to
  match the tests.
- **`ScopeMismatch: You tried to access the class scoped fixture
  _class_scoped_runner with a module scoped request object`** (or `session
  scoped`), at setup. This is pytest-asyncio's own message: a module- or
  session-scoped async fixture is pinned to a class loop
  (`loop_scope="class"`). Make the fixture class- or function-scoped, or pin
  it and the tests more widely.
