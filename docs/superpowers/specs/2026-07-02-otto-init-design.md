# `otto init` — scaffold + validate a repo's otto setup

**Date:** 2026-07-02
**Status:** Draft for review
**Scope:** a new top-level `otto init` command that scaffolds a working otto repo from nothing and
validates an existing one. Companion spec: `2026-07-02-pytest-native-flexibility-design.md` —
**lands first**; the suite template here is decorator-less and relies on auto-registration.

Identified as "the biggest remaining learning-curve lever" in
`todo/fable_review_verification.md` (review gap 1).

---

## 1. Motivation

Standing up an otto repo today is six manual steps (`docs/getting-started.md`): create
`.otto/settings.toml`, set `OTTO_SUT_DIRS`, create a lab dir with `hosts.json`, write a suite,
optionally wire `pylib/` instructions, install completion. Each step has a distinct failure mode a
newcomer discovers one at a time. `otto init` collapses this into one command that is also safely
re-runnable as a setup doctor.

**Out of scope:** initializing a git repo (one is assumed to exist), editing `.gitignore`,
modifying shell rc files, prompting for host details.

## 2. Command surface

New `src/otto/cli/init.py`, registered in `builtin_commands.py` via
`register_cli_command("init", ..., lab_free=True, gate=False, output_dir=False)` — it must run
with zero repos discovered (before `OTTO_SUT_DIRS` exists).

```text
otto init [--all] [--lab] [--tests] [--instructions]
          [--name NAME] [--version X.Y.Z] [--path DIR]
```

- **No area flags → interactive:** one yes/no prompt per **missing** area, in order
  (settings → lab → tests → instructions). Interactive mode also prompts for `--name`
  (default: directory name) and `--version` (default: `0.1.0`) when scaffolding settings.
- **`--all`:** scaffold every missing area, no prompts.
- **Specific area flags:** scaffold exactly those areas, no prompts.
- **Settings is the repo marker and a prerequisite:** whenever it is missing it is scaffolded
  regardless of which areas were requested, with a printed note.
- **`--path`** defaults to CWD.

## 3. Areas: detect / validate / scaffold

Each area is a small dataclass (`name`, `detect()`, `validate()`, `scaffold()`) driven by one
loop. **Existing areas are validated and reported, never modified.** Validation delegates to the
real ingestion code so `init` can never disagree with what otto accepts:

| Area | detect | validate (existing) | scaffold (missing) |
| --- | --- | --- | --- |
| settings | `.otto/settings.toml` | parse via the same `SettingsModel`/`Repo` path bootstrap uses | template §4.1 |
| lab | any `hosts.json` under the `labs` dirs | pydantic `HostSpec` ingest of every hosts file | template §4.2 |
| tests | any `test_*.py` under the `tests` dirs | the existing one-shot collection pass; report suite/test counts | templates §4.3 |
| instructions | `libs`/`init` entries resolve | paths exist and modules import (errors caught and reported) | template §4.4 |

Output: a ✓/✗ table per area with pointers (file:line where applicable). Exit code non-zero when
any validation failed (CI-friendly); scaffolding alone exits zero.

## 4. Templates (inline string constants in `cli/init.py`)

All templates conform to the companion spec's end state.

### 4.1 `.otto/settings.toml`

`name`/`version` filled from prompts/flags. The conventional paths are **pre-wired up front** —
`labs = ["lab_data"]`, `tests = ["tests"]`, `libs = ["pylib"]`, `init = ["<name>_instructions"]` —
so a later `otto init --lab` never needs to edit settings (which "never modify" forbids).
Commented breadcrumbs for the optional sections (`[lab]`, `[host_preferences]`, `[os_profiles]`,
`[reservations]`, `[coverage]`, `[docker]`). The `tests` key is documented in-file as *"defines
where test discovery happens"*.

Implementation must verify bootstrap tolerates listed dirs that don't exist yet; if it errors,
scaffolding settings also creates the empty conventional dirs.

### 4.2 `lab_data/hosts.json` + `lab_data/README.md`

One **valid** example unix host with unmistakably-placeholder values:

```json
[
    {
        "_comment": "Example host — replace these values. Full host schema: docs/guide/host-database.md or `otto schema export`. The `labs` list names the labs this host belongs to (select with --lab).",
        "ip": "192.0.2.1",
        "element": "example-device",
        "os_type": "unix",
        "valid_terms": ["ssh"],
        "valid_transfers": ["scp", "sftp"],
        "creds": { "admin": "CHANGE_ME" },
        "resources": ["example-device"],
        "labs": ["example_lab"]
    }
]
```

- Valid against schema v5, so `otto --lab example_lab --list-hosts` works immediately (TEST-NET-1
  IP; nothing can accidentally reach a real device).
- `resources` included because real host entries commonly carry it (fixture convention: the
  element's own name).
- **`_comment` requires a one-line schema carve-out:** a `mode="before"` validator on `HostSpec`
  (only — `OttoModel`'s `extra="forbid"` stays for everything else) strips keys starting with `_`
  before validation. The standard JSON comment idiom; typo protection is not weakened (a leading
  underscore cannot be typed by accident). Applies uniformly to every ingest path since all funnel
  through the spec models. Documented in `docs/guide/host-database.md`.
- `lab_data/README.md`: field-by-field walkthrough of the example, pointing at the host-database
  guide, lab selection (`--lab`, `OTTO_LAB`), and `otto schema export`. (JSON cannot carry longer
  prose; the README is the "what next" document.)

### 4.3 `tests/test_example.py` + `tests/conftest.py`

- `TestExample(OttoSuite[_Options])` with a small defaulted `Options` and one hostless, passing
  async test — **no decorator** (auto-registration). Plus one plain pytest function,
  `test_example_function`, demonstrating the suite-less `otto test --tests` path.
- `tests/conftest.py`: one trivial working fixture the example test uses, plus a commented
  `get_host(...)`-based fixture example — demonstrating the repo-wide-fixtures pattern the
  companion spec enables.

### 4.4 `pylib/<name>_instructions/__init__.py`

One sample `@instruction()` function, matching the in-tree `repoN_instructions` shape.

## 5. Epilogue (printed after every run)

Next steps, each skipped when already satisfied:

1. `export OTTO_SUT_DIRS=<abs path>` (skipped when the env var already contains the path).
2. `otto --install-completion`.
3. Verification: `otto --lab example_lab --list-hosts`, `otto test --list-suites`,
   `otto test TestExample`, `otto test --tests test_example_function`.

## 6. Error handling

- Scaffold never overwrites: an area detected as present is never written, even partially
  (e.g. `lab_data/` exists but is empty → hosts.json is still written only after an explicit
  detect-as-missing; detection is per-artifact, not per-directory).
- Validation failures are reported with the underlying pydantic/collection error text and the
  offending file — not re-worded, not swallowed.
- `--path` pointing at a non-directory or unwritable location: clean error, no partial writes.

## 7. Testing

1. **Per-area unit tests** scaffolding into `tmp_path` (never the dev repo): each template written
   byte-stable, settings parses via `SettingsModel`, hosts.json passes `HostSpec` ingest, the
   example suite auto-registers, the instructions module imports.
2. **Validation tests** against broken fixtures: typo'd settings key, invalid host field, suite
   file with a syntax error, missing `libs` dir — each yields ✗ with the real error and non-zero
   exit.
3. **Idempotency:** run `otto init --all` twice — second run reports all ✓ and writes nothing
   (mtime assertion).
4. **e2e (hostless subprocess infra):** `otto init --all` in a temp repo, then with
   `OTTO_SUT_DIRS` set: `otto test --list-suites` shows `TestExample`; `otto test TestExample`
   passes; `otto --lab example_lab --list-hosts` lists the example host.
5. **Prompt paths:** interactive per-area confirm honored (typer CliRunner input); `--lab` alone
   scaffolds lab + prerequisite settings with the note.

Gates: `make coverage` per task; full gate before hand-off; typecheck round budgeted after src
edits.

## 8. Docs

`docs/getting-started.md` rewritten around `otto init` (manual steps become the "what init did for
you" explanation); `docs/guide/repo-setup.md` team checklist updated; `docs/guide/host-database.md`
gains the `_`-prefixed-keys idiom; `docs/guide/cli-reference.md` gains the command.

## 9. Delivery

- Stage-only; **no self-commit**. Paste-able commit messages on completion.
- Lands **after** the companion spec's implementation (decorator-less template).
- Suggested commits: (1) `HostSpec` underscore-key carve-out + tests; (2) `otto init` command +
  templates + unit tests; (3) e2e + docs rewrite.
