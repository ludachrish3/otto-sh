# Bootstrap and multi-project design

otto composes one running process out of potentially many repos. Each repo
directory named in `OTTO_SUT_DIRS` contributes independently — its own
`.otto/settings.toml`, its own `libs` joining `sys.path`, its own `init`
modules and test files. Bootstrap's two phases discover every repo and import
its init modules; the test files load later, only for the commands that read
suites ({doc}`../lifecycle` walks the shared path). Either way, instructions,
suites, and host classes from every repo land in the same flat registries,
indistinguishable to the CLI or to `all_hosts()`. See
{doc}`../../configuration/settings` for the settings a repo contributes and
what happens at startup from a user's point of view. `otto init`
(`otto.cli.init`) works one repo at a time: it gets a single repo into
the shape bootstrap expects to compose.

## Areas, not a monolith

The command is organized around five **areas** — `settings`
(`.otto/settings.toml`), `schemas` (`.otto/schemas/`), `lab`
(`lab_data/lab.json`), `tests`, and `instructions` — each a small value
object with three operations:

- `detect` — does this area already exist here?
- `validate` — is what exists actually loadable?
- `scaffold` — write a minimal, working starting point.

Interactively it walks the areas and prompts; `--all` or per-area flags
(`--schemas`, `--lab`, `--tests`, `--instructions`) run non-interactively.
Existing files are never mutated — except the otto-owned schemas area,
which `otto init --schemas` refreshes — an area that exists is validated,
not overwritten — and the run ends with a status table plus a "next steps"
list, exiting `1` if any validation failed.

## The doctor is the ingest code

The architecturally important choice: validation reuses the **same boundary
models bootstrap uses** — settings validate through the settings spec model,
host entries through the same validator lab loading uses
({doc}`data-boundary`). `otto init` cannot drift from what otto actually
accepts, because there is no second validator to drift. A repo that passes
`otto init` loads.

## Project activation

Composing every repo into one process has a cost: before activation
({doc}`../../cli/projects`), every repo named in `OTTO_SUT_DIRS` was equally
present in every invocation. A colleague's half-finished repo with an
unimportable `init` module took down `otto host dut1 exec uptime` for
everybody, and the only cure was to edit the environment variable. Activation
makes "which projects is this run about?" a question otto answers per
invocation, from the labs the user already had to name, so a repo that is not
part of the run cannot fail it by being broken. A repo that declares no
`[project]` table stays always active, which keeps a single-repo workspace
exactly as it was and makes activation opt-in for everyone else.

The reporting and enforcement rules each follow from what the user can
already see and what they asked for:

- **An unknown `-I`/`-E` name is a usage error**, not a no-op: the failure it
  prevents is a run the user believed was narrowed and was not.
- **Only `-E` gets a warning line in a fleet walk.** Every reason a lab
  verdict can leave a repo out is already printed beside it, but nothing in
  that report records that the user typed `-E` at all.
- **The import-failure gate demotes on the lab axis only**, because it runs
  before the lab is built and its job is to protect lab construction from a
  half-registered world. A repo forced active with `-I` stays fatal when
  broken: a run cannot be partly about a repo that did not load.
- **Only build-up walks refuse over a dependency the labs dropped.** A
  cleanup that dies rather than removing what it can leaves the lab dirtier
  than it found it, and a report that dies whole is worse than a report with
  a row in it.
- **Help and completion list every instruction whatever the lab.** Discovery
  and dispatch answer different questions; a list that changed shape with the
  lab would leave a user unable to find out that an instruction exists at
  all.

`[project] lab_patterns` has no default, and a table without it applies to no
lab: every-lab is spelled `[".*"]`, out loud, so match-all is a visible choice
and never a default that quietly widens a project's reach. Explicit targeting
(`otto host <id>`, `get_host("id")`) is not bounded by the declaration, because
a repo naming a jump host it does not own must still be able to reach it, and a
scoping typo must never brick the one command that could diagnose it.

## The orchestration environment

One process means one interpreter, so the repos' glue code has to be
satisfiable from a single environment; `otto env` ({doc}`../../cli/env/index`)
builds it. Its choices:

- **otto installs itself the way it is running** — the same editable
  checkout, or the same wheel version. A pipx-global otto would otherwise
  import against the wrong site-packages, and the environment would be
  decoration.
- **The dependency preflight runs after the lab loads**, so it can ask the
  real activation question. A pre-lab approximation could not tell that a repo
  whose `host_patterns` match no host is inactive, and would refuse the run
  instead of warning. It is metadata only — no network, no imports — which is
  what makes it affordable on every invocation.
- **The command line outranks the settings file.** `--backend` beats a repo's
  `[env] backend`, because the operator at the terminal knows things the file
  does not — that uv is not installed on this particular host, say.
- **Backend choices are never silently changed.** An explicit backend that
  cannot be honoured is refused rather than downgraded, since the user asked
  for it precisely to avoid the fallback; two repos declaring different
  backends is an error, since picking one would bind an installer nobody
  chose; and the recorded backend lives *inside* the venv, so `create --force`
  cannot inherit the backend it was run to escape.
- **The verbs otto's own messages name stay safe to follow.** `sync` never
  removes anything and builds a missing environment rather than refusing — a
  refusal would answer "your environment is out of date" with a second error —
  and `show` never fails on a broken environment, because a diagnostic that
  fails when things are broken is the one the user needed most.
- **Resolver failures are passed through, not guessed at.** otto adds a line
  naming the colliding repos only when it can attribute the failure; a guess
  would send the user to edit the wrong `pyproject.toml`.

## Where the code lives

- {mod}`otto.bootstrap` — the two-phase composition root: discovery (env +
  every repo's `settings.toml`) and contained registration (each repo's
  `libs` and `init` modules), plus {func}`~otto.bootstrap.load_test_suites`,
  the suites registry's loader, which imports the test files on demand with
  the same per-file containment. Phase 1 is
  {func}`~otto.bootstrap.discover`, and its
  {class}`~otto.bootstrap.DiscoveryResult` carries three fields — `env`,
  `repos`, and the `errors` for repos whose settings would not parse — which
  phase 2 folds into the {class}`~otto.bootstrap.BootstrapResult` alongside
  its own. {func}`~otto.bootstrap.invalidate` drops every cached result and
  is the supported recovery path for a long-lived embedder: fix the repo or
  the environment, invalidate, bootstrap again. That is also why the errors
  ride the cached result rather than a parallel module global — recomputing
  discovery necessarily recomputes them, so a stale error cannot outlive the
  discovery that produced it
- `otto.cli.init` — the `otto init` areas (settings, schemas, lab, tests,
  instructions): detect / validate / scaffold, reusing bootstrap's own
  ingestion code
