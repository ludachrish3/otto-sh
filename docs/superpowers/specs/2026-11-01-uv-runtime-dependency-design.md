# uv as otto's runtime dependency (#555 step 0)

**Status:** reviewed (Astra and Fable, two rounds each); awaiting the maintainer.
**Issue:** #555. The approved addendum is
[comment 6568396062](https://github.com/ludachrish3/otto-sh/issues/555#issuecomment-6568396062).
**Date:** 2026-11-01.

## Intent

#555 redesigns `otto env` on the assumption that uv is always available. This
step makes that a guarantee: otto declares uv as a runtime dependency and runs the
uv installed with it. It brings two things:

- **One installer path for `otto env`.** The pip fallback, and choosing between
  backends, go away.
- **uv compatibility becomes a tested, locked, Dependabot-watched dependency
  range.** Until now, prose in the installation page stated the version.

pip stays only as a way to bootstrap: `pip install otto-sh` brings uv with it.

## Decisions (maintainer, 2026-11-01)

- **Range:** `uv>=0.11,<0.14`.
  - The floor matches the outer-uv floor documented in `docs/installation.md`.
  - The cap is the next minor, because uv makes breaking changes in minor releases
    while it is 0.x.
  - The precedent is the build backend's `uv_build>=0.10.0,<0.14.0`.
- **Raising the cap:** a Dependabot PR on each uv minor, whose CI checks
  compatibility. Dependabot's `uv` ecosystem has already widened a capped
  `build-system` requirement this way: `9eeee473a`, "update uv-build requirement
  from <0.13.0 to <0.14.0". Widening a `[project].dependencies` cap is expected to
  work the same way. Verify it on the first uv 0.14 PR.
- **A floor lane in CI:** it proves otto's uv use works at the floor.

## Correction to the addendum: no `required-version` guard

The addendum proposed `[tool.uv] required-version = ">=0.11"` in the user's
`pyproject.toml`, so that an older uv on PATH would refuse to run. Tested
2026-11-01 with `uv lock` on a `[tool.uv]` table holding `required-version`,
`package`, `environments` and a TLS key:

| uv | `system-certs = true` | `native-tls = true` |
|---|---|---|
| 0.9.0 | warns `Failed to parse pyproject.toml during settings discovery … unknown field system-certs`, ignores the whole table, exits 0 | refuses: `Required uv version >=0.11 does not match the running version 0.9.0`, exits 2 |
| 0.10.0 | same as 0.9.0 | same as 0.9.0 |
| 0.11.0 | resolves | resolves |
| 0.13.6 | resolves | resolves, but prints on every command: `The native-tls setting is deprecated and will be removed in a future release. Use system-certs instead.` |

The guard works only when every key in the table is one the older uv knows.
`system-certs`, the key the documented file uses, first appears in 0.11, the floor
itself. So in that file the guard is thrown away along with the rest of the table,
on exactly the versions it was meant to stop.

Spelling TLS as `native-tls` makes the guard work today, but it costs every
current-uv user a deprecation warning on every command. When uv removes that key,
the whole table also breaks on new uv. Both reviewers rejected that trade.

**Ruling:**

- Keep `system-certs`. Add no guard.
- The docs quote the old-uv warning verbatim and say what it can lead to.
- The docs make installing the outer uv from the same wheel set the main route.
- At the next floor raise, re-evaluate `required-version`. If every key in the
  documented table predates the new floor, the guard then works and should be
  added.

The guarantee this step adds covers **otto's own** uv calls. A dependency can
constrain those; it cannot constrain a command that runs before otto is installed
(`uv init`, `uv add`).

## Design

### The uv otto runs: resolved once per operation

`otto.env.backends` keeps its path. `venv_python` and `activation_line` stay as they
are. The rest of the module becomes the single installer:

- **`uv_bin() -> Path`** is the only place in otto that locates the uv executable.
  - It does a lazy `from uv import find_uv_bin`.
  - A `ModuleNotFoundError` whose `.name == "uv"` (the package is absent, e.g. after
    `pip install --no-deps`) becomes otto's `UvNotFoundError(OttoError, FileNotFoundError)`.
  - So does a `FileNotFoundError` from `find_uv_bin()` (the package is present but
    has no binary). `FileNotFoundError` is the public base class of the uv
    package's own `UvNotFound`, which `uv/__init__.py` does not export: its
    `__all__` is `["find_uv_bin"]`, in both 0.11.0 and 0.13.6.
  - Otto's `UvNotFoundError` is public, exported from `otto.env` (Chris, 2026-11-02).
    It mirrors the uv package's class: the same name with the `Error` suffix
    that ruff's N818 requires of otto, and the same `FileNotFoundError` base.
    A caller catches one name for "otto cannot run its uv", and an existing
    `except FileNotFoundError` still catches it.
  - No other `ImportError` is caught.
  - Both legs give the same message: otto's installation is missing the uv it
    depends on, so reinstall otto with its dependencies.
  - There is no `shutil.which("uv")` fallback.
- **`find_uv_bin()` has fallbacks of its own.** It searches the current
  interpreter's scripts directory, then the base prefix's, then the directories
  above and beside the package (`--prefix`/`--target` installs), then the
  user-scheme scripts directory (for example `~/.local/bin`). The first file found
  wins. In an intact install, that is the uv installed with otto.
  - Otto does not try to second-guess the locator.
  - What otto **records** is the version of the binary that actually ran, so a
    fallback never misreports (see "What the env records").
- **`uv_version(uv: Path) -> str`** runs `[uv, "--version"]` and parses the second
  token of `uv 0.13.6 (…)`. A failure raises `UvNotFoundError` naming the path.
- `create_venv(uv, env)` and `install(uv, env, targets, passthrough)` take the
  resolved path as their first argument, and their argv starts with it. Neither
  resolves on its own. How uv picks the venv's interpreter (`uv venv <env>`) doesn't
  change.
- **Deleted:** `VALID_BACKENDS`, `_uv_on_path`, `select_backend`, and
  `BackendUnavailableError`.
  - The `otto.env` facade swaps `BackendUnavailableError` for `UvNotFoundError`
    at all three of its sites: the `TYPE_CHECKING` import, the lazy map and
    `__all__`.
  - `uv_bin` and `uv_version` stay internal to `otto.env.backends`.

### One acquisition per create/sync, before anything changes

`manage` gains one small helper, `_acquire_uv(env) -> tuple[Path, str]`, shared by
`create_env` and `sync_env`. A sync with no env delegates to create as it does
today, so it runs the helper once, not twice.

1. Resolve `uv = uv_bin()` and `version = uv_version(uv)`.
2. **Refuse a self-rebuild.** If `uv` lies inside `env`, otto is running from the
   env it is about to replace. `create --force` would `rmtree` the executable it
   then calls, and a sync could replace it mid-run.
   - Refuse with an `OttoError` before deleting or installing anything.
   - The message tells the operator to run otto from an installation outside that
     env (for example, the project's own `.venv`).
   - Before this change uv came from PATH, so this case could not arise. The
     bundled uv makes it reachable.
3. The path is passed to every uv call of the operation. The version travels with
   it into `_fill`, which writes it into the metadata after the installs succeed,
   as today.

### What the env records and reports

- `EnvMeta.backend` is replaced by `EnvMeta.uv_version: str`. It holds the version
  of the uv that the last successful `create` or `sync` ran, measured at the start
  of that operation.
- `otto_version` already means "as of the last create/sync". The two fields share
  that meaning, so they are reported together on one line:
  `  last sync:   otto 0.18.0, uv 0.13.6`.
  - `create`, `sync` and `env show` all print that line where they printed
    `backend:`.
  - The `installed` column of `env show` stays the live probe.
- **Metadata from before this change** has no `uv_version`. `read_meta` already
  treats a missing key as unreadable:
  - `env show` says the metadata is unreadable. It names `otto env sync` first, which
    writes the record afresh, and `otto env create --force` for an env that is broken.
  - `sync` needs nothing from the old record. It runs and re-stamps the metadata.
  - Nothing refuses.

### Removing the setting and the flag

- `--backend` goes from `otto env create` and `otto env sync`. The `--` passthrough
  stays and goes to uv only.
- The setting goes from every site that carries it:
  - `EnvSettingsSpec` and `SettingsModel.env` (`src/otto/models/settings.py`);
  - `CompiledSettings.env_backend` and its construction from `model.env.backend`;
  - its copy into `Project.env_backend` (`src/otto/config/project.py`);
  - `manage._settings_backend` and its disagreement refusal.
- A `settings.toml` that still holds an `[env]` table is refused **with a pointer**
  in `SettingsModel._reject_removed_keys` (`src/otto/models/settings.py`). That
  validator already refuses the removed top-level `host_defaults`, `labs` and
  `lab_data_type`. Message: "`[env]` was removed: `otto env` always uses the uv
  installed with otto."
- The `otto init` template loses its commented `[env]` block.
- This is a user-facing removal, so the commit carries `!`.

### The floor lane

The lane exists because the uv dependency and its locator are now obligations at
the floor. That covers the Python package API, both missing-installation legs, the
version probe and argv construction. Step 1 will lean on it further when `uv sync`
and `uv lock --check` arrive.

- **The floor has one owner.** `scripts/check_docs_dependency_table.py`'s
  `parse_pyproject` already maps each `[project].dependencies` entry to its declared
  floor (its pattern reads the `>=` bound and ignores a cap, as with
  `pydantic>=2.6,<3`). It is tested in `tests/unit/test_docs_dependency_table.py`.
  - That script gains `--floor NAME`, which prints that one dependency's floor.
  - The lane runs it. Nothing else reads the floor, and the noxfile imports no
    script module.
- **The nox session is `tests_uv_floor`** (primary Python only).
  - It is a `nox_uv` session with `uv_groups=["dev"]`, like the other test sessions.
  - It then runs `session.install("--no-deps", f"uv=={floor}")`.
  - It asserts with `packaging.version.Version` equality (`0.11` equals `0.11.0`)
    that both `importlib.metadata.version("uv")` and the binary that `uv_bin()`
    returns report the floor.
  - Then `session.run("pytest", …)` runs `tests/unit/env` and
    `tests/e2e/cli/test_env_e2e.py` with `--no-cov`, keeping `-p no:tach` as the
    other uninstrumented sessions do.
  - It never runs `uv run pytest`, which would re-sync the project and undo the pin.
- **The e2e children use the pinned uv.** `run_otto` runs the `otto` script beside
  `sys.executable` (`tests/e2e/_otto_subprocess.py`), and `find_uv_bin` checks that
  interpreter's scripts directory first.
- **A new CI job** runs the session. Its cost is measured during implementation,
  not estimated here.
  - It is a **required check**, not nightly: a runtime-dependency floor is a
    property of every landing (Dev Process, 2026-11-02).
  - Like every validation job, it has `needs: [gate-admission]`, and its context
    name goes in `.github/required_contexts.json`, the one home of the required
    list. Both arrive with the landing-enforcement work, which lands first.
  - The live ruleset is a maintainer step. The PR body says the ruleset needs the
    new context. Until the ruleset has it, the job runs but does not block.
- **Make target:** none, written as `—`, like `api_dump_invariance`.
- **Binding:** the row goes in the single gate inventory,
  `docs/architecture/quality-gates.md`. `docs/contributing.md` points there; it
  doesn't hold a second table.
- **Ruleset:** whether `main`'s ruleset requires the job is the maintainer's call.
  The landing-enforcement work (Dev Process) is editing that ruleset and gets told
  about the job.
- **Narrower than the addendum's `--resolution lowest-direct`.** That flag drops
  *every* direct dependency to its floor, a wider question. Pinning only uv answers
  this lane's question.

### The wheel-matrix gate

uv's wheels are tagged `py3-none-<platform>`: platform-specific, but not tied to a
Python version. `scripts/check_docs_wheel_matrix.py` doesn't handle that shape
today:

- `classify` treats every non-pure, non-abi3 tag as per-version.
- `_tag_supports` rejects a binary tag whose interpreter is `py3`.

Adding uv to the runtime closure therefore turns the docs lane red. The fix:

- Add a matrix kind for "per-platform, any Python".
- Make `_tag_supports` accept a `py3` binary tag whose platform matches.
- Add tests in `tests/unit/test_docs_wheel_matrix.py`.
- Don't special-case the package name.

### Docs

**`docs/installation.md`:**

- **Quick install:** one sentence. The environment now also holds a compatible uv,
  and activating it puts that uv on PATH.
- **"With pip and venv"** becomes the bootstrap note. With no uv on the machine:
  1. Create and activate the quick-install venv; that gives you uv.
  2. Run the recommended `uv init` / `uv add` setup with it.

  The `requirements.txt` project layout and "both layouts are fully supported" go.
  One reassurance stays: a venv made by any means still *runs* otto, because the
  layout is about managing the project.
- **One "Which uv runs what" paragraph** that the env docs link to and don't
  restate. uv has two **roles**, which may or may not be the same executable:
  - The uv you run on your project (`uv init`, `uv add`, `uv sync`).
  - The uv otto runs for `otto env`, installed beside otto. `otto env show` prints
    its version from the last create/sync.

  Both must be 0.11 or newer, and they needn't match. Inside an activated
  environment that holds otto, the `uv` on PATH is otto's. The paragraph makes no
  claim about `uv self update`; upstream says that depends on how uv was installed.
- **"Getting uv into the air gap"** leads with the wheel route. `pip download
  otto-sh` now brings the uv wheel, so installing the outer uv from that same
  directory gives a compatible version with no extra step. The standalone binary
  becomes the alternative. Exact equality between the roles is promised only when
  both deliberately install that same version.
  - The separate `pip download uv` instruction goes.
  - The paragraph claiming that older releases don't know `environments` is
    corrected. 0.9.0 parses `environments`; only `system-certs` is new in 0.11.
- **The old-uv diagnostic:** the warning text, verbatim. It tells the reader that
  their uv is older than 0.11 and that `[tool.uv]` was ignored. What can follow is
  stated conditionally: a TLS failure behind an internal CA (no `system-certs`), or
  the unavailable-platform-dependency lock failure (no `environments`).
- **Tables:**
  - The direct-dependency table gains uv's row; `check_docs_dependency_table.py`
    requires it.
  - The native-wheel table gains uv as per-platform, any Python. Its intro changes
    from "These six do not … native (C/Rust) extensions" to wording that covers a
    bundled executable.
  - Step 1's wheel count changes.
- **Other edits:**
  - "Choosing a Python interpreter" loses its pip-venv clause.
  - The multi-project example output shows the `last sync:` line.

**Other docs:**

- **`docs/cli/env/`:**
  - `index.md` loses its usage-line `--backend` mentions and the "Backends" section.
  - Its passthrough section names uv only, and links the "Which uv runs what"
    paragraph.
  - `create.md` and `sync.md` lose `--backend`, the recorded-backend prose and the
    link to the removed backend order.
  - `show.md` loses its two-layout sentence.
  - All of them show the `last sync:` line, and `create.md` documents the
    self-rebuild refusal.
- **`docs/configuration/settings.md`:** the `[env]` section goes.
- **`docs/architecture/subsystems/bootstrap.md`:** the two backend bullets go.
- **`docs/architecture/quality-gates.md`:** gains the floor lane's row.

### Code and test inventory

**Production code:**

- `src/otto/env/backends.py`: the design above, plus its module docstring.
- `src/otto/env/manage.py`:
  - `_acquire_uv`.
  - `_fill`, `create_env` and `sync_env` lose `backend`/`backend_flag` and take the
    acquired pair.
  - `_settings_backend` is deleted.
  - The module docstring, the `_LIST_DISTS` note and the `EnvMeta` docstring lose
    their backend prose.
- `src/otto/env/__init__.py`: the three error-name sites, and the module docstring.
- `src/otto/cli/env.py`: `_BACKEND` goes, `_report` and `show` print the
  `last sync:` line, and the docstring loses `--backend`.
- `src/otto/models/settings.py`, `src/otto/config/project.py` and
  `src/otto/init/templates.py`: the setting removal above.
- `pyproject.toml`: the dependency, with a comment saying why it is capped. Then
  run `uv lock`.

**Tests:**

- **`tests/unit/env/test_backends.py`:**
  - The selection classes go.
  - Argv tests for **both** `create_venv` and `install` assert that `argv[0]` is the
    path passed in.
  - `uv_bin` tests patch `uv.find_uv_bin`, the real package attribute, which the
    lazy import reads. One test returns a sentinel; one raises `FileNotFoundError`;
    one simulates the absent package (`ModuleNotFoundError(name="uv")`). Both
    failure tests expect `UvNotFoundError`, and an unrelated `ImportError` still
    propagates.
  - A `uv_version` parse test, and its failure leg.
- **`tests/unit/env/test_env_paths.py`:** round-trips the new `EnvMeta`. A
  deliberately old, complete `{backend, otto_version}` record reads as `None`.
- **`tests/unit/env`, the manage tests:**
  - `_acquire_uv` runs once per operation.
  - The self-rebuild refusal fires before anything is deleted.
  - The acquired path reaches every call.
- **`tests/e2e/cli/test_env_e2e.py`:**
  - `BACKENDS` and every `--backend` go, and the docstring loses its pip-era cost
    notes.
  - Metadata asserts `uv_version` against the version of the running binary.
  - The `show` assertion reads the `last sync:` line.
  - The uninstall helper uses `uv_bin()`, not PATH `uv`.
  - Add a real **existing-env** `sync` case, the branch the file never runs today.
  - Add an `env show` case on an old-format record.
- **Settings and init:**
  - Settings: a leftover `[env]` table is refused with the pointer.
  - `tests/unit/config/test_compile_settings.py` loses `env_backend`.
  - `tests/unit/init/test_init_templates.py` loses its `EnvSettingsSpec` import and
    its `"env"` section entry.
- **Other test files:**
  - `tests/unit/test_error_base.py` names `UvNotFoundError`.
  - `tests/unit/test_docs_wheel_matrix.py` covers the new matrix kind.
  - `tests/unit/test_docs_dependency_table.py` covers `--floor`.
  - `tests/_fixtures/wheels/build_fixture_beetroot.py` loses "under both otto env
    backends".
- **API snapshot:** `tests/unit/api_snapshot/public_api.txt` loses
  `otto.config:Project.env_backend`. `otto.env` is not in `api/public.toml`, so the
  new names are pinned by the unit tests, not the snapshot.

**Left alone deliberately:**

- the PATH-uv calls in `scripts/api_regen.py` and its tests (developer tooling);
- nox's own `--backend` (`noxfile.py`);
- the reservations `--backend`;
- the synthetic malformed-markup fixture in `tests/unit/cli/test_help_text_user_facing.py`;
- the paste-able `or: uv pip install …` remediation in `src/otto/cli/invoke.py`,
  which is the user's uv, not a call otto makes.

## Out of scope

- **Step 1 of #555:** the generated root `pyproject.toml`, `uv sync` and
  `uv lock --check`, removing `_attribute_conflict` and the mtime staleness check,
  and #554.
- Groups, identity and `otto env switch`.
- A lane testing every dependency at its floor.
- A guarantee that every subprocess in one operation runs byte-identical uv. The
  recorded version means "the uv at the start of the operation".

## Risks

- **Size.** Measured on aarch64:

  | uv | Wheel | Installed binary |
  |---|---|---|
  | 0.13.6 | 16.5 MB | 31.7 MB |
  | 0.11.0 | 23 MB | 50.5 MB |

  Each venv gets a copy (a hardlink from a warm uv cache in the usual case). The
  e2e builds pay the download on a cold CI cache inside `BUILD_TIMEOUT = 150`.
- **Platforms.** PyPI ships uv wheels for manylinux (2_17 and 2_28) and musllinux,
  on x86_64, aarch64 and others, plus macOS and Windows. That covers the documented
  targets. A platform with no wheel builds uv from source, which needs Rust.
- **An outer uv below 0.11** still warns and ignores `[tool.uv]`. The docs quote the
  warning (see the correction).
- **Cap churn:** one Dependabot PR and one otto release per uv minor.
- **Step 1 (hypothesis):** if the outer and inner uv ever read or write the same
  lockfile, minor-version skew could matter. Step 1's design must answer this.
