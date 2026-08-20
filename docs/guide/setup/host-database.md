# Host Database

Otto builds its lab — the set of hosts a command can touch — from an ordered
list of **host sources**. Each repo declares its own list as `[[lab.sources]]`
entries in `.otto/settings.toml`: the built-in `json` backend reading
`lab.json` files, a CMDB or inventory API you plug in by implementing one
small interface, or several of them at once. Otto reads every declared source
and combines them, later sources overriding earlier ones per host record.

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

```{important}
Return a **fresh `Lab`** from every `load_lab` call. When more than one source
is configured, otto merges the sources' labs **in place** — so a backend that
caches one `Lab` object and hands it back from every call would eventually
return a lab an earlier merge has already mutated.
```

### One optional capability

`list_host_summaries() -> list[HostSummary]`
: Enumerate hosts *without building them*, for tab completion and tunnel
  path-narrowing. Implementing
  [`SupportsHostSummaries`](../../api/labs.rst) is purely an optimization —
  otto detects it structurally, and a backend that omits it still gets
  completion, because otto falls back to `list_labs()` + `load_lab()`.

  If you do implement it, a summary must agree with the host `load_lab()`
  builds — in three ways, all checked by `assert_lab_repository_conforms`:

  - **Every id you return must be one `load_lab()` produces**, or completion
    offers names that cannot dispatch. Derive ids with
    [`host_identity`](../../api/host/factory.rst) rather than formatting your
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

  Otto also bounds how long it will wait for your backend during completion
  (2 seconds by default). If yours is legitimately slower, raise
  `OTTO_COMPLETION_HOST_TIMEOUT`; otto logs a warning naming it rather than
  hanging the user's shell.

## Declaring sources

Sources are an ordered array of tables. One entry naming the built-in `json`
backend is the everyday setup — and what `otto init` scaffolds:

```toml
name = "my_project"
version = "1.0.0"

[[lab.sources]]
backend = "json"
paths = ["lab_data"]
```

Every entry takes two otto-owned keys:

`backend`
: **Required.** The name of a registered backend. `"json"` ships with otto;
  any other name must be registered by an `init` module — see [Custom backends
  as sources](#custom-backends-as-sources).

`name`
: Optional label for the source. The label otto prints in warnings and errors
  is always `<repo-name>/<name>`, repo-qualified so two repos may each call a
  source `"global"`. Omit it and the label defaults to `<backend>#<ordinal>`
  (1-based position in *that repo's* list), e.g. `my_project/json#1`. Two
  entries in one repo may not share an explicit `name`.

Every remaining key belongs to the backend the entry selected.

A repo with no `[lab]` table declares no sources — normal for a repo that
ships only libs and tests. The table itself may hold nothing but `sources`.

### json sources: `paths` are directories or files

For `backend = "json"`, `paths` is required and non-empty. Each entry is
either:

- a **directory**, searched for a `lab.json` inside it, or
- a path ending in **`.json`**, read directly as the lab file.

Entries resolve like every other settings path: `~` expands, a relative path
anchors to the **repo root** (never the directory you ran `otto` from), and an
absolute path is used as written — so a file shared outside the repo is simply
an absolute entry:

```toml
[[lab.sources]]
backend = "json"
paths = ["lab_data", "/srv/lab/global-hosts.json"]
```

A path that does not resolve to an existing file is skipped rather than
failing, so an optional-by-design location costs nothing. `paths` is the
*only* key the json backend accepts; anything else is a typo and fails loud at
startup.

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

## Combining sources

Sources are consulted **in declaration order**, and every declared source is
live — otto never drops one because another already answered.

### Ordering and overrides

When two sources define the same host id in the same lab, the **later**
source's record replaces the earlier one *wholesale* — there is no field-level
merge — and otto logs a warning naming both labels:

```text
host 'orange' in lab 'site': my_project/virtual overrides my_project/global
```

That warning is the whole transparency story: an override is a deliberate act,
and otto says so, in ordinary command output, every time one takes effect. An
override that repoints the host at a different `ip` is allowed — pointing the
lab at a re-imaged VM is exactly the use this serves.

Inside a *single* source a duplicate host id is still an error, not an
override: there it is a typo. Splitting the records across two
`[[lab.sources]]` entries is the opt-in to override semantics.

Overriding is per-lab and has nothing to do with combining labs: `--lab a+b`
still merges two *different* labs by otto's cross-lab rules (see
{doc}`lab-config`), after each lab has been assembled from the sources.

### Layering a repo over a global source

This is the pattern the source list exists for. Physical devices are global
truth — every team must be served the same records, from a database or a
globally shared file. The VMs and QEMU guests a project deploys, re-images and
reconfigures are the project's own, and belong in its repo. Declare the global
source first and the repo-owned one after it:

```toml
[[lab.sources]]
name = "global"              # optional label, used in warnings and errors
backend = "cmdb"             # any name registered via register_lab_repository
server = "cmdb.example.com"  # remaining keys = constructor kwargs for that backend

[[lab.sources]]
name = "virtual"
backend = "json"
paths = ["lab"]              # json-specific; entries are directories or .json files
```

The same layering is how you **try a data change before it lands in the global
database**: paste the record into the repo's json source with your edit
applied, run against it, and the override warning tells you — and anyone
reading the log afterwards — that a local record beat the global one. Because
the override is whole-record, paste the *whole* record: fields you leave out
are dropped, not inherited from the source below.

### Multiple repos

With several repos on `OTTO_SUT_DIRS`, the process-wide list is every repo's
list concatenated in `OTTO_SUT_DIRS` order, and all of it is live. A later
repo's source overrides an earlier repo's on a colliding host id, with the
same warning — the labels are repo-qualified precisely so that reads
unambiguously.

If *no* repo declares a source, otto still starts and lab-free commands still
work; the failure arrives where a lab is actually demanded:

```text
LabNotFoundError: Lab 'site' cannot be loaded: no repo declares a [[lab.sources]] entry in .otto/settings.toml
```

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

## Custom backends as sources

Any **registered** backend can be named by a source entry. Register yours from
an `init` module (one of the modules listed in `init = [...]`), then select it:

```python
# my_lab_source.py  (listed in init = [...])
from otto.labs import register_lab_repository
from my_company.cmdb import CmdbLabRepository

register_lab_repository("cmdb", CmdbLabRepository)
```

```toml
[[lab.sources]]
name = "global"
backend = "cmdb"
url = "https://cmdb.example.com"
```

Otto constructs that entry as
`CmdbLabRepository(repo_dir=<repo root>, url="https://cmdb.example.com")` —
every key other than `backend` and `name` becomes a keyword argument, plus
`repo_dir` so the backend can resolve relative paths of its own. Otto does not
interpret those kwargs; validate them in your constructor and fail loud there.
Two entries may name the same backend with different kwargs (two databases,
two files) — each is constructed separately. Naming an unregistered backend
raises [`LabRepositoryError`](../../api/labs.rst), listing the registered
names.

```{note}
This is the same named-registry mechanism otto uses everywhere else
(`register_term_backend`, `register_reservation_backend`, `register_host_class`).
An `init` module always imports before the lab is loaded, so the name is
registered by the time settings select it.
```

See {doc}`Extension points <../../architecture/subsystems/extension-points>` for the
registry machinery behind this and every other seam otto can be extended at.

## Writing a custom backend

A backend is any class satisfying the two required methods (plus, optionally,
`list_host_summaries`). Otto ships a small,
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
: A `[[lab.sources]]` entry's `backend` names a backend that was never
  registered — raised per entry, as otto constructs that source. Check the
  name against the registered list the message prints, and confirm the `init`
  module that calls `register_lab_repository(...)` is listed in `init = [...]`.

`"host 'X' in lab 'Y': A overrides B"`
: Sources `A` and `B` both define host `X` in lab `Y`, and `A` — the later
  entry — won the whole record. Expected when you are deliberately layering a
  repo source over a global one. A *surprise* means two sources own the same
  record: drop it from one of them, or reorder the entries so the one you want
  wins last.

`LabNotFoundError: Lab '...' not found in any configured source: ...`
: No source knows that lab; the message lists every source label consulted, so
  a missing label there means the source you expected never got declared.
  Check `--lab` / `OTTO_LAB` against `otto --list-labs`. Labs are combined with
  `+`, not `,` — `--lab a,b` asks for one lab literally named `a,b`.

`LabNotFoundError: Lab '...' cannot be loaded: no repo declares a [[lab.sources]] entry`
: No repo on `OTTO_SUT_DIRS` declares any source at all. Add an entry (`otto
  init` scaffolds one) — and if you expected a repo to provide it, check
  startup output: a repo whose `settings.toml` fails to parse is skipped with
  a framed error, and only the commands that need it fail.

`ValueError: ... was removed` at startup
: The settings file still uses a pre-sources spelling. Top-level
  `labs = [...]`, `[lab] backend` and the long-dead `lab_data_type` key are
  parse errors that name their replacement; a leftover `[lab.<backend>]` kwarg
  sub-table is refused too, since the `[lab]` table takes nothing but
  `sources`. The repo is skipped with a framed startup error, so the commands
  that need its hosts fail loud. [Declaring sources](#declaring-sources) above
  has the full shape.
