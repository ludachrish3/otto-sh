# `otto init`: sample plumbing, full settings.toml, automatic schemas

**Date:** 2026-07-17
**Status:** Draft — awaiting approval

## Goal

`otto init` should leave a new repo with working, imitable plumbing for the
three things every otto user builds — instructions, suites, and options — plus
a settings.toml that shows the entire configuration surface, and editor
schemas wired up automatically. The samples themselves stay trivial (one log
statement each); their value is the wiring, not the content.

## Non-goals

- No new sample functionality beyond the plumbing (no host-exec, impairment,
  monitor, etc. samples — deliberately kept out per design discussion).
- No change to `otto schema export` itself beyond one default: its `--out`
  moves from `schemas` to `.otto/schemas` so both tools share one location
  (the doctor's "re-run `otto schema export`" remedy must refresh the files
  the doctor checks). `--out` still accepts any path.
- No merging into existing editor config: `.vscode/*` files are written
  only-if-absent, never edited (VS Code settings are JSONC — comments,
  trailing commas — so programmatic merge is a corruption risk).
- No `.taplo.toml` (settings.toml is the only otto-owned TOML and it carries
  `#:schema` in-file) and no `.editorconfig` (indentation policy, unrelated
  to schemas and not otto's place). Neovim wiring cannot ride in repo files;
  it remains a docs section.
- No full `--name` slug validation (quotes/spaces UX); only module-name
  sanitization (below).

## 1. Areas & CLI surface

`AREAS` grows from four to five, in doctor-table order: settings, schemas,
lab, tests, instructions.

- `init_command` gains a `--schemas` flag mirroring `--lab`/`--tests`/
  `--instructions`; interactive mode prompts "Scaffold the schemas area?"
  (default yes) like the others; `--all` includes it.
- **Refresh semantics (schemas only):** the explicit `--schemas` flag also
  re-generates a *detected* schemas area. Generated `*.schema.json` files are
  otto-owned, so overwriting is safe — and without this, the staleness
  doctor's "re-run `otto init --schemas`" remedy would be a no-op (areas
  otherwise scaffold only when missing). `--all` and interactive keep
  missing-only semantics; user-owned areas (lab/tests/instructions) are never
  refreshed.
- Templates move from `src/otto/cli/init.py` (614 lines) into a new sibling
  `src/otto/cli/init_templates.py` holding string constants only; all logic
  stays in `init.py`.
- **Module-name sanitization** (targeted fix for a known follow-up that now
  bites twice): names derived from `--name`/dirname are passed through
  `re.sub(r"\W", "_", name)` before forming the `<name>_instructions` and
  `<name>_options` module names, so a repo dir like `my-repo` produces
  importable modules (`my_repo_instructions`). The settings `name` field keeps
  the unsanitized value; only module names are sanitized. Detection is
  unaffected: it reads module names back from settings.toml's `init` list.

## 2. settings.toml — full field surface

### Template content

Line 1 is taplo's schema directive (a plain TOML comment; editors that
understand it get autocomplete, others ignore it):

```toml
#:schema ./schemas/settings.schema.json
```

(The path is relative to `.otto/settings.toml`; schemas live in
`.otto/schemas/`, below.)

Then, **active** (uncommented): `name`, `version`, `labs`, `tests`, `libs`,
`init = ["<name>_instructions"]` — as today. The options module is
deliberately NOT listed in `init`: it needs no startup import (suites and
instructions import it directly), and listing it would falsely trip
instructions-area detection (`_detect_instructions` returns true when any
`init` module resolves).

Then every optional field, **commented out**, grouped by section with prose
guidance:

- top level: `valid_labs`
- `[lab]`: `backend` (+ a pointer for backend-specific `[lab.<backend>]`)
- `[logging]`: `capture`
- `[host_preferences."<selector>"]`: `term`, `transfer`, `impairer` lists;
  the six per-protocol option tables (`ssh_options`, `telnet_options`,
  `sftp_options`, `scp_options`, `ftp_options`, `nc_options`) are named with a
  pointer to `.otto/schemas/settings.schema.json` / the docs — their ~50
  tuning knobs are not enumerated
- `[os_profiles.<name>]`: `base` + free-form field defaults (one worked
  example)
- `[reservations]`: `backend`, `url` (+ backend sub-table pointer)
- `[coverage]`: `hosts`, `gcda_remote_dir`, one worked
  `[coverage.tiers.<name>]` example (`kind`, `precedence`, `color`,
  `harvest_dirs`, `max_age`), `[coverage.exclusions]` `markers`, and an
  `embedded` passthrough pointer
- `[docker]`: `registry_url`, one worked `[[docker.images]]` example (`name`,
  `dockerfile`, `context`, `target`, `build_args`), one worked
  `[[docker.composes]]` example (`path`, `default_host`, `services`)

Legacy `lab_data_type` is omitted (legacy passthrough, consumed by nobody).

### Comment convention (load-bearing)

sshd_config-style, so tests can distinguish commented-out TOML from prose
mechanically:

- commented-out TOML: `#key = value` / `#[section]` — `#` immediately
  followed by a non-space character
- prose commentary: `# text` — `#` followed by a space
- the `#:schema` directive is excluded from uncommenting by its `#:` prefix

### Drift guards (two unit tests)

1. **Uncomment-and-validate:** strip the leading `#` from every
   commented-out-TOML line (per the convention above), parse with tomli, and
   `SettingsModel.model_validate` the result. `extra='forbid'` turns any
   template typo into a red test.
2. **Completeness:** collect the set of top-level keys and section names the
   template mentions (active + commented) and diff against
   `SettingsModel.model_fields`, with an explicit allowlist of intentional
   omissions (`{"lab_data_type"}`). A future settings field cannot land
   without the template learning it.

## 3. Sample plumbing — options → suite → instruction

Each layer adds exactly one field, so a reader sees inheritance and extension
in the smallest possible example. Every sample body is one log statement.

### `pylib/<name>_options.py` (new, shared)

```python
@options
class RepoOptions:
    message: Annotated[str, typer.Option(help="...")] = "hello from <name>"
```

Created by a shared idempotent helper `_ensure_options_module(root, cfg)`
that BOTH the tests and instructions scaffolds call: creates the file only if
missing (never overwrites), returns the path when created so the scaffold
echoes it. Either area alone therefore works; scaffolding the second area
later reuses the existing module.

### `tests/test_example.py` (rewritten)

- `from <name>_options import RepoOptions` (libs are on sys.path under
  `otto test`)
- `@options class _Options(RepoOptions)` adds `greeting`
- `TestExample(OttoSuite[_Options])` with one test that logs
  `suite_options.message` via `self.logger` — hostless, passes out of the
  box. The test keeps the `repo_marker` fixture parameter (one trivial
  assert) so the conftest sample stays consumed, not dead code
- keeps the one-line plain `test_example_function` (the epilogue's
  `--tests` step exercises pytest-native collection)
- `tests/conftest.py` unchanged (the `repo_marker` fixture remains the
  fixture-plumbing sample; commented live-host fixture block stays)

### `pylib/<name>_instructions/__init__.py` (rewritten)

- `@options class _Options(RepoOptions)` adds a `loud` flag
- `@instruction(options=_Options) async def smoke(opts)` logs `opts.message`
  (uppercased when `--loud`)

Net effect, provable from `--help`: `--message` appears on BOTH
`otto test TestExample` and `otto run smoke`; each also has its own flag.

### Epilogue

Next-steps list gains `otto run smoke` (after the two `otto test` steps).

## 4. Schemas area + self-wiring

### Area behavior

- **Scaffold:** call the same `build_schemas()` backing `otto schema export`;
  write `.otto/schemas/*.schema.json` — inside the otto-owned marker
  directory, hidden away rather than claiming a visible root-level dir.
  `otto schema export`'s default `--out` moves to the same path (non-goals
  note). No path knob on init.
- **Detect:** any `*.schema.json` under `.otto/schemas/`.
- **Validate (staleness doctor):** regenerate in-memory and compare
  structurally (parsed JSON equality, not bytes — a reformatted file is not
  stale) against the on-disk `*.schema.json` set. Missing, mismatched, and
  orphaned files each produce a doctor problem naming both remedies:
  "stale — re-run `otto init --schemas` or `otto schema export`". This
  mechanizes editor-schemas.md's "remember to regenerate after upgrading"
  note. Files not matching `*.schema.json` are ignored.
- Caveat (accepted, same as `otto schema export`): schemas reflect the custom
  host classes registered in the environment where init runs.

### Self-wiring

- **settings.toml** → the `#:schema ./schemas/settings.schema.json`
  directive (section 2). Path is relative to `.otto/settings.toml`.
- **lab.json** → the scaffolded template gains a top-level
  `"$schema": "../.otto/schemas/lab.schema.json"` (relative to
  `lab_data/lab.json`), giving VS Code's JSON language server autocomplete
  with no `.vscode` edits.

### Editor wiring (workspace config)

There is no directory editors auto-scan for schemas — pickup always comes
from in-file pointers (`$schema` / `#:schema`, above), workspace config, or
the public schemastore catalog (published schemas only). Location is
therefore purely a path convention, and `.otto/schemas/` keeps the generated
files inside the otto-owned marker dir instead of claiming visible root
space. The schemas-area scaffold additionally writes workspace config,
**only when the file does not already exist**:

- `.vscode/settings.json` — `json.schemas` associations for `**/lab.json`
  and `**/reservations.json` pointing into `./.otto/schemas/` (covers files
  created later without an in-file `$schema`, e.g. a reservations file,
  which init does not scaffold), plus the Even Better TOML association for
  `settings.toml` (redundant with `#:schema`, harmless belt-and-braces).
- `.vscode/extensions.json` — `"recommendations":
  ["tamasfe.even-better-toml"]` (a suggestion prompt, never forced).

If `.vscode/settings.json` already exists it is left untouched and the
scaffold echoes a one-line pointer to editor-schemas.md instead. These files
are scaffold-only and **never doctor-validated** — once created they are
user-owned editor config, and staleness-flagging a customized settings.json
would be noise. A repo whose schemas area already exists can still gain
missing `.vscode` files via `otto init --schemas` (the refresh semantics in
section 1 re-run the scaffold, and the wiring step is only-if-absent); bare
`otto init` / `--all` skip a detected area as usual.

### Two small core changes (required for self-wiring)

1. **`parse_lab_sections` tolerates `$schema`:** the top-level unknown-key
   check treats `$schema` like `_`-prefixed comment space (one line). Runtime
   loader and init doctor share this helper, so both accept it automatically.
2. **Generated schemas allow what the runtime tolerates:** the emitted
   lab-related schemas currently `additionalProperties: false`-reject the
   `_comment` keys the runtime strips — a previously-noted follow-up that
   self-wiring makes user-visible (the editor would squiggle the very file we
   scaffold). `models/jsonschema.py` will emit an explicit `$schema` string
   property (top level of `lab.schema.json`) and `^_` patternProperties
   allowances exactly where the runtime tolerates `_` keys (lab top level,
   host entries; link entries only if `LinkSpec` tolerates them — verify at
   implementation and match the runtime precisely).

## 5. Docs

- `docs/guide/setup/repo-setup.md`: five areas; refreshed settings.toml
  example (or reference to the scaffolded template); schemas emitted
  automatically by init.
- `docs/guide/setup/editor-schemas.md`: init now exports the schemas,
  self-wires the scaffolded files, and writes the `.vscode` config for you;
  manual instructions retained for existing repos and pre-existing
  `.vscode/settings.json` files; every `schemas/` path in the page (VS Code,
  Neovim, taplo snippets and the `--out` example) becomes `.otto/schemas/`;
  drift note updated to mention the staleness doctor.
- Termynal `help-init.html` regenerated (picks up `--schemas`).
- Docs gate: clean rebuild (incremental `-W` misses docstring `:doc:` refs).

## 6. Testing

Unit (`tests/unit/cli/test_init_*`, plus labs/model tests):

- the two settings-template drift tests (section 2)
- schemas area: detect/scaffold; staleness doctor red on a mutated schema
  file, on a missing file, and on an orphaned extra `*.schema.json`; green on
  reformatted-but-equal JSON
- `parse_lab_sections` accepts top-level `$schema`; still rejects other
  unknown keys
- generated lab schema validates the scaffolded lab.json (including
  `$schema` + `_comment`) — e.g. via jsonschema-lib check or structural
  assertion on the emitted schema
- `_ensure_options_module` idempotence: tests-then-instructions and
  instructions-then-tests orders both yield one module, never overwritten
- editor wiring: `.vscode/settings.json` + `extensions.json` created when
  absent; a pre-existing `settings.json` is byte-for-byte untouched (and the
  pointer line is echoed); doctor output never mentions `.vscode`
- module-name sanitization: `--name my-repo` scaffolds importable modules

E2E (extend the existing init→list→test→run flow):

- fresh `otto init --all` passes its own doctor (exit 0, schemas row green)
- `otto test TestExample --message hi` and `otto run smoke --message hi`
  both accept the inherited flag (plumbing proven end-to-end);
  `otto run smoke --loud` proves the instruction's own flag

Gate: `make coverage`.

## Decisions log (from design discussion)

- Samples stay trivial; plumbing is the deliverable. (Chris)
- Repo-wide options: shared pylib module, scaffolded on demand — no 5th
  options area. (Chris)
- settings.toml depth: everything except the six per-protocol option tables'
  leaf fields. (Chris)
- Templates hand-authored + drift tests; no schema-driven generation; no
  reuse of `otto.examples.options`. (Chris)
- Schemas: new 5th area with staleness doctor. (Chris)
- Self-wiring including the `$schema` tolerance + jsonschema allowance core
  changes. (Chris)
- Editor wiring: `.vscode/settings.json` + `extensions.json`, only-if-absent,
  never validated; no `.taplo.toml`, no `.editorconfig`. (Chris)
- Schemas live in `.otto/schemas/`, hidden inside the otto marker dir — no
  editor auto-scans any directory, so location is purely a path convention;
  `otto schema export`'s default `--out` moves to match. (Chris)
- `--schemas` refreshes a detected schemas area (found during planning: the
  doctor remedy requires it; generated files are otto-owned so overwrite is
  safe). (planning refinement)
