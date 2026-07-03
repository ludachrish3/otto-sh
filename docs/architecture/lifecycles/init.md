# otto init — scaffold and doctor

`otto init` bootstraps a repo into an otto project — and doubles as a
*doctor* for one that already exists. It is `lab_free`, creates no output
directory, and runs no gate: it operates purely on files under the target
path.

## Areas, not a monolith

The command is organized around four **areas** — `settings`
(`.otto/settings.toml`), `lab` (`lab_data/hosts.json`), `tests`, and
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
({doc}`../subsystems/data-boundary`). `otto init` cannot drift from what
otto actually accepts, because there is no second validator to drift. A repo
that passes `otto init` loads.

## What the scaffold teaches

The generated files are deliberately didactic: the sample suite is a
`Test`-prefixed {class}`~otto.suite.suite.OttoSuite` subclass (demonstrating
auto-registration — {doc}`test`), alongside a plain pytest function runnable
via `otto test --tests`, and the `hosts.json` template uses the sanctioned
`_`-prefixed comment keys to explain itself in place.
