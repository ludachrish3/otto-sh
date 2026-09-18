# Boards of interest

A lab file can describe more equipment than one repository cares about. The
bed has sixteen hosts; a BusyBox project wants five. `[project]` in
`settings.toml` says which:

```{literalinclude} ../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin project"
:end-before: "# doc: end project"
```

Both are {ref}`fullmatched regexes <project-scope>` — `bb`
would match nothing, `bb.*_qemu` matches the five guests and not `test1`,
which is also a member of the `busybox` lab. The host id is what is matched:
`bb1350_qemu`, the element name plus the board slug otto appends.

What the declaration changes is the fleet every walk starts from.
`all_hosts()` and {meth}`~otto.context.OttoContext.do_for_all_hosts` iterate
the **fleet of interest**, not the lab, and a walk that would iterate
nothing refuses loudly rather than silently doing nothing. The same
computation, without a connection, run from the example project's
directory, after its init modules are imported as a real `otto` run does
at startup:

```{testsetup}
import os

from otto.config.repo import Repo
from otto.registry import registering_repo

_old_cwd = os.getcwd()
os.chdir(GS_EXAMPLE)
_init_repo = Repo(sut_dir=GS_EXAMPLE)
with registering_repo(_init_repo.name):
    _init_repo.import_init_modules()
```

```{testcleanup}
os.chdir(_old_cwd)
```

```{doctest}
>>> from pathlib import Path
>>> from otto.config.lab import load_lab
>>> from otto.config.repo import Repo
>>> from otto.config.scope import resolve_scopes, scoped_ids
>>> repo = Repo(sut_dir=Path.cwd())
>>> lab = load_lab("busybox", search_paths=[Path("lab_data")])
>>> sorted(lab.hosts)
['bb1161_qemu', 'bb1211_qemu', 'bb1281_qemu', 'bb1310_qemu', 'bb1350_qemu', 'local', 'test1']
>>> scopes = resolve_scopes([repo], lab.component_names, lab.hosts, exclude_ids=frozenset({"local"}))
>>> sorted(scoped_ids(lab.hosts, scopes, None))
['bb1161_qemu', 'bb1211_qemu', 'bb1281_qemu', 'bb1310_qemu', 'bb1350_qemu']
```

`test1` is in the lab and out of the fleet. Naming it explicitly —
`otto host test1 …` — still works: explicit targeting is never scoped.
Container hosts and the built-in `local` host are held out of every walk by
default regardless of the declaration.

{doc}`../configuration/lab-config` (*Project scope*) is the reference,
including what happens when several repositories declare scopes over one
lab; {doc}`../cli/run/defaults` shows the walk from an instruction's
point of view, including the two memberships held out of it.
