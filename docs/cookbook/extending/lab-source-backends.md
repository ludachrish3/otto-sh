# Lab source backends

Otto reads its hosts through **host-data sources** declared in
`[[lab.sources]]` — see {doc}`../../configuration/host-sources` for the
declaration syntax and merge order. The `json` backend ships with otto;
anything else (a CMDB, an inventory API, a scheduler's asset list) is a
backend you register from your own repo. This page is that contract.

## The interface

A host source implements the [`LabRepository`](../../api/labs.rst) protocol —
two read-only methods:

`load_lab(name, preferences=None, inventory=None) -> Lab`
: Build and return the named lab. `inventory` is the process inventory that
  referenced host entries resolve against; when it is `None`, a referenced
  entry is an error. Raises
  [`LabNotFoundError`](../../api/labs.rst) if the name is unknown. Populate the
  reservation identifiers at every level your equipment uses: `Lab.resources`
  for what the lab reserves as a whole, and, on each host it builds,
  `element.resources` for the element it belongs to and `resources` for the
  host itself. Both host-side sets are `frozenset[str]`; a host built through
  [`create_host_from_dict`](../../api/host/index.rst) gets the element's set
  from the `Element` passed as `element=` and the host's own from the host
  dict's `resources` key. See {doc}`../../cli/reservation/index` for what
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

`list_host_summaries(inventory=None) -> list[HostSummary]`
: Enumerate hosts *without building them*, for tab completion and tunnel
  path-narrowing. `inventory` carries the same meaning as `load_lab`'s (see
  the {doc}`migration note <inventory-backends>`); otto always passes it by
  keyword, and `assert_lab_repository_conforms` checks the signature.
  Implementing
  [`SupportsHostSummaries`](../../api/labs.rst) is purely an optimization —
  otto detects it structurally, and a backend that omits it still gets
  completion, because otto falls back to `list_labs()` + `load_lab()`.

  If you do implement it, a summary must agree with the host `load_lab()`
  builds — in three ways, all checked by `assert_lab_repository_conforms`:

  - **Every id you return must be one `load_lab()` produces**, or completion
    offers names that cannot dispatch. Derive ids with
    `host_identity(record, element, profiles=...)` (the same `element` and
    `profiles` you pass to `create_host_from_dict`) rather than formatting
    your records by hand: it
    applies the same profile merge and validation the host factory applies,
    which hand-formatting silently gets wrong (a numeric field arriving as
    `3.0`, or an `os_profile` that supplies `board`/`slot`). See
    [`host_identity`](../../api/host/index.rst).
  - **Every host `load_lab()` produces must be summarized.** Otherwise
    completion simply stops offering it, and nothing anywhere says so.
  - **Every FIELD must match**, not just `id`. `HostSummary`'s fields have
    defaults so the dataclass will let you omit them, but each one drives a
    surface: `labs` scopes `otto host -l <lab> <TAB>` (and must be exactly the
    labs that contain the host — claiming one it is not in offers an id that
    cannot dispatch there), `docker_capable` gates `otto docker --parent`, and
    `ip` drives tunnel narrowing.

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
  hanging the user's shell.

## Writing a custom backend

A backend is any class satisfying the two required methods (plus, optionally,
`list_host_summaries`). Otto ships a small,
dependency-free reference implementation —
[`otto.examples.lab_repository.ExampleLabRepository`](../../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to element dicts — each grouping its own host
dicts — builds one `Element` per group, resolves each host dict's inventory
reference with `resolve_host_entry(record, inventory, element)` (a
pass-through when the record carries no `inventory` key), and builds real
hosts with [`create_host_from_dict`](../../api/host/index.rst) (`element=`
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
>>> repo = ExampleLabRepository()
>>> repo.list_labs()
['east', 'west']
>>> lab = repo.load_lab("east")
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
...     repo.load_lab("does-not-exist")
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
  `name`, and that labels are unique within the repo. The options are kept
  as they were written.
- **After every repo's `init` modules have run**, otto prepares each source:
  it calls the config model's `model_validate(options, context={"env": env})`
  once. `env` is a {class}`~otto.labs.LabSourceEnv`: the declaring repo's root
  (`repo_dir`, where a relative path anchors), the source's `label`, its
  `origin` (the settings file) and `profiles`, the selected repos'
  `[os_profiles]` tables as a {class}`~otto.host.ProfileContext`. Keep
  `profiles` and pass it as `profiles=` to every host helper you call
  (`create_host_from_dict`, `host_identity`, `validate_host_dict`), so a
  host's `os_type` may name a table any selected repo declares; without it,
  such a host is refused as an unknown `os_type`. An unknown option or a bad
  value fails here,
  naming the source's settings file. Preparing a source while an init module
  is still being imported is refused, because registration is not complete
  then; so is building one, and so is {func}`otto.session.build_lab`.
- **When a lab is loaded**, otto calls the factory once per source with
  `Configured(config, env)` and checks that what it returns has callable
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
to the root of the repo that declares the source, which it reads from
`info.context["env"].repo_dir`:

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
        return info.context["env"].repo_dir / value


def cmdb_source(c: Configured[CmdbConfig, LabSourceEnv]) -> CmdbLabRepository:
    return CmdbLabRepository(
        url=c.config.url, ca_bundle=c.config.ca_bundle, profiles=c.env.profiles
    )


register_lab_repository("cmdb", config=CmdbConfig, factory=cmdb_source)
```

`OttoModel` refuses an unknown key, so a typo in the source's options fails
when the source is prepared, naming the field and the settings file. The
anchored path is part of the parsed configuration, so the factory never
needs `repo_dir` itself; when it does, it reads `c.env.repo_dir`.

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
[`otto.labs`](../../api/labs.rst)):

[`LabNotFoundError`](../../api/labs.rst)
: `load_lab` was asked for a name the backend does not know. Raise this — never
  return `None` or raise a bare `KeyError`.

[`LabRepositoryError`](../../api/labs.rst)
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
        repo_dir=tmp_path, label="test/cmdb", origin="test", profiles=ProfileContext.empty()
    )
    config = CmdbConfig.model_validate({"url": "https://cmdb.example.com"}, context={"env": env})
    assert_lab_repository_conforms(cmdb_source(Configured(config, env)))
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
