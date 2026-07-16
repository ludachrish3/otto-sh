# Host Database

Otto builds its lab — the set of hosts a command can touch — from a **host
source**. By default that source is the `lab.json` files under your `labs`
directories, but the source is a pluggable backend: point otto at a CMDB, an
inventory API, or any system of record by implementing one small interface.

```{note}
Choosing a host source is a one-time, team-level decision — part of setting otto
up for your team. See the {ref}`team-setup-checklist` in {doc}`repo-setup`.
```

Otto is strictly a consumer of host data. It reads hosts; it never writes back
to your source of record.

## The interface

A host source implements the [`LabRepository`](../../api/labs.rst) protocol —
two read-only methods:

`load_lab(name, preferences=None) -> Lab`
: Build and return the named lab. Raises
  [`LabNotFoundError`](../../api/labs.rst) if the name is unknown.

`list_labs() -> list[str]`
: The lab names this source can provide.

Configuration is supplied at construction time, so a backend is built once and
then queried.

## Quick start: the built-in JSON source

The default backend is `"json"`: it reads `lab.json` from each directory in
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

The per-host `lab.json` schema — every field, and how labs merge — lives in
{doc}`lab-config`.

```{tip}
Running `otto init` (or `otto init --lab`) scaffolds a `lab.json` with one
example entry and a `lab_data/README.md` walking through its fields — a
faster way to see a valid entry than building one from scratch. See
{doc}`../../getting-started`.
```

### Annotating entries with `_`-prefixed keys

`lab.json` is plain JSON, which has no comment syntax. Any key beginning
with `_` (e.g. `_comment`) on a host or link entry is stripped before
validation, so it is otto's sanctioned way to leave a note inline without
tripping the schema's `extra="forbid"` check:

```json
{
    "hosts": [
        {
            "_comment": "Replace before connecting to a real host.",
            "ip": "192.0.2.1",
            "element": "example-device",
            "os_type": "unix",
            "valid_terms": ["ssh"],
            "creds": [{ "login": "admin", "password": "CHANGE_ME" }],
            "labs": ["example_lab"]
        }
    ],
    "links": []
}
```

This idiom is scoped to host and link entries only — it is not a general
convention elsewhere in otto's JSON/TOML configuration.

## Credentials and login proxies

A host's `creds` field is an **ordered list** of cred entries, each with a
required `login` and four optional fields:

| Field | Type | Description |
|-------|------|--------------|
| `login` | string | The account name (required). |
| `password` | string or `null` | Password, or omit/`null` for key/agent auth on SSH (an empty line on telnet). |
| `proxy` | string | Name of a registered login proxy (see {doc}`../hosts/extending-backends`) that drives the steps to *become* this login, after authenticating as `via`. Omit for a directly-loginable account — a proxy-less entry still uses the built-in `"su"` proxy when `switch_user`/`as_user` switches to it. |
| `via` | string | The `login` of another entry in this same list to authenticate as first. Only valid alongside `proxy`. Omit to default to the first proxy-less (directly-loginable) entry. |
| `params` | object | Free-form data handed to the proxy callable (e.g. a container name, a service name) — otto itself never interprets it. |

**The first entry is the default login** — the user otto authenticates as
unless `user` names a different entry:

```json
"creds": [
    {"login": "admin", "password": "hunter2"},
    {"login": "mysql", "proxy": "mysql-su", "via": "admin",
     "params": {"service": "mysqld"}}
]
```

Here otto logs in as `admin` by default. Setting `"user": "mysql"` on the
host entry (or calling `switch_user("mysql")` at runtime) authenticates as
`admin` first, then runs the `mysql-su` proxy to become `mysql`.

Validated at load, alongside the usual schema checks: every `login` is
unique; `via`/`params` are only allowed alongside `proxy`; `via` must name
another entry in the same list, never itself; a chain of `via` links must
terminate at a proxy-less entry (a cycle is rejected at load, not discovered
mid-connection); and `proxy` names are checked against the live login-proxy
registry the same way `term`/`transfer` selectors are checked against theirs
— an unregistered name fails loud, listing what's registered, instead of
failing later mid-connection.

### Ownership when a login is proxied

Every *command* surface (`run`, `exec`, named sessions) executes as the
proxied user once a session has switched to it — but file **transfer** is not
uniform, because not every transfer protocol rides a shell:

- `nc` transfers ride pooled, already-proxied shell sessions, so a file it
  puts lands owned by the **target** (proxied) user.
- `scp` / `sftp` / `ftp` authenticate at the transport layer directly as the
  resolved *direct* (`via`) cred — they cannot replay proxy steps, since they
  are not interactive shells — so a file they put lands owned by the **via**
  user, not the proxied target.

Pick `nc` (`"transfer": "nc"`, or include it in `valid_transfers`) when a
proxied host's file ownership needs to match the target account rather than
the account otto authenticated as.

### Breaking change: `creds` was a dict, now a list

`creds` used to be a flat `{"login": "password"}` mapping; it is now the
ordered list described above (`feat(host)!`). A `lab.json` still written in
the old dict shape is rejected loudly at load:

```text
ValueError: creds is now a list of cred objects: [{"login": "user", "password": "pw"}, ...]
(was: {user: password}). See the host-database guide.
```

Update every entry to `[{"login": ..., "password": ...}, ...]`. The first
entry keeps the old "first dict entry is the default login" behavior — now
explicit and ordered, rather than relying on dict insertion order.

## Selecting a different source

`[lab] backend` selects any **registered** backend by name. Register your
backend from an `init` module (one of the modules listed in `init = [...]`),
then name it in settings:

```python
# my_lab_source.py  (listed in init = [...])
from otto.labs import register_lab_repository
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
[`LabRepositoryError`](../../api/labs.rst), listing the registered names.

```{note}
This is the same named-registry mechanism otto uses everywhere else
(`register_term_backend`, `register_reservation_backend`, `register_host_class`).
An `init` module always imports before the lab is loaded, so the name is
registered by the time settings select it.
```

See {doc}`Extension points <../../architecture/subsystems/extension-points>` for the
registry machinery behind this and every other seam otto can be extended at.

## Writing a custom backend

A backend is any class satisfying the two-method protocol. Otto ships a small,
dependency-free reference implementation —
[`otto.examples.lab_repository.ExampleLabRepository`](../../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to host dicts and builds real hosts with
[`create_host_from_dict`](../../api/host/factory.rst) so each becomes a `RemoteHost`
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
>>> from otto.labs import LabNotFoundError
>>> try:
...     repo.load_lab("does-not-exist")
... except LabNotFoundError:
...     print("not found")
not found
```

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
  `list_labs()`. Labs are combined with `+`, not `,` — `--lab a,b` asks for one
  lab literally named `a,b`.
