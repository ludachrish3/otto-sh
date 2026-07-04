# Host Database

Otto builds its lab — the set of hosts a command can touch — from a **host
source**. By default that source is the `hosts.json` files under your `labs`
directories, but the source is a pluggable backend: point otto at a CMDB, an
inventory API, or any system of record by implementing one small interface.

```{note}
Choosing a host source is a one-time, team-level decision — part of setting otto
up for your team. See the {ref}`team-setup-checklist` in {doc}`repo-setup`.
```

Otto is strictly a consumer of host data. It reads hosts; it never writes back
to your source of record.

## The interface

A host source implements the [`LabRepository`](../api/storage.rst) protocol —
two read-only methods:

`load_lab(name, preferences=None) -> Lab`
: Build and return the named lab. Raises
  [`LabNotFoundError`](../api/storage.rst) if the name is unknown.

`list_labs() -> list[str]`
: The lab names this source can provide.

Configuration is supplied at construction time, so a backend is built once and
then queried.

## Quick start: the built-in JSON source

The default backend is `"json"`: it reads `hosts.json` from each directory in
your `labs` setting. No `[lab]` block is required — a repo with just
`labs = [...]` already uses it:

```toml
name = "my_project"
version = "1.0.0"

labs = ["${sut_dir}/lab_data"]
```

Writing it out explicitly is equivalent:

```toml
[lab]
backend = "json"
```

The per-host `hosts.json` schema — every field, and how labs merge — lives in
{doc}`lab-config`.

```{tip}
Running `otto init` (or `otto init --lab`) scaffolds a `hosts.json` with one
example entry and a `lab_data/README.md` walking through its fields — a
faster way to see a valid entry than building one from scratch. See
{doc}`../getting-started`.
```

### Annotating entries with `_`-prefixed keys

`hosts.json` is plain JSON, which has no comment syntax. Any key beginning
with `_` (e.g. `_comment`) on a host entry is stripped before validation, so
it is otto's sanctioned way to leave a note inline without tripping the
schema's `extra="forbid"` check:

```json
{
    "_comment": "Replace before connecting to a real host.",
    "ip": "192.0.2.1",
    "element": "example-device",
    "os_type": "unix",
    "valid_terms": ["ssh"],
    "creds": [{ "login": "admin", "password": "CHANGE_ME" }],
    "labs": ["example_lab"]
}
```

This idiom is scoped to host entries only — it is not a general convention
elsewhere in otto's JSON/TOML configuration.

## Selecting a different source

`[lab] backend` selects any **registered** backend by name. Register your
backend from an `init` module (one of the modules listed in `init = [...]`),
then name it in settings:

```python
# my_lab_source.py  (listed in init = [...])
from otto.storage import register_lab_repository
from my_company.cmdb import CmdbLabRepository

register_lab_repository("cmdb", CmdbLabRepository)
```

```toml
[lab]
backend = "cmdb"

[lab.cmdb]
url = "https://cmdb.example.com"
```

Otto constructs the backend as
`CmdbLabRepository(repo_dir=<repo root>, url="https://cmdb.example.com")` — the
`[lab.<name>]` sub-table becomes keyword arguments, plus `repo_dir` for
resolving any relative paths. Selecting an unregistered name raises
[`LabRepositoryError`](../api/storage.rst), listing the registered names.

```{note}
This is the same named-registry mechanism otto uses everywhere else
(`register_term_backend`, `register_reservation_backend`, `register_host_class`).
An `init` module always imports before the lab is loaded, so the name is
registered by the time settings select it.
```

## Writing a custom backend

A backend is any class satisfying the two-method protocol. Otto ships a small,
dependency-free reference implementation —
[`otto.examples.lab_repository.ExampleLabRepository`](../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to host dicts and builds real hosts with
[`create_host_from_dict`](../api/storage.rst) so each becomes a `RemoteHost`
keyed by its `id` — which is what the rest of otto expects.

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
```

Loading an unknown lab raises the contract's error — never a bare `KeyError` or
`None`:

```{doctest}
>>> from otto.storage import LabNotFoundError
>>> try:
...     repo.load_lab("does-not-exist")
... except LabNotFoundError:
...     print("not found")
not found
```

## Error contract

A backend signals trouble through two exceptions (from
[`otto.storage`](../api/storage.rst)):

[`LabNotFoundError`](../api/storage.rst)
: `load_lab` was asked for a name the backend does not know. Raise this — never
  return `None` or raise a bare `KeyError`.

[`LabRepositoryError`](../api/storage.rst)
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

## Troubleshooting

`"Unknown lab repository backend '...'"`
: `[lab] backend` names a backend that was never registered. Check the name, and
  confirm the `init` module that calls `register_lab_repository(...)` is listed
  in `init = [...]`.

`LabNotFoundError: Lab '...' not found`
: The backend has no lab by that name. Check `--lab` / `OTTO_LAB` against
  `list_labs()`.
