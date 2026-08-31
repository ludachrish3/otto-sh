# Docker use-cases — scenario deployment across projects

**Date:** 2026-08-30
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** nothing unlanded. Builds on per-project lab/host scoping
(`afc417c4`), lab.json v2 (`0fba081a`), the dry-run contract (`03407261`),
and the shipped `otto.docker` package. Supersedes and absorbs
`todo/docker-support-improvements.md`.

## 1. Goal

Give otto a first-class **use-case**: a named, cross-project deployment
scenario. `otto docker up integration` (or `await deploy("integration")`
from an instruction) selects the participating compose fragments from every
active project, resolves which lab host each lands on, assembles the env
the product's compose files consume, and runs **one merged compose stack
per resolved host** — then registers the containers as lab hosts exactly as
today.

### In scope

- The `[[docker.use_cases]]` fragment schema and the `roles` host field in
  `lab.json` (§3).
- Fragment selection: provider competition with declared priorities (§4).
- Placement: role resolution inside each repo's scoped universe, explicit
  tie-break knobs, no implicit precedence (§5).
- The config layer: three env channels, fact references, the repo adapter,
  zero automatic injection (§6–§7).
- The deploy pipeline, staging changes, naming, registration (§8–§9).
- CLI surface (`up`/`down [USE_CASE]`, `use-cases`, `--provide`, `--env*`)
  and the library API (`deploy`/`teardown`/`deployed`) (§10–§11).
- Errors and the dry-run story (§12).
- Workstream 0: verify the current verbs against the bed, make empty
  selection loud, fix two documented inaccuracies (§13).
- Hard cutover from `default_host` (§14). Testing (§15) and docs (§16).

### Out of scope

- Image push to registries; local (non-parent) builds — unchanged limits.
- Cross-host container networking (no overlay wiring; cross-host addressing
  flows through env values).
- Host scheduling: roles carry no ranks, otto never chooses between
  candidate hosts. The moment that is wanted, it is a scheduler and a new
  design.
- `otto docker images` (follow-ups ledger, §17).

## 2. Principles

1. **Products own their deploy interface; otto adapts to it — never the
   reverse.** A repo's compose files and templating code may be deliverable
   product artifacts. They must stay deployable with no otto anywhere:
   no `OTTO_*` variables in product files, no otto imports in product
   templating code, no otto template syntax over product files. Otto-aware
   glue lives only where glue already lives (`.otto/settings.toml`, init
   modules).
2. **Selection, not merge.** A mock and its real counterpart trade places
   by *fragment selection* (winner in, loser out entirely), never by YAML
   merge order — compose's per-key merge would bleed mock settings into
   the real service.
3. **Activation does the combinatorics.** Which fragments compete is
   decided by project activation (labs' `lab_patterns` + `-I`/`-E`),
   which already shipped. Precedence is declared once, at the provider;
   each lab/project combination merely changes who is present to compete.
4. **Ambiguity is a configuration error.** Empty or multiple role matches,
   provider ties — hard errors naming the candidates and the knobs. No
   implicit winner, no dependency-order arbitration between projects.
5. **Zero injection.** No env var reaches the compose environment unless a
   channel explicitly mapped it. Products see only variables their own
   contract names.

## 3. Schema

### 3.1 `settings.toml`

```toml
[docker]
registry_url = "docker.io"            # unchanged

[[docker.images]]                     # unchanged
name = "api"
dockerfile = "docker/api.Dockerfile"
context = "docker"

[[docker.composes]]                   # now a pure file inventory
name = "core"                         # NEW - referenceable handle; optional,
                                      # defaults to the file stem; unique
path = "docker/compose.yml"
services = ["api", "db"]              # the names in THIS file's services:
                                      # block; db runs a published image
                                      # (postgres-style), so no image entry
                                      # default_host: REMOVED (hard cutover, §14)

[[docker.use_cases]]                  # a FRAGMENT; repeatable; same name = same use-case
name = "integration"
composes = ["core"]                   # handles into [[docker.composes]]
role = "edge"                         # placement role (optional, §5)
placement = { edge = "test3" }        # optional committed pin; values may be
                                      # lab-qualified ("unix:test3") for multi-lab sessions
provides = "edge"                     # optional: candidate provider of a capability
priority = 10                         # optional, default 0, higher wins; only with provides
env = { LOG_LEVEL = "debug", EDGE_ADDR = "${otto:role.edge.addr}" }  # channel 1, §6
pass_env = ["EDGE_TAG"]               # optional allowlist from the invoking shell, §6
```

`services` names the entries of this file's own `services:` block — local
to the repo, never other projects'. Its cardinality is unrelated to
`[[docker.images]]`: one built image can back several services, and a
service may run a published image otto never builds (the `db` above). It is
declared rather than parsed because an adapter-rendered file (§7) need not
be valid YAML until deploy time; the declaration is cross-checked against
`compose config --services` at up on the legacy per-repo path (as today);
extending the cross-check to the use-case deploy path is a ledgered
follow-up — 0.10.0 ships without it there.

A fragment is the atomic unit of participation *and* placement. Services
that must land on different hosts are different fragments. A repo may
declare any number of fragments per use-case name.

### 3.2 `lab.json`

Host entries grow an optional `"roles": ["edge", "builder"]` list beside
`docker_capable`. Roles are **lab intent** ("what this lab uses the machine
for"), not machine facts — they live in `lab.json` proper and never in the
inventory layer. Multi-role hosts are natural; multiple hosts claiming one
role is representable and caught at resolution (§5).

## 4. Fragment selection — provider competition

Per use-case name, over the **active** repos only:

1. Fragments without `provides` always participate.
2. Fragments sharing a `provides` capability compete: highest `priority`
   wins; its composes join the deployment, every loser is excluded whole.
3. An exact tie is a hard error naming both fragments — resolved per
   invocation with `--provide <capability>=<repo>` (repeatable), or by
   fixing the priorities. A tie between two fragments of the *same* repo
   is always a config error; no knob.

Convention (documented, not enforced): real infrastructure declares a
positive priority; mocks omit it (0); higher-fidelity mocks may rank above
lower ones. Displacements are reported by `otto docker use-cases` and in
`up`'s selection report ("repo-b/mock-edge.yml — displaced by repo-a,
priority 10 > 0").

`provides` is a single capability string in v1 (list form is a follow-up
if a real fragment ever needs two).

## 5. Placement

Each winning fragment resolves its own placement, in this order:

1. `--on HOST` — collapses **every** fragment of the invocation to HOST.
2. The fragment's `placement` pin for its role (lab-qualified allowed).
3. Its `role`, resolved **inside the owning repo's scoped universe**
   (per-project scoping): exactly one docker-capable host in scope carrying
   the role wins; zero or several is a hard error listing candidates and
   knobs 1–2.
4. No `role` at all: if the repo's scope holds exactly one docker-capable
   host, that host; otherwise a hard error.

Because resolution is repo-scoped, two projects' fragments naming the same
role in the same lab resolve identically by construction, and cross-project
collisions cannot happen through the lab at all — only through provider
competition (§4), which is where they are meant to be decided. Fragments
resolving to different hosts split the use-case into one merged stack per
host; addressing between them flows through env values (§6).

## 6. The env mapping — three channels, two sinks

One ordered mapping per deployment, later channels winning:

1. **Static table** — the fragment's `env`. Values are literal except the
   `${otto:...}` fact-reference form, resolved by otto at deploy time.
   This syntax is valid **only inside `settings.toml`** (otto's own file),
   never over product files. v1 namespace:
   `${otto:use_case}`, `${otto:compose_project}`,
   `${otto:role.<role>.addr}`, `${otto:role.<role>.host_id}`,
   `${otto:host.<id>.addr}`, `${otto:parent.addr}`.
   Unknown reference = config error. Anything not matching `${otto:` is
   passed through untouched, so product `${VAR}` strings are safe as
   values. `pass_env` names variables copied from the invoking user's
   shell (explicit allowlist; absent vars are simply unset, reported by
   the selection report).
2. **Repo adapter** — registered code for values only logic can compute
   (§7). Its returned env merges over channel 1.
3. **Caller/CLI** — `env=` / `env_files=` on the library verbs,
   `--env K=V` / `--env-file PATH` (local path, read client-side) on the
   CLI. Merges over everything.

Two sinks, both fed the identical final mapping:

- a staged env file passed via `docker compose --env-file`, and
- the **remote process environment** of every compose invocation
  (`env K=V ... docker compose ...`).

Both sinks because compose consumes env at parse time (`${VAR}`
interpolation), at container-env time (`environment:` pass-through — whose
resolution rules differ across compose versions), and for its own knobs
(`COMPOSE_*`, `DOCKER_*`). Feeding both makes the version differences
moot: the deployment behaves as if the user had exported the mapping and
run compose by hand. Zero injection (§2.5): otto adds nothing of its own.

The product side of the contract, concretely — a consuming compose file is
plain compose, indistinguishable from one driven by hand-exported vars:

```yaml
# product compose.yml — no otto anywhere, deployable without it
services:
  api:
    image: repo1-api:latest
    environment:
      - EDGE_ADDR                      # pass-through from the compose env
      - LOG_LEVEL=${LOG_LEVEL:-info}   # native interpolation, native default
    extra_hosts:
      - "edge:${EDGE_ADDR}"            # interpolation into any value slot
```

```toml
# .otto/settings.toml — the only file that speaks otto
[docker.use_cases.env]
EDGE_ADDR = "${otto:role.edge.addr}"   # otto fact -> product-named var
LOG_LEVEL = "debug"
```

The decoupling test is executable, and docs/tests should treat it as the
contract: `EDGE_ADDR=10.0.0.5 docker compose up -d` run by hand must behave
identically to the otto deployment. `${otto:...}` resolves entirely on
otto's side of the boundary; the compose file only ever sees resolved
values under the names the product chose. Compose-native env features (`:-`
defaults, `${VAR?err}` refusals, a product-shipped `.env`) keep working —
otto parses none of them.

## 7. The repo adapter

For arbitrary, repo-controlled templating. Registered from the repo's init
module — the one place that is already otto-dependent by design:

```python
from otto.docker import register_compose_adapter, AdapterResult

@register_compose_adapter("integration")        # repo attributed via init module,
def render(facts):                              # same idiom as register_project_actions
    rendered = my_product.deploy.render(        # product code: zero otto imports
        template_dir=..., edge_addr=facts["roles"]["edge"]["addr"])
    return AdapterResult(files={"core": rendered}, env={"WORKER_TAG": "1.4"})
```

Contract:

- `facts` is **plain data** (JSON-able mapping): `use_case`,
  `compose_project`, `parent` (`{id, addr}`), `roles`
  (`{name: {host_id, addr}}`), `hosts` (`{id: {addr}}` — the owning repo's scoped universe), `files`
  (`{compose-handle: local path}` for this repo's winning fragments),
  `scratch_dir` (a private temp dir the adapter may write to).
- `AdapterResult.files` maps compose handles to replacement text (omitted
  handles ship verbatim); it may also introduce extra sidecar files staged
  beside the compose files (for `env_file:`-style references).
  `AdapterResult.env` merges as channel 2.
- Adapters must be pure with respect to devices: no host access. They run
  under `--dry-run` (that is what makes the full plan printable). The
  registration line is the only otto touchpoint; everything beneath it is
  the product's own code.

One adapter per (repo, use-case); a repo registering a second for the same
use-case fails loud, like `register_project_actions`.

## 8. Deploy pipeline

`deploy(use_case)` — CLI and library share it verbatim:

1. **Select** (§4) over active repos; print the selection report: every
   fragment in, displaced, or excluded-with-reason. Empty selection is a
   hard error (§12), never a silent exit 0.
2. **Place** (§5); group winning fragments by resolved host.
3. **Build** every winning repo's declared images, in
   `resolve_dependencies` order, context-hash skip as today.
4. **Per host:** assemble the env mapping (§6) → run each repo's adapter
   (§7) → stage compose files (rendered or verbatim), sidecar files, and
   the generated env file → one
   `docker compose -p <lab>-<usecase>-<suffix> -f ... --env-file ... up -d --remove-orphans`
   with the mapping in the process env. `-f` order = repo dependency
   order (later overrides earlier on the rare shared key; a same-service
   key collision across fragments logs a warning, since replacement is
   §4's job, not the merge's).
5. **Register** containers, return a `UseCaseStack`.

Teardown mirrors: per host, `compose down` on the merged project, then
close-and-unregister container hosts (children before parent, as today).
Staging fixes riding along: files referenced by service-level `env_file:`
keys are staged beside their compose file (today they are silently not
shipped); staging stays keyed by compose project so concurrent suffixed
runs cannot collide.

Existing hardening is inherited: partial-up rollback, the libnetwork-race
retry, container-id resolve polling. One deliberate change: the already-up
probe no longer short-circuits `up -d` (today any running container under
the project name turns `up` into a lookup). `up -d` is convergent — it
creates what is missing, leaves unchanged services running, and recreates
only services whose config or image changed — so re-running a broader
deployment *adds on* the newly active projects' services to a live stack.
`--remove-orphans` makes provider transitions concrete: when a newly
active real provider displaces a mock, the mock's still-running container
is an orphan of the merged file set and is removed by the same `up`. The
probe's one remaining job is answering `deployed()`'s ownership question,
and ownership stays **stack-level**: `deployed()` tears down all or
nothing — per-container "only what I added" is a non-goal (compose keeps
no such ledger, and diffing before/after would fake one).

## 9. Naming and registration

- Compose project: `<lab>-<usecase>-<suffix>` (lab = the resolved
  host's source lab, slugged to compose-legal characters; suffix = user or
  `OTTO_COMPOSE_SUFFIX`, as today — the username default keeps concurrent
  users' stacks isolated on a shared host). No `otto-` prefix: the deployment
  belongs to the product, and branding it with the enabling tool is
  exactly the backwards dependency §6 refuses (the legacy per-repo
  `otto-<repo>-<suffix>` naming keeps its prefix until that path is
  removed). One project per (use-case, host). The
  lab segment is load-bearing, not cosmetic: `--remove-orphans` reaps
  within a project, and one docker host can serve containers for several
  labs — two labs must never share a project. It also makes `docker ps`
  on a shared host visually attributable to a lab from outside otto.
- Container host ids: `<parent>.<usecase>.<service>` (unchanged: inside
  otto the parent already carries its lab; container listings surface the
  parent's lab alongside the id).
- Placeholder registration (`register_declared_container_hosts`) walks
  use-cases instead of composes: best-effort role resolution at lab load;
  a fragment whose placement cannot be resolved yet simply contributes no
  placeholder (completion only; deploy-time resolution stays authoritative
  and loud).
- Auto-start on access keeps working: a placeholder's stack is the
  use-case's merged stack, brought up with `build=False` as today.

## 10. CLI surface

```text
otto docker build [USE_CASE] [--repo NAME] [--on HOST] [--rebuild] [IMAGE]...
otto docker up    [USE_CASE [SERVICE]...] [--on HOST] [--no-build] [--provide CAP=REPO]... [--env K=V]... [--env-file PATH]...
otto docker down  [USE_CASE [SERVICE]...] [--on HOST] [--provide CAP=REPO]...
otto docker ps    [--on HOST]                            # unchanged
otto docker use-cases [USE_CASE]                         # NEW, read-only, no output dir
```

- `up`/`down` with no argument: if the active repos declare exactly one
  use-case, it is chosen; several → hard error listing them.
- `build USE_CASE` restricts to the winners' images; bare `build` keeps
  today's all-active-repos meaning.
- Trailing SERVICE names (allowed only after an explicit use-case name, so
  the positionals stay unambiguous) narrow the verb to those services:
  `up` composes just them (compose may also start their `depends_on`
  dependencies); `down` stops and removes just their containers, leaving
  the rest of the stack and its network standing. Registration and
  unregistration scope to the named services.
- `use-cases` renders each use-case: contributing repos and fragments,
  provider outcomes (winner/displaced), roles and their resolution in the
  active lab (or the error they would produce), env keys by channel
  (names, not values — adapters do not run here), target hosts. It is the
  inventory view; action preview belongs to `--dry-run`.
- Tab completion: use-case names (from discovery, like instruction names);
  `--on` completes docker-capable hosts as today.
- `--repo` disappears from `up`/`down`: a merged scenario is not per-repo.
  Narrowing is done by use-case name; `build` keeps `--repo` for its
  per-repo meaning.

## 11. Library API

```python
from otto.docker import deploy, teardown, deployed, UseCaseStack

async def deploy(use_case: str, *, services: Sequence[str] | None = None,
                 env: Mapping[str, str] | None = None,
                 env_files: Sequence[Path] | None = None,
                 on: str | None = None, provide: Mapping[str, str] | None = None,
                 build: bool = True) -> UseCaseStack: ...
async def teardown(use_case: str, *, services: Sequence[str] | None = None,
                   on: str | None = None, stop_timeout: int = 1) -> None: ...
@asynccontextmanager
async def deployed(use_case, *, own: bool = False, **kw) -> AsyncIterator[UseCaseStack]: ...
```

`UseCaseStack`: mapping `service -> DockerContainerHost` across all hosts
of the deployment, plus `by_host`, `env` (the final mapping), and the
selection report data. `deployed()` keeps `composed()`'s sharing contract
(tear down only what it brought up unless `own=True`). The existing
per-repo primitives (`compose_up`/`compose_down`/`composed`/`build_images`)
remain public — they are what `deploy` is built from, and instructions
already use them.

## 12. Errors and dry-run

- Resolution failures — unknown use-case, empty selection, role
  ambiguity/absence, provider tie, unknown `${otto:...}` reference,
  unknown `--provide` target — are configuration refusals: `ValueError`
  family, candidates and knobs named, nothing touched. CLI exits 1.
- Everything through the adapter is pure, so
  `otto --dry-run docker up integration` prints the complete plan
  (winners, displacements, hosts, files, env keys, the exact compose
  command) and declines at the first device touch, per the dry-run
  contract. Device-touching verbs keep their existing
  `CommandNotRunError` arms.

## 13. Workstream 0 — verify, loudness, doc corrections

Independent of the feature; lands first.

1. **Reproduce the reported `--on` failure.** The e2e suite drives
   `up`/`down --on` against real daemons, so the code path works; the
   likely culprit is `_select_repos` silently skipping a repo whose
   `default_host` is not in the active lab (DEBUG log, exit 0, no
   output). Reproduce exactly that shape, then run the targeted e2e slice
   against the bed to confirm the verbs themselves.
2. **Make empty selection loud.** `up`/`down`/`build` selecting zero
   repos: print why each candidate was excluded, exit 1.
3. **Fix two doc inaccuracies now:** `settings.md` claims `services` is
   "used for tab-completion only" (it is the authoritative registration
   list); the per-verb pages claim `--on` defaults to "all docker-capable
   hosts" for `build`/`up`/`down` (they default to each repo's
   `default_host` and error without one).

## 14. Cutover (hard)

- `DockerComposeSpec`: `default_host` removed, `name` added (optional —
  defaults to the compose file's stem; effective names must be unique).
  `DockerUseCaseSpec` added. Breaking-change commit with migration note:
  a composes-only repo adds one three-line `[[docker.use_cases]]` entry;
  naming it after the repo keeps container ids literally unchanged.
- No implicit use-case synthesis, no deprecation shims.
- Touch list: `models/settings.py`, `config/repo.py`, new
  `docker/resolve.py` (selection/placement/env engine),
  `docker/compose.py` (`_resolve_parent` retires into the engine),
  `docker/staging.py` (sidecars, rendered files), `cli/docker.py`,
  `cli/init_templates.py` scaffold, completion cache (use-case names),
  sample repos (`tests/repo1`, `tests/repo2`), docs (§16).

## 15. Testing

- **Unit:** the resolution engine is pure — table-driven tests for
  competition (priorities, ties, displacement report), placement
  (scope, knob precedence, error text), env assembly (channel order,
  fact refs, pass_env, unknown-ref refusal), adapter invocation. Every
  new guard mutate-and-observe-red per house rule.
- **CLI unit:** new args, empty-selection exit, `use-cases` rendering.
- **Integration (test3):** two-repo merged stack up/exercise/down; a
  real/mock displacement pair added to the repo1/repo2 fixtures; sidecar
  `env_file:` staging; env visible inside a container.
- **E2E CLI:** `use-cases`, `--provide`, `--env-file`, the loud
  empty-selection message (pins W0), dry-run plan output.
- Existing docker suites keep passing with fixtures migrated to the new
  schema (the cutover's own regression net).

## 16. Docs

One home per topic; link, never restate:

- New `guide/cli/docker/use-cases.md` — the concept page: fragments,
  providers, roles, env channels, adapter (the workflow home). It MUST
  carry a dedicated "how templating works" walkthrough of the two-sided
  mechanism (§6's worked pair): compose-native interpolation on the
  product side, `${otto:...}` fact resolution confined to otto's side,
  and the executable decoupling test as the framing. Explicit review
  feedback: powerful but non-obvious — document it prominently, not as
  an aside.
- `guide/configuration/settings.md` — schema (source of truth for TOML).
- `guide/configuration/lab-config.md` — the `roles` host field.
- `architecture/subsystems/docker-hosts.md` — a short design section on
  selection/placement pointing at the guide.
- `library/suite-recipes.md` — `deployed()` replaces `composed()` as the
  recommended scope for use-case work.
- W0 corrections (§13.3).

## 17. Follow-ups ledger

- `otto docker images` — list built images per parent.
- Workspace-level provider preference (env var) if `--provide` proves
  repetitive.
- `provides` as a list; ranked role candidates (only if scheduling
  pressure ever materializes).
