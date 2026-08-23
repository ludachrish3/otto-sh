# Host sources

Otto builds its lab — the set of hosts a command can touch — from an ordered
list of **host sources**. Each repo declares its own list as `[[lab.sources]]`
entries in `.otto/settings.toml`: the built-in `json` backend reading
`lab.json` files, a CMDB or inventory API you plug in by implementing one
small interface, or several of them at once. Otto reads every declared source
and combines them, later sources overriding earlier ones per host record.

```{note}
Choosing a host source is a one-time, team-level decision — part of setting otto
up for your team. See the {ref}`team-setup-checklist` in {doc}`settings`.
```

Otto is strictly a consumer of host data. It reads hosts; it never writes back
to your source of record.

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
host 'alt1' in lab 'site': my_project/virtual overrides my_project/global
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
| `proxy` | string | Name of a registered login proxy (see {doc}`../../library/extending-backends`) that drives the steps to *become* this login, after authenticating as `via`. Omit for a directly-loginable account — a proxy-less entry still uses the built-in `"su"` proxy when `switch_user`/`as_user` switches to it. |
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
(was: {user: password}). See the host sources guide.
```

Update every entry to `[{"login": ..., "password": ...}, ...]`. The first
entry keeps the old "first dict entry is the default login" behavior — now
explicit and ordered, rather than relying on dict insertion order.

## Custom backends as sources

Any **registered** backend can be named by a source entry. Register yours from
an `init` module (one of the modules listed in `init = [...]`), then select it —
the `LabRepository` protocol the class must satisfy is in
{doc}`../../library/lab-source-backends`:

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
