# Selecting hosts

An instruction or a test reaches the lab's hosts through two calls from
{mod}`otto.lab`: {func}`~otto.fleet.all_hosts` for a group, and
{func}`~otto.lab.get_host` for one particular device.

## One call for every host

```python
from otto.lab import all_hosts


async def uptime_everywhere() -> None:
    for host in all_hosts():
        await host.run("uptime")
```

Prefer this to naming each host. The code is the same whatever the lab holds:
a lab with two routers and a lab with twenty run it unchanged, and a host
added later is picked up without an edit. "Every host" means the fleet: the
hosts this run's projects select from the lab ([What the fleet
holds](#what-the-fleet-holds) has the detail).

## Narrowing by id

`pattern=` takes a compiled regex that is fully matched against each host's
id. A host's id is composed from its lab entry's element, board and slot
({ref}`host-identity` shows how), and `host.id` is what patterns match;
`host.name` is only a display name:

```python
import re

from otto.lab import all_hosts

routers = list(all_hosts(pattern=re.compile(r"router.*")))
```

The match is `re.fullmatch`, never `re.search`: `router` selects only the host
whose id is exactly `router`, so write `router.*` to select by prefix. A
pattern that matches nothing in a non-empty fleet raises
{class}`otto.lab.EmptySelectionError <otto.config.scope.EmptySelectionError>`
instead of yielding an empty walk, and so does one whose every match is held
out by `include_containers` or `include_local` (see
[What the fleet holds](#what-the-fleet-holds)).

A list comprehension that keeps nothing is not an error; it is an empty list.
A test that needs at least one host says so:

```python
from otto.lab import all_hosts

servers = [h for h in all_hosts() if h.id.startswith("server")]
assert servers, "this test needs at least one server in the lab"
```

## Grouping with list comprehensions

`all_hosts()` yields hosts, and a comprehension can read their
attributes. Each recipe below is a short block over the same walk.

By kind:

```python
from otto.host import UnixHost
from otto.lab import all_hosts

unix_hosts = [h for h in all_hosts() if isinstance(h, UnixHost)]
```

By element, the device a host belongs to ({doc}`../configuration/lab/elements`
defines elements in the lab file):

```python
from otto.lab import all_hosts

gateway_hosts = [h for h in all_hosts() if h.element.name == "gateway"]
```

By element metadata, the free-form table the lab author wrote on the element
(a host has its own `metadata` table too):

```python
from otto.lab import all_hosts

rev2 = [h for h in all_hosts() if h.element.metadata.get("hw_version") == "rev2"]
```

Two groups from one walk:

```python
from otto.lab import all_hosts

servers, others = [], []
for h in all_hosts():
    (servers if h.id.startswith("server") else others).append(h)
```

A regex and a comprehension together, the regex narrowing by id and the
comprehension by anything else:

```python
import re

from otto.host import UnixHost
from otto.lab import all_hosts

unix_servers = [h for h in all_hosts(pattern=re.compile(r"server.*")) if isinstance(h, UnixHost)]
```

`h.element` is set on every host of the default walk. A walk that opts in to
`local` or containers, or one through a project's own view (below), can yield
a host with no element, so guard the recipes that read it:
`h.element is not None and ...`.

## Acting on a group at once

{func}`~otto.fleet.run_on_all_hosts` runs the same commands on every matching
host concurrently, and {func}`~otto.fleet.do_for_all_hosts` calls any async
host method the same way (`UnixHost.exec` below is passed unbound, and runs
one command on a connection the host does not keep). Both narrow with
`pattern=`; neither takes a list you filtered yourself, so to act on a
comprehension's result, loop over it as in the `for` loop of the first
section:

```python
import re

from otto.host import UnixHost
from otto.lab import do_for_all_hosts, run_on_all_hosts


async def survey() -> None:
    versions = await run_on_all_hosts("uname -r", pattern=re.compile(r"router.*"))
    uptimes = await do_for_all_hosts(UnixHost.exec, "uptime")
```

Each returns a dict from host id to the result or to the exception that host
raised, so one unreachable host never costs you the others' results. Check a
value with `isinstance(value, BaseException)` before using it. The
[async patterns page](../extend/async-patterns.md) covers both in full.

(what-the-fleet-holds)=
## What the fleet holds

`all_hosts()` walks the run's fleet of interest: the hosts the active
projects' `[project]` declarations admit, or the whole loaded lab when none
declared one. The built-in `local` host and Docker container hosts are left
out unless asked for with `include_local=True` (on `all_hosts()` and
`do_for_all_hosts()`) or `include_containers=True`.
A walk through a project's own view (`self.ctx.all_hosts()` in a
`ProjectActions` override) also yields the containers that project owns,
without `include_containers=True`
({ref}`a project's own containers <fleet-owned-containers>`).
{doc}`../configuration/lab/project-scope` covers what decides the fleet, and
[The fleet of interest](../cli/run/defaults.md#the-fleet-of-interest) how the
run's flags change it. {doc}`host-scopes` is about something else: how widely
a host's connection is shared.

When the run's declared fleet, the union over the projects that declared a
scope, admits no host, the walk raises
{class}`~otto.bootstrap.ProjectScopeError` rather than quietly touching
nothing. Through a project's own view (`self.ctx.all_hosts()`), the walk
raises when that project's own scope admits no host, even if another
project's does.

## One specific host

```python
from otto.lab import get_host


async def reboot_dut() -> None:
    dut = get_host("dut1")
    await dut.run("reboot")
```

Use `get_host()` when you mean that device: a test pinned to one board. It
reaches any host the run may target, and `get_host("local")` always resolves
the local host. A lab host outside the run's scope raises
`otto.lab.HostOutOfScopeError`, a `KeyError` naming the settings file to widen
({ref}`project-scope-hosts`). An id the lab does not hold raises
`UnknownHostError`, also a `KeyError`, naming the ids the run may target.
