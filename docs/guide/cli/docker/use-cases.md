# Use-cases

A **use-case** is a named, cross-repo deployment: "bring up `integration`" is
one command whatever combination of projects is currently active. `otto docker
up`, `down` and `build` all speak use-cases, and so does the library API that
instructions and suites import.

The unit a repo declares is not the whole use-case — it is a **fragment** of
one. Every active repo contributes the fragments it declares under the name,
otto decides which fragments take part, works out which lab host each lands
on, assembles one env mapping, and runs **one** `docker compose up` per host
over the merged file set.

```toml
# repo-a/.otto/settings.toml
[[docker.composes]]
name = "core"                         # a handle for this file
path = "docker/compose.yml"
services = ["api"]                    # the names in its services: block

[[docker.use_cases]]
name = "integration"                  # the use-case this fragment joins
composes = ["core"]                   # handles from above
role = "edge"                         # which lab host it wants (below)
```

`otto docker up integration` now deploys it, and the container comes back as
the lab host `<parent>.integration.api` — see
[Container hosts](index.md#container-hosts).

Only `name` and `composes` are required; `role` is shown because you need it
as soon as the repo's scope holds more than one docker-capable host, which is
the common case. Drop it when there is exactly one, and the fragment is three
lines.

{doc}`../../configuration/settings` is the schema reference for every key
above; this page is about what they *mean*.

## Seeing what is declared before deploying anything

`otto docker use-cases` is the inventory view. It contacts nothing, starts
nothing, and creates no output directory, so it answers the same with or
without `--dry-run`:

```console
$ otto --lab unix docker use-cases integration
                              use-case integration
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ fragment         ┃ role   ┃ provides         ┃ host  ┃ env keys  ┃ status    ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ repo1[core,edge] │ docker │ edge (priority   │ test3 │ EDGE_ADDR │           │
│                  │        │ 10)              │       │           │           │
│ repo2[core]      │ docker │ -                │ test3 │ -         │           │
│ repo2[mock-edge] │ docker │ edge (priority   │ -     │ -         │ displaced │
│                  │        │ 0)               │       │           │           │
└──────────────────┴────────┴──────────────────┴───────┴───────────┴───────────┘
docker: edge goes to repo1 (priority 10); repo2 (priority 0) stands down
```

A `<repo>[<handles>]` cell names one fragment: the repo that declared it and
the compose handles it contributes. Omit the argument to list every declared
use-case.

Env **key names** are listed, never values — a value can be a secret pulled
from your shell, and an inventory has no reason to print one.

The verb reports rather than raises: a use-case whose placement cannot be
resolved prints its refusal in place of a host and the listing still exits 0,
because "one of my six use-cases cannot place its edge fragment" is the answer
you came for, not a reason to hide the other five.

## How templating works — the two-sided mechanism

This is the part that is powerful and not obvious, so it gets its own section.

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
role = "edge"
env = { EDGE_ADDR = "${otto:role.edge.addr}", LOG_LEVEL = "debug" }
```

`${otto:role.edge.addr}` is a **fact reference**. Otto resolves it at deploy
time — "the address of whichever lab host this use-case's `edge` role landed
on" — and the resolved value is what enters the env mapping under the
product's own name, `EDGE_ADDR`.

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

### The guarantee, as an executable test

The contract is not a promise in prose — it is something you can run. From the
repo root, with the image already built:

```console
$ EDGE_ADDR=10.0.0.5 LOG_LEVEL=debug docker compose -f docker/compose.yml up -d
```

(`-f` because the file above lives at `docker/compose.yml`; from inside that
directory a bare `docker compose up -d` is the same command.)

Run by hand, with the values supplied yourself, that must behave **identically**
to otto's deployment of the same stack. `${otto:...}` resolves entirely on
otto's side of the boundary; the compose file only ever sees resolved values,
under the names the product chose. This is the decoupling test — if a change to
otto ever broke it, otto would be the thing that is wrong.

Two consequences worth stating plainly:

- **Every compose-native env feature keeps working**, because otto parses none
  of them: `${VAR:-default}`, `${VAR?message}` refusals, a product-shipped
  `.env` file, `env_file:` keys. Otto contributes values; compose does the
  interpolating.
- **The product stays deployable with no otto anywhere.** Hand the compose
  file to someone with no otto installed and it runs. That is the point of
  keeping fact references on otto's side.

### The fact-reference namespace

| Reference | Resolves to |
| --------- | ----------- |
| `${otto:use_case}` | The use-case name being deployed |
| `${otto:compose_project}` | The compose project name ({ref}`below <docker-use-case-naming>`) |
| `${otto:role.<role>.addr}` | Address of the host that `<role>` resolved to |
| `${otto:role.<role>.host_id}` | That host's lab id |
| `${otto:host.<id>.addr}` | Address of a named **unix** lab host in scope — including one that runs no containers, such as a DUT |
| `${otto:parent.addr}` | Address of the host this stack is being deployed to |
| `${otto:parent.id}` | That host's lab id |

"In scope" is the same project-scoping clause placement uses, unioned across
every repo taking part in the deployment. It is deliberately *not* narrowed to
docker-capable hosts: telling a container the address of the bench device it
is supposed to drive is the point. Two limits are real, though — the namespace
covers **unix** lab hosts only, so a serial-attached or Zephyr target is not
addressable this way, and a host with no configured address is refused rather
than fabricated.

An unknown reference is a configuration refusal naming the known forms and the
roles and hosts actually available — nothing is staged and nothing is started.
Anything not matching `${otto:` is passed through untouched, which is why a
product `${VAR}` string is safe to use as a literal value.

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
is no refusal and no warning — this is the one ambiguity in the design that is
resolved rather than reported, because a merged stack's fragments routinely
share innocuous keys and refusing on every one of them would make cross-repo
use-cases unusable.

The practical consequence: a variable two repos both care about is not a
coordination mechanism. If the value matters, name it something only one
fragment sets, or pin it from the caller with `--env`, which beats every
fragment. `otto docker use-cases` lists each fragment's env KEY names, so an
overlap is visible before you deploy.

The final mapping is fed to **both** sinks: a staged env file passed as
`docker compose --env-file`, and the remote process environment of the compose
invocation itself (`env K=V ... docker compose ...`). Both, because compose
consumes env at three different moments — parse-time `${VAR}` interpolation,
`environment:` pass-through, and its own `COMPOSE_*`/`DOCKER_*` knobs — and
the rules differ across compose versions. Feeding both makes the version
differences moot: the deployment behaves exactly as if you had exported the
mapping and run compose by hand, which is the decoupling test again.

## Provider competition: swapping a mock for the real thing

The examples from here on are a different deployment from the templating
walkthrough above — `repo1`/`repo2` with `role = "docker"`, the shape the
captured output below was really run against, rather than the walkthrough's
`repo-a` with `role = "edge"`. Read each half on its own; a fragment stitched
from both would ask for a role no participating fragment carries, and be
refused.

Two projects can offer the same thing. A repo that owns the real edge service
and a repo that ships a mock of it both want to supply `edge` — and they must
never both run.

A fragment may declare `provides` (a capability name) and `priority`:

```toml
# repo1 — the real edge
[[docker.use_cases]]
name = "integration"
composes = ["core", "edge"]
role = "docker"
provides = "edge"
priority = 10

# repo2 — the mock
[[docker.use_cases]]
name = "integration"
composes = ["mock-edge"]
role = "docker"
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

This is the part that surprises people, so it is worth seeing.

Repo1's fragment above contributes *two* compose handles, `core` and `edge`,
and it is the one carrying `provides = "edge"`. Deployed normally, repo1 wins
and both of its files are in the merged stack:

```console
$ otto --lab unix --dry-run docker up integration
… Resolved plan: test3 <- repo1[core,edge], repo2[core].
  Displaced: edge -> repo1 (priority 10), repo2 (priority 0) stands down. …
```

Now hand the capability to the mock:

```console
$ otto --lab unix --dry-run docker up integration --provide edge=repo2
… Resolved plan: test3 <- repo2[core], repo2[mock-edge].
  Displaced: edge -> repo2 (priority 0), repo1 (priority 10) stands down. …
```

Repo1's `core` file — and the `api` service in it — is **gone**, not just its
`edge` file. That is correct and deliberate: a fragment is the atomic unit of
participation, and losing means the fragment stands down entirely. If repo1's
`api` must survive a mock swap, it belongs in a *separate* fragment that
declares no `provides`, exactly as repo2 splits its own `core` from its
`mock-edge`.

The same rule explains the displacement line's careful wording. `--provide`
narrows the field to one repo *before* ranking, so the winner can carry a
lower priority than the fragment it displaced — as it does above. The report
names who won, at what, and who stood down, and never calls either priority
"the higher one", because that would be false here.

## Placement: which host a fragment lands on

A fragment is the atomic unit of placement as well as participation. Services
that must land on different hosts belong in different fragments.

Each winning fragment resolves its own host, in this order:

| Precedence | Knob | Where it lives | Scope |
| ---------- | ---- | -------------- | ----- |
| 1 | `--on HOST` | The invocation | Collapses **every** fragment of the deployment onto that host |
| 2 | `placement = { edge = "test3" }` | The fragment, committed | That fragment; may be lab-qualified (`"unix:test3"`) |
| 3 | `role = "edge"` | The fragment, committed | The one docker-capable host in the repo's scope tagged with that role |
| 4 | *(no role)* | — | The repo's scope, if it holds exactly one docker-capable host |

Roles are declared on hosts in lab data as `"roles": ["edge", "builder"]` —
see {doc}`../../configuration/lab-config`. They are lab *intent* ("what this
lab uses the machine for"), not machine facts.

Role resolution happens **inside the owning repo's scoped universe** (see
{doc}`../projects`). Because of that, two projects naming the same role in the
same lab resolve identically by construction, and cross-project collisions
cannot happen through the lab at all — only through the provider competition
above, which is where they are meant to be decided.

Ambiguity is a configuration error, never an implicit winner: zero hosts
carrying the role, or several, is a hard refusal listing the candidates and
the knobs. Two committed pins refuse in their own words:

- A pin naming a host the active lab does not have is refused with the lab's
  available host ids. (A *lab-qualified* pin naming some other lab is not an
  error — it is legitimate multi-lab config, and resolution falls through to
  the role.)
- A pin naming a host that is not a docker-capable unix host is refused as
  such — otto will not deploy a container stack onto a host that cannot run
  one.

Fragments that resolve to different hosts split the use-case into one merged
stack per host; addressing between them flows through env values, which is
what `${otto:role.<role>.addr}` is for.

## The repo adapter

For values only code can compute, or for compose files that must be *rendered*
rather than shipped verbatim, a repo registers an adapter from its init module
— the one place that is already otto-dependent by design:

```python
from otto.docker import AdapterResult, register_compose_adapter


@register_compose_adapter("integration")
def render(facts):
    rendered = my_product.deploy.render(  # product code: zero otto imports
        template_dir=...,
        edge_addr=facts["roles"]["edge"]["addr"],
    )
    return AdapterResult(files={"core": rendered}, env={"WORKER_TAG": "1.4"})
```

The registration line is the only otto touchpoint; everything beneath it is
the product's own templating, with its own syntax, owned by the product.

`facts` is plain JSON-able data — `use_case`, `compose_project`, `parent`,
`roles`, `hosts`, `files` (the repo's winning compose files by handle), and
`scratch_dir`, a private temp dir the adapter may write to.
{class}`~otto.docker.adapter.AdapterResult` returns `files` (compose handle ->
replacement text; omitted handles ship verbatim), `extra_files` (extra files
staged beside the compose files, for `env_file:`-style references), and `env`,
which merges as channel 2.

Adapters must be **pure with respect to devices** — no host access. That is
what lets them run under `--dry-run`, which in turn is what makes the full
plan printable. One adapter per (repo, use-case); a second registration for
the same use-case fails loud.

See {mod}`otto.docker.adapter` for the API.

## Deploying, narrowing, and tearing down

```console
$ otto docker up integration                 # every service, every resolved host
$ otto docker up integration api db          # just these services
$ otto docker down integration api           # stop and remove just api
$ otto docker down integration               # the whole deployment
```

Trailing service names are allowed only after an explicit use-case name, so
the positionals stay unambiguous. `up` composes just the named services
(compose may also start their `depends_on` dependencies); `down` stops and
removes just their containers, leaving the rest of the stack and its network
standing. Registration and unregistration scope to the named services too.

With no use-case named at all, `up` and `down` pick the only declared one.
Zero or several is a hard error listing them — never a quiet no-op. Bare
`build` is different: it has a per-repo meaning of its own and builds every
selected repo's images without resolving a use-case at all (see {doc}`build`).

### `up` is convergent, and that is deliberate

`docker compose up -d` creates what is missing, leaves unchanged services
running, and recreates only services whose config or image changed. Otto does
not short-circuit that: re-running a broader deployment **adds** the newly
active projects' services to a live stack rather than looking up what is
already there.

`--remove-orphans` rides along on every `up`, which is what makes provider
transitions concrete. When a newly active real provider displaces a mock, the
mock's still-running container is an orphan of the merged file set — and the
same `up` removes it. The swap is one command, not a teardown plus a deploy.

### Dry run

`otto --dry-run docker up <usecase>` prints the resolved plan and declines at
the first device touch — see {doc}`../dry-run` for the contract. Because
selection, placement, env assembly and the adapters are all pure, the preview
includes the **exact** per-host compose command, not a description of one:

```console
$ otto --lab unix --dry-run docker up integration
'deploy(integration)' was not run on host 'test3': this is a dry run, which
contacts no device. … Resolved plan: test3 <- repo1[core,edge], repo2[core].
Displaced: edge -> repo1 (priority 10), repo2 (priority 0) stands down.
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
  never collide, and there is deliberately no `otto-` prefix. Why each segment
  is shaped that way is in
  {doc}`../../../architecture/subsystems/docker-hosts`.
- **Container host ids:** `<parent>.<usecase>.<service>`, as
  [Container hosts](index.md#container-hosts) describes. A repo migrating from
  a composes-only declaration keeps its container ids literally unchanged by
  naming its use-case after the repo.

## From instructions and suites

The CLI is a thin wrapper. The same deployment from Python:

```python
from otto.docker import deployed


async with deployed("integration", own=True) as stack:
    await stack.hosts["api"].run("./run-tests")
```

{func}`~otto.docker.deployment.deployed` is the recommended scope — see
{doc}`../../../library/suite-recipes` for the sharing contract and
{mod}`otto.docker.deployment` for `deploy`, `teardown` and
{class}`~otto.docker.deployment.UseCaseStack`.

## Errors

Every resolution failure — unknown use-case, empty selection, an ambiguous or
absent role, a provider tie, an unknown `${otto:...}` reference, an unknown
`--provide` target, a service name nothing declares — is a **configuration
refusal**: it names the candidates and the knobs, nothing is touched, and the
CLI exits 1. An empty selection is never a silent exit 0.

Failures *after* the first device touch are different: whatever the failed
call brought up is torn down again before the error propagates.
