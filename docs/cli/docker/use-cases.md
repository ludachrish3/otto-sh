# Use-cases

A **use-case** is a named, cross-project deployment: "bring up `integration`" is
one command whatever combination of projects is currently active. `otto docker
compose up`, `compose down` and `compose build` all speak use-cases, and so
does the library API that instructions and tests import.

The unit a project declares is not the whole use-case — it is a **fragment** of
one. Every active project contributes the fragments it declares under the name,
otto decides which fragments take part, assembles one env mapping, and runs
**one** `docker compose up` on the parent over the merged file set. Which lab
host the parent is, is {ref}`Which host <docker-which-host>`.

```toml
# project-a/.otto/settings.toml
[[docker.images]]
name = "project-a-api"                   # the image name, verbatim; only for an
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
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ fragment            ┃ provides         ┃ host  ┃ env keys  ┃ status    ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ project1[core,edge] │ edge (priority   │ test3 │ EDGE_ADDR │           │
│                     │ 10)              │       │           │           │
│ project2[core]      │ -                │ test3 │ -         │           │
│ project2[mock-edge] │ edge (priority   │ -     │ -         │ displaced │
│                     │ 0)               │       │           │           │
└─────────────────────┴──────────────────┴───────┴───────────┴───────────┘
docker: edge goes to project1 (priority 10); project2 (priority 0) stands down
```

A `<project>[<handles>]` cell names one fragment: the project that declared it and
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
# project-a/docker/compose.yml — a deliverable product artifact
services:
  api:
    image: project-a-api:latest
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
# project-a/.otto/settings.toml — the only file that speaks otto
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
project root, with the image already built:

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

"In scope" is each participating project's project scope (see {doc}`../projects`),
unioned across every project taking part in the deployment. It is *not* narrowed to
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
2. **The project adapter** — code, for values only code can compute
   ([below](#the-project-adapter)).
3. **The caller** — `--env K=V` and `--env-file PATH` on the CLI, `env=` and
   `env_files=` on the library verbs. Wins over everything (`--env` over
   `--env-file`).

### When two fragments set the same key

Channel 1 is assembled from *every* participating fragment, in selection
order, and a later fragment's value silently replaces an earlier one's. There
is no refusal and no warning.

The practical consequence: a variable two projects both care about is not a
coordination mechanism. If the value matters, name it something only one
fragment sets, or pin it from the caller with `--env`, which beats every
fragment. `otto docker use-cases` lists each fragment's env KEY names, so an
overlap is visible before you deploy.

The final mapping reaches compose as its **process environment**. otto stages
an env launcher, `otto-env.sh`, beside the compose files: one line,
`exec env 'K=V' … "$@"`, each value quoted for the shell and never evaluated.
Every compose command that loads the deployment's files runs under it: `up`,
a whole `down` and the rollback of a failed start, as
`sh …/otto-env.sh docker compose …`. So those behave as if you had exported
the mapping on the parent and run compose by hand, with nothing to set up
there, and `up` and `down` see the same values:

- a key in the mapping wins over the same variable in the parent's own
  environment;
- a variable the mapping does not name is the parent's, as it would be for
  compose typed by hand;
- `COMPOSE_*` and `DOCKER_*` settings in the mapping reach those commands the
  way an export would.

The commands that go by the project label alone do not run under the
launcher: a `down` that names services (`stop` and `rm`), `otto docker compose
ps` and `logs`, the label-only `down` when the staging is gone, otto's own
`docker` probes and the `docker exec` behind each container host. A
`DOCKER_HOST` or `DOCKER_CONTEXT` in the mapping therefore moves only the
commands above it; everything else talks to the parent's default daemon
([#749](https://github.com/ludachrish3/otto-sh/issues/749)).

otto passes no `--env-file`, so compose falls back to its own `.env` lookup,
below the mapping, exactly as compose typed by hand on the parent would:

- it reads a `.env` in the first compose file's staged directory when one is
  there. otto puts one there only when the first compose file declares
  `env_file: .env`, or an adapter generates an extra file named `0/.env`; that
  file then also feeds `${VAR}` interpolation and settings such as
  `COMPOSE_PROFILES`;
- some compose versions (v2.27.1, for one) also read a `.env` in the directory
  compose runs from: the connecting account's working directory on the
  parent, usually its home. A variable there that the mapping does not set
  reaches compose.

## Provider competition: swapping a mock for the real thing

The examples from here on are a different deployment from the templating
walkthrough above — `project1` and `project2`, rather than the walkthrough's
`project-a`. Read each half on its own.

Two projects can offer the same thing. A project that owns the real edge service
and a project that ships a mock of it both want to supply `edge` — and they must
never both run.

A fragment may declare `provides` (a capability name) and `priority`:

```toml
# project1 — the real edge
[[docker.use_cases]]
name = "integration"
composes = ["core", "edge"]
provides = "edge"
priority = 10

# project2 — the mock
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
  invocation with `--provide edge=project1` (repeatable), or fix the priorities.
  A tie between two fragments of the *same* project is always a config error —
  there is no knob for it.

The convention (documented, not enforced): real infrastructure declares a
positive priority, mocks omit it (`0`), and a higher-fidelity mock may rank
above a lower one.

Which fragments are even present to compete is decided by **project
activation** — a lab's `lab_patterns` and `-I`/`-E` (see
{doc}`../projects`). Precedence is declared once, at the provider; each
lab/project combination merely changes who shows up.

### Losing is whole-fragment — a worked example

Project1's fragment above contributes *two* compose handles, `core` and `edge`,
and it is the one carrying `provides = "edge"`. Deployed normally, project1 wins
and both of its files are in the merged stack:

```console
$ otto --lab unix --dry-run docker compose up integration
… Resolved plan: test3 <- project1[core,edge], project2[core].
  Displaced: edge goes to project1 (priority 10); project2 (priority 0) stands down. …
```

Now hand the capability to the mock:

```console
$ otto --lab unix --dry-run docker compose up integration --provide edge=project2
… Resolved plan: test3 <- project2[core], project2[mock-edge].
  Displaced: edge goes to project2 (priority 0); project1 (priority 10) stands down. …
```

Project1's `core` file — and the `api` service in it — is **gone**, not just its
`edge` file: a fragment is the atomic unit of participation, and losing means
the fragment stands down entirely. If project1's `api` must survive a mock swap,
it belongs in a *separate* fragment that declares no `provides`, exactly as
project2 splits its own `core` from its `mock-edge`.

`--provide` narrows the field to one project *before* ranking, so the winner can
carry a lower priority than the fragment it displaced — as it does above. The
displacement line names who won, at what priority, and who stood down.

## Where the stack lands

Every fragment of a use-case lands on the one parent, so the deployment is one
merged stack on one host. Which host that is — `--parent`, or the lab's
`docker_priority` — is {ref}`Which host <docker-which-host>`. Services that
must run on different hosts belong in different use-cases, each brought up with
its own `--parent`; addressing between them flows through env values
(`env`, `pass_env`, `--env`) on the second use-case, not through otto.

(docker-staging)=
## Where otto stages files on the parent

Every file a deployment needs on the parent is staged under one private
directory per account: `/tmp/otto-docker-<login>/`, where `<login>` is the
account the parent's creds log in as. The compose files, their sidecars and
the env launcher go to `<project>/compose/`, build contexts to
`<project>/build/`, and each `put`/`get` to a container passes through its own
directory under `.transfer/`.

The account is read from the parent's `creds`; a `username` given in
`ssh_options.extra` is not consulted, so a parent that names its login only
there is refused.

otto creates the directory with mode 0700, so only that account (and root) can
list, read or change what is in it. Before it writes there, and before a `down`
runs the launcher it finds there, otto checks that `/tmp` is owned by root with
the sticky bit set and that the directory is a real directory (not a symbolic
link) owned by your account with mode exactly 0700. Anything else is refused
with an error naming the path, and nothing is written or run: otto never
repairs or takes over a directory it did not make. Remove it on the parent (as
its owner, or as root) and run again.

The directory is per account, not per person. Two people who share one login
on a parent and deploy the same project still share one staging directory, as
they did before; that setup is not supported.

Commands and uploads must run as that one account. A parent whose creds reach
the docker account through a login proxy (`proxy = "su"` with `via`), or whose
uploads authenticate as a different account than its commands, is refused,
and the error names both accounts. Declare the account that runs docker as a
direct cred on the parent instead (see
[Credentials and login proxies](../../configuration/host-sources.md#credentials-and-login-proxies)).

A `down` leaves the staged files in place: the next `down` reads the same
files back, so it removes nothing the first one kept. otto never removes
`/tmp/otto-docker-<login>/<project>/`; they stay readable only by your
account. Once the deployment is down and you no longer need its files, delete
that directory on the parent by hand.

### Staging left by otto 0.17

otto 0.17 staged every account's files in one shared, world-readable
directory, `/tmp/otto-docker/<project>/`. A `down` of a deployment that 0.17
started, with nothing staged in the new location, reads its files back from
there, but only when `/tmp/otto-docker`, the project's directory, its
`compose/` directory, the numbered directories under it, the compose files
in them and the launcher are all owned by your account, none is a symbolic
link, none is writable by group or others, and your account can search each
directory. Those checks apply only when the deployment's directory is there:
a `/tmp/otto-docker` that holds nothing of this deployment is not used, whoever
owns it. A directory otto cannot look into is never taken for empty: a
deployment directory hidden behind one is refused, not skipped.
Otherwise `down` refuses with an error naming the path and stops nothing,
rather than run a launcher another account could have written or tear the
stack down by its label: run `docker compose -p <project> down` on the parent
yourself. otto
writes nothing new there. Once your 0.17 deployments are down, delete
`/tmp/otto-docker` on the parent: anyone can read it, and it holds your old
env launcher.

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

## The project adapter

For values only code can compute, or for compose files that must be *rendered*
rather than shipped verbatim, a project registers an adapter from its init
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
`hosts`, `files` (the project's winning compose files by handle), and
`scratch_dir`, a private temp dir the adapter may write to.
{class}`~otto.docker.adapter.AdapterResult` returns `files` (compose handle ->
replacement text; omitted handles ship verbatim), `extra_files` (extra files
staged beside the compose files, for `env_file:`-style references), and `env`,
which merges as channel 2.

Adapters must be **pure with respect to devices** — no host access. They run
under `--dry-run` too, so the full plan is printable. One adapter per (project,
use-case): a second registration for the same use-case raises
{class}`~otto.registry.DuplicateRegistration`, unless it passes
`overwrite=True` (`@register_compose_adapter("integration", overwrite=True)`),
which replaces the project's adapter deliberately. Registering outside an init
module's import raises {class}`~otto.registry.RegistrationRefused`, since that
import is what names the project. Both are decided when the decorator is
applied, not when `register_compose_adapter(...)` is called: the adapter
belongs to the project whose init import applies it, and applying it outside an
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

### What `down` removes

A whole-deployment `down` removes only what the deployment started. It goes
through the compose files the last `up` staged on the parent, read back as
they are, never re-rendered or re-staged, so only the services those files
declare are removed, with the networks compose created for them. A container
that another file set started under the same compose project stays up. The
limits of that rule are compose's own:

- a container with the same project **and** the same service name is removed,
  whichever file set started it;
- a declared network that is not `external` is removed unless something still
  uses it;
- volumes and bind-mounted data stay: `down` passes no `-v`;
- a service behind a compose `profiles:` entry that a narrowed `up` started by
  name survives a whole-deployment `down`, as it does a plain `docker compose
  down`; name it (`down <usecase> <service>`) to remove it;
- with `COMPOSE_PROJECT_NAME` exported in the parent's environment or set in
  the mapping, a staged file that compose can no longer load makes compose
  fall back to that name and tear down everything under it, as the same
  command typed on the parent would.

A compose handle is one path segment: a `name` containing `/` or `\`, or one
that is `.` or `..`, is refused when the lab configuration loads, since each
staged file sits directly in its own numbered directory.

The files are the last `up`'s, whether or not that `up` succeeded: an `up` of a
different selection that failed leaves its own files staged, so a later `down`
goes through those, and a service only the earlier deployment declared
survives it.

Because `down` reads the files back, compose loads and checks them again: a
staged file compose now rejects (after a compose upgrade, say) fails the
`down` with compose's own error.

When the parent no longer holds the staged files (its `/tmp` was cleared, or
the stack was started outside otto), the project label is all that is left to
go by: `down` falls back to `docker compose -p <project> down
--remove-orphans`, which removes every container under the label, and says so
in an INFO log line.

If otto cannot tell whether the files are there (the parent cannot be read, or
what it holds is not what an `up` leaves), `down` refuses with an error naming
the staging directory and stops nothing, rather than widen the teardown on a
guess. The container hosts stay registered, since their containers are still
up. Removing that directory on the parent makes the next `down` go by the
project label instead, which removes every container under it. An `up` that
failed while copying its files leaves such a directory: its compose files
without the `otto-env.sh` beside them. So does a staging left by an otto
older than the env launcher.

### Dry run

`otto --dry-run docker compose up <usecase>` prints the resolved plan and declines at
the first device touch — see {doc}`../dry-run` for the contract. Because
selection, env assembly and the adapters are all pure, the preview
includes the **exact** compose command on the parent and the env launcher it
would run under, not a description of them. The launcher shows each key with
its value replaced by `<redacted>`: the preview is printed and logged, and the
mapping can carry secrets.

```console
$ otto --lab unix --dry-run docker compose up integration
'deploy(integration)' was not run on host 'test3': this is a dry run, which
contacts no device. … Resolved plan: test3 <- project1[core,edge], project2[core].
Displaced: edge goes to project1 (priority 10); project2 (priority 0) stands down.
Fragment env keys: ['EDGE_ADDR']. No image was built, no file was staged and
no container was started. The adapters ran (plain data; the only thing one may
write is its own scratch dir), so this is the command itself, not a
description of it. On test3, with the env launcher `exec env
'EDGE_ADDR=<redacted>' "$@"` (values redacted), would run: sh
/tmp/otto-docker-vagrant/unix-integration-vagrant/compose/otto-env.sh docker
compose -p unix-integration-vagrant -f …/core.yml -f …/edge.yml -f …/core.yml
up -d --remove-orphans
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
  [Container hosts](index.md#container-hosts) describes. A project migrating from
  a composes-only declaration keeps its container ids literally unchanged by
  naming its use-case after the project.

## From instructions and tests

The CLI is a thin wrapper. The same deployment from Python:

```python
from otto.docker import deployed


async with deployed("integration", own=True) as stack:
    await stack.hosts["api"].run("./run-tests")
```

{func}`~otto.docker.deployment.deployed` is the recommended scope — see
{doc}`../../setup/test-recipes` for the sharing contract and
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
