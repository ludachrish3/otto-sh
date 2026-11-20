# Lab source backends

Otto reads its hosts through **host-data sources** declared in
`[[lab.sources]]` — see {doc}`../configuration/host-sources` for the
declaration syntax and merge order. The `json` backend ships with otto;
anything else (a CMDB, an inventory API, a scheduler's asset list) is a
backend you register from your own project. This page is that contract.

## The interface

A host source implements the [`LabRepository`](../api/labs.rst) protocol —
two read-only methods:

`load_lab(name, *, preferences=None, inventory=None) -> Lab`
: Build and return the named lab. `inventory` is the process inventory that
  referenced host entries resolve against; when it is `None`, a referenced
  entry is an error. Raises
  [`LabNotFoundError`](../api/labs.rst) if the name is unknown. Populate the
  reservation identifiers at every level your equipment uses: `Lab.resources`
  for what the lab reserves as a whole, and, on each host it builds,
  `element.resources` for the element it belongs to and `resources` for the
  host itself. Both host-side sets are `frozenset[str]`; a host built through
  [`create_host_from_dict`](../api/host/index.rst) gets the element's set
  from the `Element` passed as `element=` and the host's own from the host
  dict's `resources` key. See {doc}`../cli/reservation/index` for what
  the three levels mean.

  Every host in the returned lab's `hosts` must be a
  {class}`~otto.host.remote_host.RemoteHost`, stored under its own `id`.
  `Lab.hosts` is typed more loosely than that, but the contract is not:
  [`assert_lab_repository_conforms`](#verify-your-backend) fails a host of any
  other type, and one keyed by anything but its `id`.

`list_labs() -> list[str]`
: The lab names this source **declares**. This is not a convenience listing:
  otto decides a lab *exists* from it, so a name you omit here cannot be
  loaded even if `load_lab` would happily build it.

Configuration is supplied at construction time, so a backend is built once and
then queried. How otto constructs it is [Registering a backend](#registering-a-backend).

```{important}
Return a **fresh `Lab`** from every `load_lab` call. When more than one source
is configured, otto merges the sources' labs **in place** — so a backend that
caches one `Lab` object and hands it back from every call would eventually
return a lab an earlier merge has already mutated.
```

```{warning}
**A level you leave empty is a level nobody reserves.** The gate reads
`Lab.resources` *and* each in-play host's `element.resources` and `resources`;
a backend that sets only the first under-reserves **silently** — the check
passes, and two runs land on the same slot. Nothing catches it for you:
`assert_lab_repository_conforms` compares `Lab.resources` across calls and does
not inspect the host-level sets. If your equipment really is reservable only as
whole labs, leaving both host-side fields empty is the correct declaration —
just make it a decision rather than an omission.
```

### One optional capability

`list_host_summaries(*, inventory=None) -> list[HostSummary]`
: Enumerate hosts *without building them*, for tab completion and tunnel
  path-narrowing. `inventory` carries the same meaning as `load_lab`'s (see
  the {doc}`migration note <inventory-backends>`); otto always passes it by
  keyword, and `assert_lab_repository_conforms` checks the signature.
  Implementing
  [`SupportsHostSummaries`](../api/labs.rst) is purely an optimization —
  otto detects it structurally, and a backend that omits it still gets
  completion, because otto falls back to `list_labs()` + `load_lab()`.

  If you do implement it, a summary must agree with the host `load_lab()`
  builds — in three ways, all checked by `assert_lab_repository_conforms`:

  - **Every id you return must be one `load_lab()` produces**, or completion
    offers names that cannot dispatch. Derive ids with
    `host_identity(record, element=element, profiles=...)` (the same `element` and
    `profiles` you pass to `create_host_from_dict`) rather than formatting
    your records by hand: it
    applies the same profile merge and validation the host factory applies,
    which hand-formatting silently gets wrong (a numeric field arriving as
    `3.0`, or an `os_profile` that supplies `board`/`slot`). See
    [`host_identity`](../api/host/index.rst).
  - **Every host `load_lab()` produces must be summarized.** Otherwise
    completion simply stops offering it, and nothing anywhere says so.
  - **Every FIELD must match**, not just `id`. `HostSummary`'s fields have
    defaults so the dataclass will let you omit them, but each one drives a
    surface: `labs` scopes `otto host -l <lab> <TAB>` (and must be exactly the
    labs that contain the host — claiming one it is not in offers an id that
    cannot dispatch there), `docker_capable` gates `otto docker --parent`,
    `docker_priority` picks the default docker parent (highest wins), `ip`
    drives tunnel narrowing, `os_type` decides which verbs
    `otto host <id> <TAB>` offers (left `None`, it offers every class's), and
    `logins` (one `LoginSummary` per login: `login`, `protocols`, `proxy`)
    is what `--user <TAB>` offers. The conformance helper compares every one
    of these against the constructed host.

  `lab_patterns` is the one field a backend may legitimately leave empty. A
  backend whose membership is
  *pattern*-based — the json one, where an element joins labs by regex — fills
  it with the element's patterns and lets the composite re-resolve them
  against every source's declared labs, so `labs` ends up complete across
  sources rather than complete only within this one. A backend that already
  knows its concrete lab names sets `labs` and leaves `lab_patterns` empty.

  Otto also bounds how long it will wait for your backend during completion
  (2 seconds by default). If yours is legitimately slower, raise
  `OTTO_COMPLETION_HOST_TIMEOUT`; otto logs a warning naming it rather than
  hanging the user's shell. Completion then offers none of your backend's
  hosts, and that incomplete answer stays in the completion cache for at most
  five minutes ({doc}`../architecture/subsystems/completion-cache`,
  "Freshness"). A source that is regularly slower than the default should have
  the timeout raised, not rely on the cache asking again every five minutes.

### Named inventories: the view capabilities

A process can hold several named inventories, and a host entry can reference
one of them with a qualified `{"name": "<inventory>", "key": "<key>"}`
reference ({doc}`../configuration/inventory`). Otto keeps them in an
[`InventoryView`](../api/inventory.rst). A backend that wants qualified
references to reach their inventory implements any of four further optional
capabilities. Otto detects each one structurally, as it detects
`list_host_summaries`:

`load_lab_in_view(name, *, preferences=None, view) -> Lab` ([`SupportsInventoryView`](../api/labs.rst))
: `load_lab`, with every entry resolved through the whole view by
  `resolve_view_entry(record, view=view, element=element)` instead of
  `resolve_host_entry`.

`list_host_summaries_in_view(*, view) -> list[HostSummary]` ([`SupportsHostSummariesInView`](../api/labs.rst))
: `list_host_summaries`, under the same contract and the same conformance
  rules, resolved through the view. Fill each summary's `inventory_ref` from
  the resolved entry's `ref`. That is the qualified identity it came from.

`list_lab_status(*, view) -> list[LabStatus]` ([`SupportsLabStatus`](../api/labs.rst))
: One [`LabStatus`](../api/labs.rst) per lab you declare. A lab that cannot
  be loaded now carries a `Diagnostic(where=..., reason=...)` as
  `unavailable`. Without this method, every `list_labs()` name is available.

`lab_scope() -> LabScope | None` ([`SupportsLabScope`](../api/labs.rst))
: The lab names you can declare at all: exact `names` and literal
  `prefixes` (a prefix is plain text, not a pattern). `None` means unscoped.
  Otto never loads a lab outside the scope from you, even when you are
  healthy, so your elements reach only labs inside it. A status for a lab
  outside it is refused as a broken backend. In return, when you fail, only
  the labs inside your scope fail
  ({ref}`source-failure-scope`).

Otto reaches every source through
[`load_lab_through`, `lab_statuses`, `lab_scope_of`](../api/labs.rst) and
[`host_summaries_in_view`](../api/labs.rst), which choose the capability when
it exists and today's call when it does not. **A backend with none of these
keeps working unchanged.** Its `load_lab` and `list_host_summaries` receive,
as `inventory`:

- the process `default` inventory, exactly as before, in a project that has
  no `[[inventories]]` or binds `default` itself;
- `None` when no `default` is configured;
- a refusing stand-in, {class}`~otto.inventory.RefusedDefault`, in a project
  that declares `[[inventories]]` without binding `default`. Its `lookup`
  refuses each bare reference with
  {class}`~otto.inventory.BareReferenceRefused`, and the refusal is
  recorded once as a completion drop, so a backend that swallows
  `InventoryError` in its summaries still loses the entry visibly.

```{note}
The refusal holds for a backend that resolves references through
`resolve_host_entry`, `resolve_view_entry` or `inventory.lookup`. A backend
that reads references some other way (its own record store, or a field it
interprets itself) is outside this guarantee: otto cannot refuse what it
never sees.
```

Through `inventory=` alone a backend reaches `default` and nothing else:
`resolve_host_entry` refuses a qualified reference to any other inventory,
saying it needs a named-inventory view. Implement `load_lab_in_view` (and
`list_host_summaries_in_view`) for those references to resolve.

## Writing a custom backend

A backend is any class satisfying the two required methods (plus, optionally,
`list_host_summaries`). Otto ships a small,
dependency-free reference implementation —
[`otto.examples.lab_repository.ExampleLabRepository`](../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to element dicts — each grouping its own host
dicts — builds one `Element` per group, resolves each host dict's inventory
reference with `resolve_host_entry(record, inventory=inventory, element=element)` (a
pass-through when the record carries no `inventory` key), and builds real
hosts with [`create_host_from_dict`](../api/host/index.rst) (`element=`
that same `Element`, `inventory_ref=` the resolution's `ref`, `profiles=` the
data profiles its factory kept from `c.env.profiles`) so each becomes
a `RemoteHost` keyed by its `id`, as [the interface](#the-interface) requires.
Note where its resources live: a *second* mapping, lab name to
resource set, mirroring `lab.json`'s `labs` table. That is the lab level
only — the sample's routers are reserved as whole labs, so no host dict
carries a `resources` key and no group dict carries one either. A backend for
chassis-and-slot equipment fills those in too.

The shipped sample works out of the box and demonstrates the contract:

```{doctest}
>>> from otto.examples.lab_repository import ExampleLabRepository
>>> repository = ExampleLabRepository()
>>> repository.list_labs()
['east', 'west']
>>> lab = repository.load_lab("east")
>>> lab.name
'east'
>>> sorted(lab.hosts)
['router1']
>>> sorted(lab.resources)
['router1']
```

Loading an unknown lab raises the contract's error — never a bare `KeyError` or
`None`:

```{doctest}
>>> from otto.labs import LabNotFoundError
>>> try:
...     repository.load_lab("does-not-exist")
... except LabNotFoundError:
...     print("not found")
not found
```

## Registering a backend

Register a backend from an `init` module, with
{func}`~otto.labs.register_lab_repository`: a name, the model that parses a
source's options, and the factory that builds the source. It is a
{ref}`configured backend <configured-backends>`, and the
shape is the same as every other configured seam's:

```python
# .otto/init.py — listed in init = [...] in .otto/settings.toml
from otto.labs import register_lab_repository
from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source

register_lab_repository("example", config=ExampleLabSourceConfig, factory=example_lab_source)
```

A `[[lab.sources]]` entry then selects it, and every key but `backend` and
`name` is an option the config model parses:

```toml
[[lab.sources]]
backend = "example"
```

What otto does with it, and when:

- **At settings parse**, only the entry's envelope is checked: `backend`,
  `name`, and that labels are unique within the project. The options are kept
  as they were written.
- **After every project's `init` modules have run**, otto prepares each source:
  it calls the config model's `model_validate(options, context={"env": env})`
  once. `env` is a {class}`~otto.labs.LabSourceEnv`: the declaring project's root
  (`project_root`, where a relative path anchors), the source's `label`, its
  `origin` (the settings file) and `profiles`, the selected projects'
  `[os_profiles]` tables as a {class}`~otto.host.ProfileContext`. Keep
  `profiles` and pass it as `profiles=` to every host helper you call
  (`create_host_from_dict`, `host_identity`, `validate_host_dict`, each with
  the element), so a
  host's `os_type` may name a table any selected project declares; without it,
  such a host is refused as an unknown `os_type`. An unknown option or a bad
  value fails here,
  naming the source's settings file. Preparing a source while an init module
  is still being imported is refused, because registration is not complete
  then; so is building one, and so is {func}`otto.session.build_lab`.
- **When a lab is loaded**, otto calls the factory once per source with
  `Configured(config=..., env=...)` and checks that what it returns has callable
  `load_lab` and `list_labs`. What a config model must be is stated once,
  under {ref}`configured-backends`.

The factory is where your constructor's signature lives; otto never calls the
class itself. The shipped sample's factory is one line:

```python
def example_lab_source(c: Configured[ExampleLabSourceConfig, LabSourceEnv]) -> ExampleLabRepository:
    return ExampleLabRepository(labs=c.config.labs, resources=..., profiles=c.env.profiles)
```

(lab-source-config-model)=
### A config model of your own

A CMDB source takes the server's URL and, optionally, a CA bundle file. The
config model parses both, and its validator anchors a relative `ca_bundle`
to the root of the project that declares the source, which it reads from
`info.context["env"].project_root`:

```python
# my_lab_source.py  (listed in init = [...])
from pathlib import Path

from pydantic import ConfigDict, ValidationInfo, field_validator

from otto.labs import LabSourceEnv, register_lab_repository
from otto.models import OttoModel
from otto.registry import Configured
from my_company.cmdb import CmdbLabRepository


class CmdbConfig(OttoModel):
    model_config = ConfigDict(frozen=True)

    url: str
    ca_bundle: Path | None = None

    @field_validator("ca_bundle", mode="after")
    @classmethod
    def _anchor(cls, value: Path | None, info: ValidationInfo) -> Path | None:
        if value is None or value.is_absolute():
            return value
        return info.context["env"].project_root / value


def cmdb_source(c: Configured[CmdbConfig, LabSourceEnv]) -> CmdbLabRepository:
    return CmdbLabRepository(
        url=c.config.url, ca_bundle=c.config.ca_bundle, profiles=c.env.profiles
    )


register_lab_repository("cmdb", config=CmdbConfig, factory=cmdb_source)
```

`OttoModel` refuses an unknown key, so a typo in the source's options fails
when the source is prepared, naming the field and the settings file. The
anchored path is part of the parsed configuration, so the factory never
needs `project_root` itself; when it does, it reads `c.env.project_root`.

### Files and the completion cache

Otto's completion cache invalidates a source's hosts when the files it reads
change, but only if it knows which files those are. A config model tells it
by defining `prepared_facts()`, returning an object whose `file_inputs` is a
tuple of anchored paths: directories (their `lab.json` is read), `.json`
files, or globs over `.json` files, by the json backend's rule. Otto
fingerprints those files, and the directories they were found in, when it
writes the cache.

A source whose config model defines no `prepared_facts()` is not file-backed:
nothing it reads can invalidate the cache, so otto gives every entry written
for the workspace the short TTL (five minutes). A source whose backend is not
registered yet is reported as unprepared (`otto cache info`), and
`otto init`'s doctor names it as checked after init.

## Error contract

A backend signals trouble through two exceptions (from
[`otto.labs`](../api/labs.rst)):

[`LabNotFoundError`](../api/labs.rst)
: `load_lab` was asked for a name the backend does not know. Raise this — never
  return `None` or raise a bare `KeyError`.

[`LabRepositoryError`](../api/labs.rst)
: Any other failure (I/O, network, parse, credentials) that prevents a
  definitive answer. `LabNotFoundError` is a subclass, so callers can catch the
  base.

Otto raises a third, {class}`~otto.labs.LabSourceConstructionError` (a
`LabRepositoryError` and a `ValueError`), when a source cannot be prepared or
built: its backend is not registered, its options do not parse, your factory
raised, or it returned something that is not a lab source. Your backend does
not raise it; the message names the stage, the backend, the module that
registered it and the settings file that declared the source.

## Verify your backend

Otto ships a conformance helper that checks a backend against the full contract
and reports **every** violation at once (it raises a single `AssertionError`
listing each failed rule). The shipped sample conforms:

```{doctest}
>>> from otto.testing import assert_lab_repository_conforms
>>> from otto.examples.lab_repository import ExampleLabRepository
>>> assert_lab_repository_conforms(
...     ExampleLabRepository(), expected_labs=["east", "west"]
... )
```

Call it from your own test suite, passing `expected_labs=[...]` to also assert
specific labs are present and loadable against your known fixtures:

```python
from otto.host import ProfileContext
from otto.labs import LabSourceEnv
from otto.registry import Configured
from otto.testing import assert_lab_repository_conforms
from my_lab_source import CmdbConfig, cmdb_source


def test_cmdb_conforms(tmp_path):
    env = LabSourceEnv(
        project_root=tmp_path, label="test/cmdb", origin="test", profiles=ProfileContext.empty()
    )
    config = CmdbConfig.model_validate({"url": "https://cmdb.example.com"}, context={"env": env})
    assert_lab_repository_conforms(cmdb_source(Configured(config=config, env=env)))
```

The source is built as otto builds it: the options parsed with the
environment, then the factory, so the test covers what your registration
hands otto.

If your repository implements the optional `list_host_summaries`, pass
`expect_host_summaries=True` as well. The capability is legitimately absent from
many backends, so the helper skips its rules by default — which means dropping
the method during a later cleanup leaves conformance green while completion
quietly falls back to loading every lab. The kwarg turns that absence into a
named failure, so the fast path you meant to keep is one you are told about
losing.

### Inventory references in your test data

The helper loads every lab with `load_lab(name)` and no inventory by default,
so a lab whose hosts reference an inventory (`"inventory": "dut1"`) fails to
load. Pass the inventory your fixture's references resolve through as
`inventory=`: every load, the repeated load, `expected_labs` and the summaries
cross-check then bind it, exactly as otto binds the process `default`:

```{doctest}
>>> import json, tempfile
>>> from pathlib import Path
>>> from otto.examples.lab_repository import ExampleLabRepository
>>> from otto.inventory import JsonInventory
>>> from otto.testing import assert_lab_repository_conforms
>>> creds = [{"login": "admin", "password": "admin"}]
>>> repository = ExampleLabRepository(
...     labs={"east": [{"name": "router1", "hosts": [{"inventory": "dut1", "creds": creds}]}]}
... )
>>> with tempfile.TemporaryDirectory() as tmp:
...     path = Path(tmp) / "inventory.json"
...     _ = path.write_text(json.dumps({"dut1": {"ip": "10.0.0.5"}}))
...     assert_lab_repository_conforms(
...         repository, inventory=JsonInventory(path), expected_ips={"router1": "10.0.0.5"}
...     )
```

`expected_ips` maps a host id to the address it must load at, and to the
address its summary must give when you offer summaries. It is the check a
backend cannot pass by accident: one that resolves a reference somewhere else
loads, but at the wrong address.

If your data holds qualified references to named inventories, pass a `view=`
instead: an [`InventoryView`](../api/inventory.rst) holding those inventories
by name. Every probe then goes through
[`load_lab_through` and `host_summaries_in_view`](../api/labs.rst), the
adapters otto itself uses, so your backend is checked on the route it is
reached by. Pass `expected_ips` for a host whose reference names an inventory
other than `default`: it is what fails a backend that ignores the view.

### What the helper checks for each capability

Each optional capability is checked on its own, only when your backend
implements it ([the view capabilities](#named-inventories-the-view-capabilities)):

| Capability | The rule |
| ---------- | -------- |
| `list_lab_status` | The statuses name exactly the labs `list_labs()` returns, and every unavailable status states a reason. A lab reported unavailable is never loaded by any probe. |
| `lab_scope` | It returns `None` or a `LabScope`, and every lab you report is inside it. |
| `load_lab_in_view`, without `view=` | Under the view `inventory=` stands for (it reaches `default` only), each lab loads to the same hosts at the same addresses as `load_lab(name, inventory=inventory)`. |
| `load_lab_in_view`, with `view=` | The two loads are never compared, because `load_lab` rightly refuses a qualified reference to a named inventory. `expected_ips` is checked on the loads through the view instead. |
| `list_host_summaries` and `list_host_summaries_in_view` | The summaries and the loads agree id for id and field for field, under the same binding. |

The unknown-name probe expects {class}`~otto.labs.LabNotFoundError`. A
{class}`~otto.labs.LabUnavailableError` is accepted instead when it reports a
failed source (`declared=False`), on the error's own word: the helper does not
check the failed source's scope. One that reports a declaration
(`declared=True`) fails, because nothing declares an unknown name.
