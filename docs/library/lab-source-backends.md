# Lab source backends

Otto reads its hosts through **host-data sources** declared in
`[[lab.sources]]` — see {doc}`../guide/configuration/host-sources` for the
declaration syntax and merge order. The `json` backend ships with otto;
anything else (a CMDB, an inventory API, a scheduler's asset list) is a class
you register from your own repo. This page is that contract.

## The interface

A host source implements the [`LabRepository`](../api/labs.rst) protocol —
two read-only methods:

`load_lab(name, preferences=None) -> Lab`
: Build and return the named lab. Raises
  [`LabNotFoundError`](../api/labs.rst) if the name is unknown. Populate the
  reservation identifiers at every level your equipment uses: `Lab.resources`
  for what the lab reserves as a whole, and, on each host it builds,
  `element_resources` for the element it belongs to and `resources` for the
  host itself. Both host-side fields are `frozenset[str]`; a host built through
  [`create_host_from_dict`](../api/host/factory.rst) gets them from the
  `element_resources=` keyword and the host dict's own `resources` key. See
  {doc}`../guide/cli/reservation/index` for what the three levels mean.

`list_labs() -> list[str]`
: The lab names this source **declares**. This is not a convenience listing:
  otto decides a lab *exists* from it, so a name you omit here cannot be
  loaded even if `load_lab` would happily build it.

Configuration is supplied at construction time, so a backend is built once and
then queried.

```{important}
Return a **fresh `Lab`** from every `load_lab` call. When more than one source
is configured, otto merges the sources' labs **in place** — so a backend that
caches one `Lab` object and hands it back from every call would eventually
return a lab an earlier merge has already mutated.
```

```{warning}
**A level you leave empty is a level nobody reserves.** The gate reads
`Lab.resources` *and* each in-play host's `element_resources` and `resources`;
a backend that sets only the first under-reserves **silently** — the check
passes, and two runs land on the same slot. Nothing catches it for you:
`assert_lab_repository_conforms` compares `Lab.resources` across calls and does
not inspect the host-level sets. If your equipment really is reservable only as
whole labs, leaving both host-side fields empty is the correct declaration —
just make it a decision rather than an omission.
```

### One optional capability

`list_host_summaries() -> list[HostSummary]`
: Enumerate hosts *without building them*, for tab completion and tunnel
  path-narrowing. Implementing
  [`SupportsHostSummaries`](../api/labs.rst) is purely an optimization —
  otto detects it structurally, and a backend that omits it still gets
  completion, because otto falls back to `list_labs()` + `load_lab()`.

  If you do implement it, a summary must agree with the host `load_lab()`
  builds — in three ways, all checked by `assert_lab_repository_conforms`:

  - **Every id you return must be one `load_lab()` produces**, or completion
    offers names that cannot dispatch. Derive ids with
    [`host_identity`](../api/host/factory.rst) rather than formatting your
    records by hand: it applies the same profile merge and validation the host
    factory applies, which hand-formatting silently gets wrong (a numeric
    field arriving as `3.0`, or an `os_profile` that supplies `board`/`slot`).
  - **Every host `load_lab()` produces must be summarized.** Otherwise
    completion simply stops offering it, and nothing anywhere says so.
  - **Every FIELD must match**, not just `id`. `HostSummary`'s fields have
    defaults so the dataclass will let you omit them, but each one drives a
    surface: `labs` scopes `otto host -l <lab> <TAB>` (and must be exactly the
    labs that contain the host — claiming one it is not in offers an id that
    cannot dispatch there), `element` and `element_id` synthesize the
    positional handles (`dut1`), `docker_capable` gates `otto docker --on`,
    and `ip` drives tunnel narrowing.

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
[`otto.examples.lab_repository.ExampleLabRepository`](../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to host dicts and builds real hosts with
[`create_host_from_dict`](../api/host/factory.rst) so each becomes a `RemoteHost`
keyed by its `id` — which is what the rest of otto expects. Note where its
resources live: a *second* mapping, lab name to resource set, mirroring
`lab.json`'s `labs` table. That is the lab level only — the sample's routers
are reserved as whole labs, so no host dict carries a `resources` key and
nothing is passed as `element_resources`. A backend for chassis-and-slot
equipment fills those in too.

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
from otto.testing import assert_lab_repository_conforms
from my_lab_source import CmdbLabRepository


def test_cmdb_conforms():
    assert_lab_repository_conforms(CmdbLabRepository(repo_dir="."))
```
