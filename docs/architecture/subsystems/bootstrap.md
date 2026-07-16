# Bootstrap and multi-project design

otto composes one running process out of potentially many repos. Each repo
directory named in `OTTO_SUT_DIRS` contributes independently — its own
`.otto/settings.toml`, its own `libs` joining `sys.path`, its own `init`
modules and test files — and bootstrap's two phases discover and register
all of them together ({doc}`../lifecycle` walks the shared path), so
instructions, suites, and host classes from every repo land in the same flat
registries, indistinguishable to the CLI or to `all_hosts()`. See
{doc}`../../guide/setup/repo-setup` for the settings a repo contributes and
what happens at startup from a user's point of view. `otto init`
(`otto.cli.init`) works one repo at a time: it gets a single repo into
the shape bootstrap expects to compose.

## Areas, not a monolith

The command is organized around four **areas** — `settings`
(`.otto/settings.toml`), `lab` (`lab_data/lab.json`), `tests`, and
`instructions` — each a small value object with three operations:

- `detect` — does this area already exist here?
- `validate` — is what exists actually loadable?
- `scaffold` — write a minimal, working starting point.

Interactively it walks the areas and prompts; `--all` or per-area flags
(`--lab`, `--tests`, `--instructions`) run non-interactively. Existing files
are never mutated — an area that exists is validated, not overwritten — and
the run ends with a status table plus a "next steps" list, exiting `1` if
any validation failed.

## The doctor is the ingest code

The architecturally important choice: validation reuses the **same boundary
models bootstrap uses** — settings validate through the settings spec model,
host entries through the same validator lab loading uses
({doc}`data-boundary`). `otto init` cannot drift from what otto actually
accepts, because there is no second validator to drift. A repo that passes
`otto init` loads.

## Where the code lives

- {mod}`otto.bootstrap` — the two-phase composition root: discovery (env +
  every repo's `settings.toml`) and contained registration (each repo's
  `libs`, `init` modules, and test files)
- `otto.cli.init` — the `otto init` areas (settings, lab, tests,
  instructions): detect / validate / scaffold, reusing bootstrap's own
  ingestion code
