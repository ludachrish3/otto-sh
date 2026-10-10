# Use-cases

A **use-case** is a named, cross-repo deployment: "bring up `integration`" is
one command whatever combination of projects is currently active. `otto docker
compose up`, `compose down` and `compose build` all speak use-cases, and so
does the library API that instructions and tests import.

The unit a repo declares is not the whole use-case — it is a **fragment** of
one. Every active repo contributes the fragments it declares under the name,
otto decides which fragments take part, assembles one env mapping, and runs
**one** `docker compose up` on the parent over the merged file set. Which lab
host the parent is, is {ref}`Which host <docker-which-host>`.

```toml
# repo-a/.otto/settings.toml
[[docker.images]]
name = "repo-a-api"                   # the image name, verbatim; only for an
                                      # image otto builds
dockerfile = "docker/Dockerfile"
context = "docker"

[[docker.composes]]
name = "core"                         # a handle for this file
path = "docker/compose.yml"
services = ["api"]                    # the names in its services: block

[[docker.use_cases]]
name = "integration"                  # the use-case this fragment joins
composes = ["core"]                   # handles from above
```

`otto docker compose up --build integration` builds the declared image and deploys
it (without `--build`, `up` builds nothing, so an image that is not already on the
host is docker's pull error), and the container comes back as the lab host
`<parent>.integration.api` — see [Container hosts](index.md#container-hosts).

Only `name` and `composes` are required. A fragment never names a host: the
stack lands on one parent, chosen on the command line or by the lab
({ref}`Which host <docker-which-host>`).

{doc}`../../configuration/settings` is the schema reference for every key
above; this page is about what they *mean*.

## Seeing what is declared before deploying anything

`otto docker use-cases` is the inventory view. It contacts nothing, starts
nothing, and creates no output directory, so it prints the same under
`--dry-run` (see {doc}`../dry-run`). Like every `otto docker` verb it
needs a lab selected (`--lab` or `OTTO_LAB`); without one it exits 2 before
listing anything. The host column is the parent the rule picks in that lab
({ref}`Which host <docker-which-host>`):

```console
$ otto --lab unix docker use-cases integration
                          use-case integration
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ fragment         ┃ provides         ┃ host  ┃ env keys  ┃ status    ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ repo1[core,edge] │ edge (priority   │ test3 │ EDGE_ADDR │           │
│                  │ 10)              │       │           │           │
│ repo2[core]      │ -                │ test3 │ -         │           │
│ repo2[mock-edge] │ edge (priority   │ -     │ -         │ displaced │
│                  │ 0)               │       │           │           │
└──────────────────┴──────────────────┴───────┴───────────┴───────────┘
docker: edge goes to repo1 (priority 10); repo2 (priority 0) stands down
```

A `<repo>[<handles>]` cell names one fragment: the repo that declared it and
the compose handles it contributes. Omit the argument to list every declared
use-case.

Env **key names** are listed, never values — a value can be a secret pulled
from your shell.

The verb reports rather than raises: a lab whose parent the rule cannot pick
prints its refusal in place of the host on every use-case (the refusal is
lab-wide), and the listing still exits 0.

## How templating works — the two-sided mechanism

Otto never templates a product's compose file. It has no template syntax over
one, injects no variable of its own into one, and the compose file contains no
mention of otto. What otto does is **assemble the environment** the compose
run sees. Everything else is compose's own, long-standing interpolation.

Two files, two languages, one boundary between them.

### The product side: plain compose, no otto anywhere

```yaml
# repo-a/docker/compose.yml — a deliverable product artifact
services:
  api:
    image: repo-a-api:latest
    environment:
      - EDGE_ADDR                      # pass-through from the compose env
      - LOG_LEVEL=${LOG_LEVEL:-info}   # native interpolation, native default
    extra_hosts:
      - "edge:${EDGE_ADDR}"            # interpolation into any value slot
```

`${EDGE_ADDR}` and `${LOG_LEVEL:-info}` are **docker compose** syntax, read by
docker compose. `EDGE_ADDR` is a name the product chose for its own contract.
Nothing here knows otto exists.

### The otto side: fact references, confined to `settings.toml`

```toml
# repo-a/.otto/settings.toml — the only file that speaks otto
[[docker.use_cases]]
name = "integration"
composes = ["core"]
env = { EDGE_ADDR = "${otto:parent.addr}", LOG_LEVEL = "debug" }
```

`${otto:parent.addr}` is a **fact reference**. Otto resolves it at deploy
time — "the address of the parent this stack is being deployed to" — and the
resolved value is what enters the env mapping under the product's own name,
`EDGE_ADDR`.

The two halves meet at exactly one point: the *name* `EDGE_ADDR`. The product
declares which variables it consumes; the settings file says where each
value comes from.

:::{important}
Fact-reference syntax is valid **only inside `settings.toml`** — otto's own
file. It is not a templating language for compose files, Dockerfiles, or
anything else the product ships. A `${otto:...}` string written into a compose
file is meaningless to otto and gets no substitution.

And no `OTTO_*` variable is ever injected. A variable reaches the compose
environment only because a channel below explicitly mapped it there.
:::

### Running the compose file without otto

A product's compose file also runs by hand, with no otto involved. From the
repo root, with the image already built:

```console
$ EDGE_ADDR=10.0.0.5 LOG_LEVEL=debug docker compose -f docker/compose.yml up -d
```

(`-f` because the file above lives at `docker/compose.yml`; from inside that
directory a bare `docker compose up -d` is the same command.)

Run by hand, with the values supplied yourself, that behaves **identically**
to otto's deployment of the same stack. `${otto:...}` resolves entirely on
otto's side of the boundary; the compose file only ever sees resolved values,
under the names the product chose. So:

- **Every compose-native env feature keeps working**, because otto parses none
  of them: `${VAR:-default}`, `${VAR?message}` refusals, a product-shipped
  `.env` file, `env_file:` keys. Otto contributes values; compose does the
  interpolating.
- **The product stays deployable with no otto anywhere.** Hand the compose
  file to someone with no otto installed and it runs.

### The fact-reference namespace

| Reference | Resolves to |
| --------- | ----------- |
| `${otto:use_case}` | The use-case name being deployed |
| `${otto:compose_project}` | The compose project name ({ref}`below <docker-use-case-naming>`) |
| `${otto:host.<id>.addr}` | Address of a named **unix** lab host in scope — including one that runs no containers, such as a DUT |
| `${otto:parent.addr}` | Address of the parent this stack is being deployed to |
| `${otto:parent.id}` | The parent's lab id |

"In scope" is each participating repo's project scope (see {doc}`../projects`),
unioned across every repo taking part in the deployment. It is *not* narrowed to
docker-capable hosts, so a container can be told the address of the bench
device it is supposed to drive. Two limits apply — the namespace
covers **unix** lab hosts only, so a serial-attached or Zephyr target is not
addressable this way, and a host with no configured address is refused rather
than fabricated.

An unknown reference is a configuration refusal naming the known forms and the
hosts actually available — nothing is staged and nothing is started.
Anything not matching `${otto:` is passed through untouched, so a product
`${VAR}` string is safe to use as a literal value.

## Where values come from: the env channels

The templating above is channel 1. There are three, merged in order, each
later one winning:

1. **The fragment's `env` table** — literal values, plus the `${otto:...}`
   references above. `pass_env = ["EDGE_TAG"]` then copies named variables
   from your invoking shell (an explicit allowlist; a variable that is absent
   is simply left unset and reported). `pass_env` is applied after every
   fragment's static table, so a name in both wins from the shell.
2. **The repo adapter** — code, for values only code can compute
   ([below](#the-repo-adapter)).
3. **The caller** — `--env K=V` and `--env-file PATH` on the CLI, `env=` and
   `env_files=` on the library verbs. Wins over everything (`--env` over
   `--env-file`).

### When two fragments set the same key

Channel 1 is assembled from *every* participating fragment, in selection
order, and a later fragment's value silently replaces an earlier one's. There
is no refusal and no warning.

The practical consequence: a variable two repos both care about is not a
coordination mechanism. If the value matters, name it something only one
fragment sets, or pin it from the caller with `--env`, which beats every
fragment. `otto docker use-cases` lists each fragment's env KEY names, so an
overlap is visible before you deploy.

The final mapping is fed to **both** sinks: a staged env file passed as
`docker compose --env-file`, and the remote process environment of the compose
invocation itself (`env K=V ... docker compose ...`), so the deployment
behaves exactly as if you had exported the mapping and run compose by hand,
whatever your compose version.

## Provider competition: swapping a mock for the real thing

The examples from here on are a different deployment from the templating
walkthrough above — `repo1` and `repo2`, rather than the walkthrough's
`repo-a`. Read each half on its own.

Two projects can offer the same thing. A repo that owns the real edge service
and a repo that ships a mock of it both want to supply `edge` — and they must
never both run.

A fragment may declare `provides` (a capability name) and `priority`:

```toml
# repo1 — the real edge
[[docker.use_cases]]
name = "integration"
composes = ["core", "edge"]
provides = "edge"
priority = 10

# repo2 — the mock
[[docker.use_cases]]
name = "integration"
composes = ["mock-edge"]
provides = "edge"
priority = 0
```

The rules:

- A fragment **without** `provides` always takes part.
- Fragments sharing a `provides` capability compete; the highest `priority`
  wins and **every loser is excluded whole**.
- An exact tie is a hard error naming both fragments. Break it for one
  invocation with `--provide edge=repo1` (repeatable), or fix the priorities.
  A tie between two fragments of the *same* repo is always a config error —
  there is no knob for it.

The convention (documented, not enforced): real infrastructure declares a
positive priority, mocks omit it (`0`), and a higher-fidelity mock may rank
above a lower one.

Which fragments are even present to compete is decided by **project
activation** — a lab's `lab_patterns` and `-I`/`-E` (see
{doc}`../projects`). Precedence is declared once, at the provider; each
lab/project combination merely changes who shows up.

### Losing is whole-fragment — a worked example

Repo1's fragment above contributes *two* compose handles, `core` and `edge`,
and it is the one carrying `provides = "edge"`. Deployed normally, repo1 wins
and both of its files are in the merged stack:

```console
$ otto --lab unix --dry-run docker compose up integration
… Resolved plan: test3 <- repo1[core,edge], repo2[core].
  Displaced: edge goes to repo1 (priority 10); repo2 (priority 0) stands down. …
```

Now hand the capability to the mock:

```console
$ otto --lab unix --dry-run docker compose up integration --provide edge=repo2
… Resolved plan: test3 <- repo2[core], repo2[mock-edge].
  Displaced: edge goes to repo2 (priority 0); repo1 (priority 10) stands down. …
```

Repo1's `core` file — and the `api` service in it — is **gone**, not just its
`edge` file: a fragment is the atomic unit of participation, and losing means
the fragment stands down entirely. If repo1's `api` must survive a mock swap,
it belongs in a *separate* fragment that declares no `provides`, exactly as
repo2 splits its own `core` from its `mock-edge`.

`--provide` narrows the field to one repo *before* ranking, so the winner can
carry a lower priority than the fragment it displaced — as it does above. The
displacement line names who won, at what priority, and who stood down.

## Where the stack lands

Every fragment of a use-case lands on the one parent, so the deployment is one
merged stack on one host. Which host that is — `--parent`, or the lab's
`docker_priority` — is {ref}`Which host <docker-which-host>`. Services that
must run on different hosts belong in different use-cases, each brought up with
its own `--parent`; addressing between them flows through env values
(`env`, `pass_env`, `--env`) on the second use-case, not through otto.

(container-users)=
## Container users

A compose fragment may declare a default access user per service — see
[Docker images and compose stacks](../../configuration/settings.md#docker-images-and-compose-stacks)
for the `users = { db = "postgres" }` field itself.

That declared default is the middle of three precedence layers: a per-call
`--user`/`user=` on `login`, `run`, `exec` or `put` beats the declared
default, which beats the image's own `USER`. `get` takes no part in this
ladder — see below.

`run` (with `send`/`expect`) shares the container's **persistent channel**,
which binds its user the first time it opens — a later `run()` naming a
*different* user refuses rather than silently switching identity
mid-session; `close()` or `rebuild_connections()` tears the channel down so
the next call rebinds. `login` is not part of that channel at all: each call
opens its own fresh `docker exec -it` over the parent connection, so it is
never subject to the bind refusal — a `login --user postgres` succeeds even
while the run channel is bound to `root`. See {meth}`~otto.host.host.BaseHost.run`
and {doc}`../host/login`.

`put` chowns the landed files to the effective user — the per-call value, or
the declared default when the call names none — as root, in one batched
`chown` after `docker cp` places the files and before `--mode` is applied.
Because it is one command over every landed file, a failure is not scoped to
a single file: every still-successful entry flips to an error, naming the
file, the user, and the reason. On containers, `get` accepts `--user`/`user=`
and ignores it — reads are ownership-indifferent, so there is nothing to
chown.

All of that is the *container* mechanism. A unix host reaches the same end by
a different route — it authenticates as the user rather than chowning after
the fact — so `put`, `get` and (on an `ssh`-term host) `exec` take `user=`
there too, while `run` stays with `as_user`. See {doc}`../host/put` for both
families.

A container's actual **runtime** user — the identity its own process runs
as — is a different concern from all of the above, and stays where compose
already owns it: the product's own compose file, lab-varied through the
ordinary env channels this page describes.

```yaml
services:
  api:
    user: ${SERVICE_USER:-1000:1000}   # runtime user — the compose file's concern, lab-varied via env
```

## The repo adapter

For values only code can compute, or for compose files that must be *rendered*
rather than shipped verbatim, a repo registers an adapter from its init
module:

```python
from otto.docker import AdapterResult, register_compose_adapter


@register_compose_adapter("integration")
def render(facts):
    rendered = my_product.deploy.render(  # product code: zero otto imports
        template_dir=...,
        edge_addr=facts["parent"]["addr"],
    )
    return AdapterResult(files={"core": rendered}, env={"WORKER_TAG": "1.4"})
```

The registration line is the only otto touchpoint; everything beneath it is
the product's own templating, with its own syntax, owned by the product.

`facts` is plain JSON-able data — `use_case`, `compose_project`, `parent`,
`hosts`, `files` (the repo's winning compose files by handle), and
`scratch_dir`, a private temp dir the adapter may write to.
{class}`~otto.docker.adapter.AdapterResult` returns `files` (compose handle ->
replacement text; omitted handles ship verbatim), `extra_files` (extra files
staged beside the compose files, for `env_file:`-style references), and `env`,
which merges as channel 2.

Adapters must be **pure with respect to devices** — no host access. They run
under `--dry-run` too, so the full plan is printable. One adapter per (repo,
use-case): a second registration for the same use-case raises
{class}`~otto.registry.DuplicateRegistration`, unless it passes
`overwrite=True` (`@register_compose_adapter("integration", overwrite=True)`),
which replaces the repo's adapter deliberately. Registering outside an init
module's import raises {class}`~otto.registry.RegistrationRefused`, since that
import is what names the repo. Both are decided when the decorator is
applied, not when `register_compose_adapter(...)` is called: the adapter
belongs to the repo whose init import applies it, and applying it outside an
init import is refused.

See {mod}`otto.docker.adapter` for the API.

## Deploying, narrowing, and tearing down

```console
$ otto docker compose up --build integration        # build the declared images, then every service, on the parent
$ otto docker compose up integration api db          # just these services (images already built)
$ otto docker compose down integration api           # stop and remove just api
$ otto docker compose down integration               # the whole deployment
```

Trailing service names are allowed only after an explicit use-case name, so
the positionals stay unambiguous. `up` composes just the named services
(compose may also start their `depends_on` dependencies); `down` stops and
removes just their containers, leaving the rest of the stack and its network
standing. Registration and unregistration scope to the named services too.

With no use-case named at all, `up` and `down` pick the only declared one.
Zero or several is a hard error listing them — never a quiet no-op.
`compose build` defaults the same way; bare {doc}`build` takes no use-case at
all: it builds images on one parent.

### `up` is convergent

`docker compose up -d` creates what is missing, leaves unchanged services
running, and recreates only services whose config or image changed. Otto does
not short-circuit that: re-running a broader deployment **adds** the newly
active projects' services to a live stack rather than looking up what is
already there.

`--remove-orphans` rides along on every `up`. When a newly active real
provider displaces a mock, the mock's still-running container is an orphan of
the merged file set — and the same `up` removes it. The swap is one command,
not a teardown plus a deploy.

### Dry run

`otto --dry-run docker compose up <usecase>` prints the resolved plan and declines at
the first device touch — see {doc}`../dry-run` for the contract. Because
selection, env assembly and the adapters are all pure, the preview
includes the **exact** compose command on the parent, not a description of one:

```console
$ otto --lab unix --dry-run docker compose up integration
'deploy(integration)' was not run on host 'test3': this is a dry run, which
contacts no device. … Resolved plan: test3 <- repo1[core,edge], repo2[core].
Displaced: edge goes to repo1 (priority 10); repo2 (priority 0) stands down.
Fragment env keys: ['EDGE_ADDR']. No image was built, no file was staged and
no container was started. The adapters ran (plain data; the only thing one may
write is its own scratch dir), so this is the command itself, not a
description of it. On test3, would run: env EDGE_ADDR=10.10.200.13 docker
compose -p unix-integration-vagrant -f …/core.yml -f …/edge.yml -f …/core.yml
--env-file …/otto.env up -d --remove-orphans
```

`otto docker use-cases` answers a different question — what is *declared* —
and this answers what *would happen*.

(docker-use-case-naming)=
## Naming

- **Compose project:** `<lab>-<usecase>-<suffix>`. The suffix is your username
  by default, or `OTTO_COMPOSE_SUFFIX`, so concurrent users on one docker host
  never collide. There is no `otto-` prefix. Design notes on each segment:
  {doc}`../../architecture/subsystems/docker-hosts`.
- **Container host ids:** `<parent>.<usecase>.<service>`, as
  [Container hosts](index.md#container-hosts) describes. A repo migrating from
  a composes-only declaration keeps its container ids literally unchanged by
  naming its use-case after the repo.

## From instructions and tests

The CLI is a thin wrapper. The same deployment from Python:

```python
from otto.docker import deployed


async with deployed("integration", own=True) as stack:
    await stack.hosts["api"].run("./run-tests")
```

{func}`~otto.docker.deployment.deployed` is the recommended scope — see
{doc}`../../cookbook/test-recipes` for the sharing contract and
{mod}`otto.docker.deployment` for `deploy`, `teardown` and
{class}`~otto.docker.deployment.UseCaseStack`.

## Errors

Every resolution failure — unknown use-case, empty selection, a provider tie,
an unknown `${otto:...}` reference, an unknown `--provide` target, a service
name nothing declares — is a **configuration refusal**: it names the
candidates and the knobs, nothing is touched, and the CLI exits 1. An empty
selection is never a silent exit 0. A parent the rule cannot choose is refused
too, at exit 2 ({ref}`Which host <docker-which-host>`): a tie names the tied
hosts, a lab with no docker-capable host says so.

Failures *after* the first device touch are different: whatever the failed
call brought up is torn down again before the error propagates.
