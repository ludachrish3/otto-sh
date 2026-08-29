# Host sources

Otto builds its lab — the set of hosts a command can touch — from an ordered
list of **host sources**. Each repo declares its own list as `[[lab.sources]]`
entries in `.otto/settings.toml`: the built-in `json` backend reading
`lab.json` files, a CMDB or inventory API you plug in by implementing one
small interface, or several of them at once. Otto reads every declared source
and combines them, later sources overriding earlier ones one **element** (or
one `labs` table entry) at a time.

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

Sources are not the host **inventory**, and the two never overlap. Sources
compose *records*: several of them contribute elements, and a later one
replaces an earlier one wholesale, one element (or one `labs` table entry) at a
time. The inventory supplies *fields* **within** a record — the machine facts
of a host entry that references it — and there is exactly one per process, so
no precedence question arises there at all. A lab file can come from any
source and still reference the inventory; see {doc}`inventory`.

### json sources: `paths` are directories, files, or globs

For `backend = "json"`, `paths` is required and non-empty. Each entry is one
of three forms:

- a **directory**, searched for a `lab.json` inside it;
- a path ending in **`.json`**, read directly as the lab file; or
- a **glob** — any entry containing `*`, `?` or `[` — expanded relative to its
  non-glob prefix, contributing the `.json` files it matches in sorted order.

Entries resolve like every other settings path: `~` expands, a relative path
anchors to the **repo root** (never the directory you ran `otto` from), and an
absolute path is used as written — so a file shared outside the repo is simply
an absolute entry:

```toml
[[lab.sources]]
backend = "json"
paths = ["lab_data", "/srv/lab/global-hosts.json"]
```

An entry that resolves to no existing file is skipped rather than failing —
an absent `lab.json`, or a glob that matches nothing — so an optional-by-design
location costs nothing. `paths` is the *only* key the json backend accepts;
anything else is a typo and fails loud at startup.

(one-source-several-files)=

#### One source, several files

A source is not one file. Every file its `paths` names is a complete lab
document that may carry **any subset** of the three `lab.json` sections, and a
source composes all of its files by **union**: the `labs` tables merge, the
`elements` arrays concatenate, and so do the `links`. An element in one file
joins a lab declared in another, so the declarations can live apart from the
equipment:

```toml
[[lab.sources]]
backend = "json"
paths = ["lab_data/labs.json", "lab_data/elements/*.json"]
```

```text
lab_data/
├── labs.json          # the labs table: every lab, its resources, its metadata
└── elements/
    ├── rack-b4.json   # elements only
    └── bench1.json    # elements only
```

Within **one** source a duplicate is a typo, never an override: the same lab
declared by two of the source's files, or the same element `(name, id)`
carried by two of them, fails the load naming both files. Overriding is the
opt-in of a *second* `[[lab.sources]]` entry — see [Ordering and
overrides](#ordering-and-overrides) below.

The `lab.json` schema itself — the `labs` table, the element entry, every host
field, and the link entry — lives in {doc}`lab-config`.

```{tip}
Running `otto init` (or `otto init --lab`) scaffolds a `lab.json` with one
declared lab and one example element, plus a `lab_data/README.md` walking
through the three sections — a faster way to see a valid file than building
one from scratch. See {doc}`../../getting-started`.
```

### Annotating entries with `_`-prefixed keys

`lab.json` is plain JSON, which has no comment syntax. Any key beginning
with `_` (e.g. `_comment`) is stripped before validation, so it is otto's
sanctioned way to leave a note inline without tripping the schema's
`extra="forbid"` check. It works at **every** level of the file — the
document, the `labs` table, a `labs` entry, an element, a host entry, a link —
so a note can sit next to whatever it explains:

```json
{
    "$schema": "../.otto/schemas/lab.schema.json",
    "_comment": "Bench 1's own equipment; racked devices live in the global source.",
    "labs": {
        "example_lab": { "resources": ["example-device"] }
    },
    "elements": [
        {
            "_comment": "Replace before connecting to a real device.",
            "name": "example-device",
            "labs": ["example_lab"],
            "hosts": [
                {
                    "ip": "192.0.2.1",
                    "os_type": "unix",
                    "valid_terms": ["ssh"],
                    "creds": [{ "login": "admin", "password": "CHANGE_ME" }]
                }
            ]
        }
    ],
    "links": []
}
```

A top-level `$schema` is comment space too — that is the key wiring the file
to the generated schema in your editor (see {doc}`../cli/schema/editors`).
The `_` idiom is scoped to `lab.json`; it is not a general convention
elsewhere in otto's JSON/TOML configuration.

## Combining sources

Sources are consulted **in declaration order**, and every declared source is
live — otto never drops one because another already answered.

### Ordering and overrides

The unit of override is the **element**, keyed by its `(name, id)` — not the
host. When two sources carry the same element in the same lab, the **later**
source's element replaces the earlier one *wholesale* — its hosts, its
`metadata`, and the membership it states — and otto logs a warning naming both
labels:

```text
element ('alt1', None) in lab 'site': my_project/virtual overrides my_project/global
```

That warning is the whole transparency story: an override is a deliberate act,
and otto says so, in ordinary command output, every time one takes effect. An
override that repoints a host at a different `ip` is allowed — pointing the
lab at a re-imaged VM is exactly the use this serves.

Because replacement is wholesale, overriding one board of a four-board chassis
means restating the whole element. In exchange, a hybrid element — this
source's hosts with that source's metadata — cannot exist.

Replacement happens per lab load, so it covers exactly the labs *both*
elements match. An override that **drops** a membership pattern therefore does
not take the element out of a lab the earlier source's element still matches —
that lab keeps loading the earlier element; to remove an element from a lab,
change it at the source that declares it.

A `labs` table entry overrides the same way. A later source that *declares*
the lab replaces the earlier declaration's `resources` and `metadata` together
— never half of one — with its own warning:

```text
labs entry 'site': my_project/virtual overrides my_project/global
```

A source that holds elements for a lab but declares no `labs` entry for it
contributes members only, and leaves the earlier declaration standing.

Inside a *single* source a duplicate is still an error, not an override: there
it is a typo (see {ref}`one-source-several-files` above). Splitting the
records across two `[[lab.sources]]` entries is the opt-in to override
semantics.

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
paths = ["lab"]              # json-specific; directories, .json files, or globs
```

The same layering is how you **try a data change before it lands in the global
database**: paste the element into the repo's json source with your edit
applied, run against it, and the override warning tells you — and anyone
reading the log afterwards — that a local element beat the global one. Because
the override is whole-element, paste the *whole* element — its `labs`
patterns and every one of its host entries: what you leave out is dropped, not
inherited from the source below.

### Multiple repos

With several repos on `OTTO_SUT_DIRS`, the process-wide list is every repo's
list concatenated in `OTTO_SUT_DIRS` order, and all of it is live. A later
repo's source overrides an earlier repo's on a colliding element, with the
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

`"element ('X', None) in lab 'Y': A overrides B"` / `"labs entry 'Y': A overrides B"`
: Sources `A` and `B` both carry element `X` (or both declare lab `Y`), and
  `A` — the later entry — won it whole. Expected when you are deliberately
  layering a repo source over a global one. A *surprise* means two sources own
  the same element: drop it from one of them, or reorder the entries so the one
  you want wins last.

`LabRepositoryError: host id 'X' in lab 'Y': element ... collides with element ...`
: Two *different* elements — surviving the merge, so not an override — produce
  the same host id. The message names both elements and the source each came
  from; give one a distinct `name`, an element `id`, or a `board`/`slot`.

`LabNotFoundError: Lab '...' is not declared by any configured source (...)`
: No source's `labs` table (or backend `list_labs`) declares that name. A lab
  exists only once it is declared — elements matching it by pattern are not
  enough, and the message says so explicitly when that is the case. Add the
  `labs` entry, or check `--lab` / `OTTO_LAB` against `otto --list-labs`. Labs
  are combined with `+`, not `,` — `--lab a,b` asks for one lab literally
  named `a,b`.

`LabRepositoryError: Lab '...' is declared by ... but no element in any source matches it`
: The declaration is there and nothing joins it. Add a `labs` pattern to an
  element, or drop the declaration — an empty lab is refused rather than
  loaded.

`LabNotFoundError: Lab '...' not found in any configured source: ...`
: A source declares the name but none could actually build it — the message
  lists every source label consulted. On a custom backend this means
  `list_labs()` and `load_lab()` disagree; check the name against
  `otto --list-labs`.

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
