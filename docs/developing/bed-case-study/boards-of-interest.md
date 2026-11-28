# Boards of interest

A lab file can describe more equipment than one repository cares about. The
bed has sixteen hosts in four labs; this project works on the five BusyBox
guests, two of the Ubuntu VMs, the Python REPL host and the Zephyr boards.
`[project]` in `settings.toml` says which:

```{literalinclude} ../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin project"
:end-before: "# doc: end project"
```

Both are {ref}`fullmatched regexes <project-scope>` — `bb`
would match nothing, `bb.*-qemu` matches the five guests, and `test1` matches
`test1` alone, not `test1-py`. The host id is what is matched:
`bb1350-qemu`, the element name plus the board slug otto appends.

What the declaration changes is the fleet every walk starts from.
`all_hosts()` and {meth}`~otto.context.OttoContext.do_for_all_hosts` iterate
the **fleet of interest**, not the lab, and a walk that would iterate
nothing refuses loudly rather than silently doing nothing.
{func}`otto.lab.fleet_of_interest <otto.fleet.fleet_of_interest>`
computes it without a connection. Here it runs from the example project's
directory, after its init modules are imported as a real `otto` run does at
startup:

```{testsetup}
import os

from otto.config import Project
from otto.registry import registering_project

_old_cwd = os.getcwd()
os.chdir(GS_EXAMPLE)
_init_project = Project(root=GS_EXAMPLE)
with registering_project(_init_project.name):
    _init_project.import_init_modules()
```

```{testcleanup}
os.chdir(_old_cwd)
```

```{doctest}
>>> from pathlib import Path
>>> from otto.config import Project
>>> from otto.lab import fleet_of_interest
>>> from otto.session import build_lab
>>> project = Project(root=Path.cwd())
>>> lab = build_lab([project], labs=["unix"])
>>> sorted(lab.hosts)
['local', 'test1', 'test2', 'test3']
>>> sorted(fleet_of_interest(lab, projects=[project]))
['test1', 'test2']
```

Each lab narrows the same way. Here is what each of the other three leaves
outside the fleet:

```{doctest}
>>> for name in ["busybox", "embedded", "pyrepl"]:
...     lab = build_lab([project], labs=[name])
...     fleet = fleet_of_interest(lab, projects=[project])
...     print(name, "outside:", sorted(set(lab.hosts) - set(fleet) - {"local"}))
busybox outside: []
embedded outside: ['test4']
pyrepl outside: []
```

`test3` and `test4` are in their labs and out of the fleet. `test4` still
carries the Zephyr boards' connections, because a host reached as a hop needs
only lab membership, but `otto --lab embedded host test4 …` is refused,
naming this project's `settings.toml` to widen ({ref}`project-scope-hosts`).
Container hosts and the built-in `local` host are held out of every **walk**
by default regardless of the declaration.  That is a walk's choice, not the
scope's: as a **target**, `local` always resolves (`otto host local`), and a
container is targetable exactly when its parent is.

{doc}`../../configuration/lab/project-scope` is the reference,
including what happens when several repositories declare scopes over one
lab; {doc}`../../cli/run/defaults` shows the walk from an instruction's
point of view, including the two memberships held out of it.
