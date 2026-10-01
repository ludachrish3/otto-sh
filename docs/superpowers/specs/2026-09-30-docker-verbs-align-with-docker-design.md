# The docker verbs align with docker; build and teardown have one owner — design

**Date:** 2026-09-30
**Series:** thin-CLI, item 3 (after items 1 `otto run` and 2 `otto test`)
**Issues:** fixes #493 (`otto docker down` reports success when compose down fails),
fixes #494 (`otto docker build` places by a different rule than `up`); refs #525
(one CLI-vs-library differential per verb).
**Principle:** {doc}`../../architecture/principles` "Input rules live in the
library entry point": the CLI parses, completes, constructs, calls, renders and
translates field-named errors at one site. It holds no rule of its own.

## 1. Intent

`otto docker build` and `otto docker up` place repos and parents by two
different rules today. `build` filters repos by a coarse pin check in the CLI
(`_select_repos`) and places each repo through the private per-repo resolver
`compose._resolve_parent`, which places *every* fragment the repo declares.
`up` places the winners of the named use-case through the deployment engine
(`deployment._resolve`). So `build` can build on a host `up` never deploys to,
refuse a use-case `up` deploys, or skip a repo `up` deploys (a lab-qualified
pin is stripped to its host id in the CLI, but the engine treats it as
addressed to another lab and falls through to role placement).

`teardown` returns `None`. The full-project path discards the
`CommandResult` of `docker compose down`; the per-service path logs a failed
`stop`/`rm` at ERROR and continues. `otto docker down` therefore prints
"torn down." and exits 0 whatever happened, and a Python caller cannot find
out either.

This item gives both verbs one owner in `otto.docker` and, while the CLI
surface is open, aligns the verbs with docker's own layering so a user needs
no new rule to know which verb takes a use-case:

| otto | docker analogue | scope | `--on` |
| --- | --- | --- | --- |
| `otto docker build --on HOST [--repo NAME] [IMAGE...] [--rebuild]` | `docker build` | the selected repos' images, on one host | required |
| `otto docker ps [--on HOST]` | `docker ps` | containers per host | optional |
| `otto docker use-cases [USE_CASE]` | none | declared inventory, config only | none |
| `otto docker compose build [USE_CASE] [IMAGE...] [--on HOST] [--provide CAP=REPO]... [--rebuild]` | `docker compose build` | the images `up` would deploy, placed by the engine | optional collapse |
| `otto docker compose up [USE_CASE [SERVICE]...] [--on HOST] [--no-build] [--provide]... [--env]... [--env-file]...` | `docker compose up` | deploy a use-case | optional collapse |
| `otto docker compose down [USE_CASE [SERVICE]...] [--on HOST] [--provide]...` | `docker compose down` | tear a use-case down | optional collapse |

`build` builds images only: it stages a `[[docker.images]]` context onto a
docker-capable lab host and runs `docker build` on that host's daemon. It
never composes. What it needs is a host, not a use-case, so a bare `build`
takes `--on` and no use-case; everything use-case-scoped lives under
`compose`, where `USE_CASE` defaults the same way for all three verbs (the
only declared one; zero or several is a refusal).

Decisions (Chris, 2026-09-30): a bare `build` requires `--on` (no per-repo
placement, no "not applicable here" case); a failed teardown raises out of
`deployed()`; `build`'s `IMAGE` values are the declared `[[docker.images]]`
names, never a file or a registry name; an `IMAGE` no selected repo declares
is a refusal; the separation of `ps`'s host rule and a lint rule against
private library imports from the CLI are deferred to tracked follow-ups
(§13).

## 2. What this deletes

- `otto docker up` and `otto docker down` at the top level (moved under
  `compose`); `otto docker build USE_CASE` (moved to `compose build`).
- `otto.cli.docker._select_repos`, `_narrow_to_use_case`,
  `_resolve_parent_for_repo`, `_canonicalize_on`, and the per-image render
  loop's own failure accounting.
- `otto.docker.compose._resolve_parent` (its only caller was the CLI).
- `teardown`'s `None` return.
- The two CLI tests that patch `_select_repos` / `_resolve_parent_for_repo`
  (they pin the retired path).

No alias, no deprecation shim, no compatibility layer.

## 3. Library entry points

All in `otto.docker`, lazily exported through `_LAZY_ATTRS` + `TYPE_CHECKING`
+ `__all__` like every other name. `deploy`, `deployed`, `UseCaseStack`,
`build_images` and the per-repo primitives (`compose_up`, `compose_down`,
`composed`, `compose_ps`) keep their signatures.

```python
async def build_on(
    host: str,
    *,
    repo: str | None = None,
    images: Sequence[str] | None = None,
    rebuild: bool = False,
) -> BuildReport: ...

async def compose_build(
    use_case: str,
    *,
    on: str | None = None,
    provide: Mapping[str, str] | None = None,
    images: Sequence[str] | None = None,
    rebuild: bool = False,
) -> BuildReport: ...

async def teardown(
    use_case: str,
    *,
    services: Sequence[str] | None = None,
    on: str | None = None,
    provide: Mapping[str, str] | None = None,
    stop_timeout: int = 1,
    project_name: str | None = None,
) -> TeardownReport: ...
```

`build_on` and `compose_build` live in a new module `otto.docker.build_verbs`
(the existing `otto.docker.build` keeps `build_images` and the per-image
machinery; the verbs compose it). `teardown` stays in
`otto.docker.deployment`.

### 3a. `build_on(host, ...)`: the image-level verb

Selection: every loaded repo with a `[docker]` section, narrowed to `repo`
when given. Placement: none. Every selected repo builds on `host`.

Input rules, checked in this order, before any host is touched:

1. `host` names a host in the active lab that is a docker-capable
   `UnixHost`. Otherwise `DockerBuildError(field="host")`: "host 'x' is not a
   docker-capable unix host in lab 'unix'; docker-capable hosts here: [...]".
   `host=None` is the same class and field with its own words, "host is
   required; docker-capable hosts here: [...]" (a bare `otto docker build`
   reaches it).
2. `repo`, when given, names a loaded repo with a `[docker]` section.
   Otherwise `DockerBuildError(field="repo")` naming the docker repos.
3. Every name in `images` is declared by at least one selected repo.
   Otherwise `DockerBuildError(field="images")` naming the unknown names and
   the declared ones. Each repo then builds the subset it declares.
4. At least one selected repo declares `[[docker.images]]`. Otherwise
   `DockerBuildError(field=None)`: "nothing to build: none of [...] declares
   [[docker.images]]".

Build order: repo dependency order (`get_ordered_repos`). A repo with no
images is a `no_images` entry in the report, never a failure.

### 3b. `compose_build(use_case, ...)`: the use-case build

Placement is `_resolve(use_case, on=on, provide=provide)`, the same call
`deploy`, `teardown` and `deployed` make. Its refusals are the same
`UseCaseResolutionError` with the same words: an `on` naming no lab host, a
provider tie, an unresolvable role, a pin that can never apply. `on`
collapses every winner onto one host exactly as `deploy(on=)` does. The
displacement report (`selection.displaced`) is carried on the `BuildReport`
so the CLI prints the same notices `up` prints.

Image rules 3 and 4 of §3a apply over the winning repos. An `IMAGE` only a
displaced repo declares is therefore a refusal naming the winners' images:
the competition said not to build it.

Build order per host: the winners on that host in repo dependency order,
hosts in sorted id order. `compose_build` then `deploy` build identical
images on identical hosts; §9 pins it.

### 3c. `teardown` returns a `TeardownReport`

Behaviour is unchanged except for the return. Full teardown records the
`CommandResult` of `docker compose -p <proj> down --remove-orphans` per
host. Partial teardown (`services=[...]`) records `[stop, rm]` per host that
carried a wanted service; hosts with nothing wanted are absent from the
report. Container hosts are unregistered whether or not the command
succeeded, as today, because a stale registration is the worse outcome; the
failure is in the report instead of only in a log line. The ERROR log lines
stay.

### 3d. `deployed()` raises on a failed teardown

On exit, when it owns the stack (`own=True` or it brought the stack up), it
calls `teardown` and, if `report.ok` is false, raises
`HostCommandError` naming the use-case, the failed hosts and each failed
command's output. If the body raised, the teardown failure is logged at
ERROR and the body's exception propagates unchanged, the way `_rollback`
already behaves: a compensating action never masks the real failure.

## 4. Reports

New module `otto.docker.reports`, frozen dataclasses, lazily exported.

```python
@dataclass(frozen=True)
class RepoBuild:
    repo: str
    host: str
    kind: Literal["built", "no_images"]
    images: dict[str, CommandResult]      # per image name; empty for no_images

@dataclass(frozen=True)
class BuildReport:
    repos: list[RepoBuild]                # in build order
    displaced: list[Displacement]         # empty for build_on

    @property
    def ok(self) -> bool: ...             # no image result failed
    @property
    def failed(self) -> list[FailedImage]: ...   # (repo, image, result)

@dataclass(frozen=True)
class FailedImage:
    repo: str
    image: str
    result: CommandResult

@dataclass(frozen=True)
class HostReport:
    """One verb's outcome as the commands it ran, per host."""
    hosts: dict[str, list[CommandResult]]  # host id -> commands run there, in order

    @property
    def ok(self) -> bool: ...             # every result ok
    @property
    def failed(self) -> dict[str, list[CommandResult]]: ...

@dataclass(frozen=True)
class TeardownReport(HostReport):
    use_case: str
```

`HostReport` is the shape every "run a docker command on each host" verb
returns. `teardown` is its first user; a future `compose stop`, `compose
start`, `compose restart`, `compose pull`, `compose logs` or a library-owned
`ps` (#553) returns a `HostReport` (or a subclass adding the verb's own
fields) and needs no new report type and no new renderer (§7a).

`build_images`' per-image contract carries through: `Status.Skipped` is a
cached image whose `:latest` was re-pointed, `Status.Success` a fresh build,
anything else a failure with the captured output in `value`. `deploy` keeps
returning `UseCaseStack`; `compose_build` and `build_on` share `BuildReport`
because both are per-repo, per-host, per-image, and the host is on each
`RepoBuild`.

## 5. Errors and their CLI spelling

- `DockerBuildError(OttoError, ValueError)` in `otto.docker.build_verbs`,
  with `field: str | None`. Raised only by the input rules of §3a/§3b. The
  message is written in field terms (`host`, `repo`, `images`).
- `UseCaseResolutionError` is unchanged and is the only refusal class for
  placement, on every verb that places.
- `CommandNotRunError` is the dry-run decline (§6).

The CLI translates at one site. `_run_use_case` (the existing runner that
prints a dry-run decline and exits 0, and prints a `UseCaseResolutionError`
verbatim and exits 1) grows one arm: `DockerBuildError` goes through
`usage_error_from(exc, flags={"host": "--on", "repo": "--repo", "images":
"IMAGE"})`, so a `host` refusal reaches the user as an `--on` usage error,
exit 2, the shape `otto test` gives a bad `--cov-dir`. A missing `--on` on
`docker build` takes this path: the leaf passes `None`, the library refuses on
`host`, the CLI spells it. The option is not declared `required=True`; the
rule would then live in two places.

## 6. Dry run

`build_on` and `compose_build` validate and place first (pure), then raise
their own `CommandNotRunError` whose message is the whole plan: every host,
each repo on it in build order, each image, and for `compose_build` the
displacements. This mirrors `teardown`'s arm and replaces letting
`build_images` decline at the first repo, which would preview one repo and
hide the rest. The CLI prints the plan and exits 0. `teardown`'s and
`deployed`'s arms are unchanged. All four mutating leaves (`build`, `compose
build`, `compose up`, `compose down`) carry the dry-run preview marker.

## 7. The CLI after this item

`docker_app` gains a `compose` sub-group (a `typer.Typer` mounted with
`docker_app.add_typer`) holding `build`, `up`, `down`. The top level keeps
`build`, `ps`, `use-cases`.

Registration is one declarative table replacing today's dict plus two
policy sets:

```python
@dataclass(frozen=True)
class _Verb:
    name: str
    group: Literal["docker", "compose"]
    leaf: Callable[..., Any]
    output_dir: bool = True          # False: read-only, no per-invocation dir
    dry_run_preview: bool = False    # True: the library declines with a plan

_VERBS: list[_Verb] = [
    _Verb("build", "docker", _build, dry_run_preview=True),
    _Verb("ps", "docker", _ps, output_dir=False),
    _Verb("use-cases", "docker", _use_cases, output_dir=False),
    _Verb("build", "compose", _compose_build, dry_run_preview=True),
    _Verb("up", "compose", _compose_up, dry_run_preview=True),
    _Verb("down", "compose", _compose_down, dry_run_preview=True),
]
```

One loop registers each row into its group app and stamps the
`__cli_output_dir__` / `__cli_dry_run_preview__` markers the leaf-invoke
preamble reads. The table is the single source of truth for which verbs
exist and their policies; a unit test pins that every registered command
has a row and every row a doc page.

### 7a. Adding a docker verb

The cost of a new verb is fixed at four things, none of them a new
mechanism:

1. **One library function** in `otto.docker` that takes the verb's inputs
   as keyword arguments, validates them first (field-named
   `DockerBuildError` for leaf inputs, `UseCaseResolutionError` for
   placement), and returns a `HostReport` (per-host commands), a
   `BuildReport` (per-image), or a `UseCaseStack` (a live deployment).
   Lazily exported.
2. **One leaf** that parses, calls the function through `_run_docker`
   (§5's one translation site, the renamed `_run_use_case`) and renders
   through the renderer for its report type: `_render_host_report`,
   `_render_build_report`, `_print_stack_report`. Exit 1 when the report's
   `ok` is false, from the report.
3. **One `_Verb` row.**
4. **One doc page** under `docs/cli/docker/` or `docs/cli/docker/compose/`,
   plus a row in the index's verb table.

Anything else a new verb seems to need (a host rule, an exit-code counter,
a second error spelling) is a sign the rule belongs in the library
function, not in the leaf. The `ps` follow-up (#553) is the first exercise
of this recipe.

Each leaf is parse, call, render:

| leaf | parses | calls | renders |
| --- | --- | --- | --- |
| `docker build` | `IMAGE...`, `--on`, `--repo`, `--rebuild` | `build_on(on, repo=, images=, rebuild=)` | per image: `repo/name: cached → tag`, `built → tag`, or `FAILED` + output; `no_images` repos as a yellow notice |
| `docker compose build` | `[USE_CASE] [IMAGE...]`, `--on`, `--provide`, `--rebuild` | `compose_build(name, on=, provide=, images=, rebuild=)` | the displacement notices, then the image lines grouped by host |
| `docker compose up` | unchanged | `deploy(...)` | unchanged stack report |
| `docker compose down` | unchanged | `teardown(...)` | `_render_host_report`: per host, `host: use-case torn down` or `host: FAILED — <command>: <output>`; partial names the services |

Exit code 1 when the report's `ok` is false, read from the report. The leaf
keeps no counter. The two report renderers are generic over their report
type (`_render_build_report(report)` for any `BuildReport`,
`_render_host_report(report, *, verb: str)` for any `HostReport`), so a
future verb that returns one of them renders with no new code. `_default_use_case` stays as the one CLI-side rule, shared
by the three `compose` verbs: it defaults the CLI's optional positional, and
the library never learns the positional was omitted. `_parse_provide`,
`_parse_env`, `_print_displacements`, `_print_stack_report` and
`_run_use_case` stay as parsing and rendering helpers.

Completion: the use-case completer serves the three `compose` verbs; the
host completer serves `--on` everywhere and `ps`.

User-facing strings elsewhere in `src/otto` that name `otto docker up`,
`down` or `build` (the container auto-start "run `otto docker up` first"
error, the `otto init` scaffold text, docstrings) change to the new
spellings in the same commit as the verb move. A unit test pins that no
string under `src/otto` names `otto docker up`, `otto docker down` or `otto
docker build <use-case>` in the old form.

## 8. Worked scenarios

One repo, one use-case, one docker host:

```text
$ otto docker compose build
myapp/api: built → myapp-api:3f9c1e2
myapp/worker: cached → myapp-worker:3f9c1e2
$ otto docker build --on test3 api
myapp/api: cached → myapp-api:3f9c1e2
```

```python
report = await otto.docker.compose_build("dev")
assert report.ok
```

Bare build without a host:

```text
$ otto docker build
Usage: otto docker build [OPTIONS] [IMAGE]...
Invalid value for --on: --on is required; docker-capable hosts here: ['test3']   # exit 2
```

Multi-lab workspace (repo1 pins `unix:test3`, repo2 pins `unix_alt:alt3`,
active lab `unix`):

```text
$ otto docker build --on test3
repo1/api: built → repo1-api:9a7b...
repo2/db: built → repo2-db:c0ff...        # --on is an explicit reach, like a pin
$ otto docker compose build integration
repo1/api: built → ...                     # placed by the engine, on the host `up` uses
```

Provider competition:

```text
$ otto docker compose build integration --provide db=repo2
docker: repo1's 'db' fragment displaced by repo2 (--provide)
repo2/db: built → repo2-db:c0ffee
repo3/api: built → repo3-api:...
```

`compose up integration --provide db=repo2` deploys exactly these images to
exactly these hosts.

Unknown image name:

```text
$ otto docker build --on test3 apo
Usage: ...
Invalid value for IMAGE: no selected repo declares an image named 'apo';
declared: ['api', 'worker']                               # exit 2
```

Teardown with one host failing:

```text
$ otto docker compose down integration
test3: integration torn down
alt2: FAILED — docker compose -p unix-integration-chris down --remove-orphans --timeout 1:
error during connect: ...                                  # exit 1
```

```python
report = await otto.docker.teardown("integration")
if not report.ok:
    for host, results in report.failed.items():
        ...
```

Fixture:

```python
async with otto.docker.deployed("integration", own=True) as stack:
    await stack.hosts["api"].run("./run-tests")
# a failed teardown raises HostCommandError here; it does not vanish into a log line
```

## 9. Testing

Unit (`tests/unit/docker/`):

- `build_on`: each input rule of §3a by field, `repo` narrowing, the image
  subset per repo, `no_images` entries, build order, a failed image
  (`ok` false, `failed` names it), the dry-run plan naming every repo and
  image.
- `compose_build`: placement through `_resolve` (patched at the seam the
  deploy tests already use), `on` collapse, `provide` displacement carried
  on the report, the displaced-only image refusal, per-host grouping and
  order, the dry-run plan.
- `teardown`: full and partial, one host failing, asserting the report's
  `hosts`, `ok`, `failed`, and that hosts were still unregistered.
- `deployed()`: raises `HostCommandError` on a failed teardown; a body
  exception propagates unchanged with the teardown failure logged.
- Reports: `ok`/`failed` over every status, frozen.

The library differential, `tests/unit/docker/test_build_placement_differential.py`:
for the same use-case, `on` and `provide`, the hosts `compose_build` builds
on equal the hosts `deploy` deploys to, over a small generated set of repo
layouts (pins, roles, a displaced provider, an `on` collapse). This is the
test that turns red if the two ever get separate placement again.

CLI (`tests/unit/docker/test_cli.py`): the four leaves parse and render;
exit codes come from the report; the `DockerBuildError` spelling for each
field; the moved completers.

The CLI differential, `tests/unit/cli/test_docker_differential.py`, shaped
like `test_test_differential.py`: for each of the four leaves the CLI hands
the library exactly the parsed arguments (a capturing fake at the library
seam), and each library refusal reaches the output in flag spelling.

Old-verb strings: one test that no file under `src/otto` contains
`otto docker up`, `otto docker down`, or `otto docker build` followed by a
use-case argument.

Verb table: one test asserts every command registered on `docker_app` and
its `compose` group has a `_Verb` row, every row's leaf carries the markers
its row declares, and every row has a doc page at the path §10 gives.

Goldens and budgets: `tests/unit/api_snapshot/public_api.txt` gains
`build_on`, `compose_build`, `BuildReport`, `RepoBuild`, `FailedImage`,
`HostReport`, `TeardownReport`, `DockerBuildError`. `reports` and `build_verbs` are
lazily exported, so the `otto docker --help` import-budget row must not
move; if it does, fix the import, never the ceiling.

e2e: the docker lanes drive `otto docker up`/`down`; they move to `compose
up`/`compose down` by mechanism. They run in `make coverage` on the bed,
once, in the final task; nothing under `tests/integration` or `tests/e2e`
runs locally.

## 10. Documentation

- `docs/cli/docker/index.md`: the verb table of §1 (its one home), the
  synopsis and options table restructured by group, the toctree.
- `docs/cli/docker/build.md`: rewritten for the image-level verb (`--on`
  required, `IMAGE` = declared name).
- New `docs/cli/docker/compose/build.md`, `up.md`, `down.md` (the latter two
  moved from the top level), each naming its library function.
- `use-cases.md`, `rebuild-policy.md`, `ps.md`: verb spellings.
- `docs/api/docker/reports.rst`, `build_verbs.rst`; `deployment.rst`
  documents the report and the `deployed()` raise.
- `docs/cookbook/python-library.md`: a docker section (build a use-case's
  images, deploy, read a teardown report, the fixture and what it raises).
- Getting-started, README, the settings guide, the docker subsystem map
  (placement has one owner): verb spellings.
- The termynal `help-docker` capture regenerated.

Per the one-home rule, other pages link to the verb table rather than
restating it.

## 11. Gates, boundaries, budgets

`tach` edges: `otto.cli` → `otto.docker` already exists; `otto.docker` never
imports `otto.cli` (unchanged, enforced). lint-arch: no plan coordinates.
Per-task gates are targeted (the touched tests + `ruff` on changed files);
`make coverage`, `nox -s tests_hostless-3.14`, `make typecheck` and `make
gate-fresh` run once, in the final task, with bed coordination first.

## 12. Migration

Breaking, one squash, `feat(docker)!`:

- `otto docker up` → `otto docker compose up`; `otto docker down` → `otto
  docker compose down`.
- `otto docker build USE_CASE` → `otto docker compose build USE_CASE`.
- `otto docker build` (bare) now requires `--on HOST`.
- `teardown` returns a `TeardownReport`; `deployed()` raises
  `HostCommandError` on a failed teardown.
- `otto.docker.compose._resolve_parent` is gone (it was private).

## 13. Out of scope (tracked)

- `ps`'s `--on` validation and host enumeration stay in the CLI; #553 moves
  them to a public `docker_parents(on=None)` in the library.
- An ast-grep rule banning `_`-prefixed imports from outside `otto.cli` in
  the CLI package goes with #508 (item 7), where the four existing sites
  (`_apply_option_overrides` ×2, `_refresh_tables` ×2) are cleaned up first.
- `deploy`'s surface, the per-repo primitives, and the `use-cases`
  inventory verb are unchanged.
