# #649: one vocabulary for projects — OTTOPATH, project, project root, workspace

**Issue:** #649 (split out of #604).
**Status:** design approved in conversation (2026-10-17). Revised after Astra (NOT READY) and Fable (READY WITH FIXES) reviews of the first draft. The Astra re-check closed 13 of its 14 findings; this text resolves its two remaining MAJORs, path spelling (§5.2) and the rename-test mechanism (§6.3). Awaiting Chris's review.
**Base:** origin/main af010a79c (after #590 P9b and the ratchet-retirement follow-up).

## 1. Why

otto calls one idea by several names. A directory holding a user's project is a "repo", a "SUT dir" or an entry of `OTTO_SUT_DIRS`. "Workspace" and "project" appear beside those words with overlapping meanings. otto has very few users, so this is the cheapest time to choose one vocabulary and apply it everywhere.

## 2. The vocabulary

| Term | Meaning | In code |
|---|---|---|
| **project** | A named unit. Its identity is its `name`. Its optional `[project]` table scopes it to labs and hosts. | `Project` (the loaded object), `project` / `projects` |
| **project root** | The directory holding one copy of a project: `.otto/settings.toml`, libs and tests. Two clones of one project are two project roots. | `root: Path` (`Project.root`) |
| **`OTTOPATH`** | The list of project roots, separated by `os.pathsep` like `PATH`. It replaces `OTTO_SUT_DIRS`. | `OttoEnvSettings.ottopath` |
| **workspace** | The set of projects active together: one `OTTOPATH` value, with its own workspace home under `~/.otto`. This is otto's current meaning, and it is kept. | unchanged (`workspace_home`, `workspace_key`) |

A path needs no noun of its own. It points at a project root, as `PATH` entries point at directories. Prose says "add the project root to `OTTOPATH`"; code holds the path in `root`.

"Workspace" keeps its current meaning because that meaning matches the wider ecosystem. In Cargo, uv, npm, pnpm, Yarn, Go (`go.work`), VS Code multi-root and Eclipse, a workspace is a container of several projects. It is never one copy of one project.

### 2.1 Homonyms

- **Compose project** is Docker Compose's own term. It is kept, but always qualified as "compose project", just as git's "repository" is. Its identifiers are not renamed, including the public `project_name=` keyword on `compose_up`, `compose_down` and `composed` and on the deployment helpers.
  - The one rename on this side is Docker staging's `project_root(project)` (`docker/staging.py:56`), which becomes `compose_staging_root(project)`, so that "project root" has exactly one meaning.
- **The coverage report title** is called `project_name` today (default `"Coverage Report"`). It becomes `report_title` everywhere it means the title, and only there:
  - the parameters and fields in `suite/run.py` (`RunOptions`), `coverage/reporter.py` (`CoverageReporter`, `run_coverage_report`) and `coverage/renderer/*`;
  - the CLI flag `--project-name` → `--report-title` (`cli/test.py:561`, `cli/cov.py:208`);
  - the SPA payload key `"project_name"` → `"report_title"` (`coverage/renderer/spa_data.py:606`), together with `web/src/covapp/types.ts:119` and its contract test (`contract.test.ts:259`), rebuilt by `make web`.

  This rename is per file, not a global identifier rule, because the compose `project_name` keeps its name.
- **Lab-data "repository"** is the repository pattern for host data: `LabRepository`, `ExampleLabRepository`, `lab_repository` and the `labs/json_repository.py` backend. It keeps its name. Where that code abbreviates it as `repo`, the abbreviation becomes `repository`, so that the bare word "repo" leaves otto entirely. This includes the public `otto.testing.assert_lab_repository_conforms(repo=…)` → `(repository=…)` and the lab-repository locals and fixtures. The vocabulary guard (§6.3) then needs no lab-data exemption.
- **Git's own "repository"** is untouched. Prose about git keeps "git repository" and is allowlisted where it says "repo" (§6.3).

## 3. Landings

The rename lands in two squashes. Each is its own plan and its own gated tip.

**Landing 1, the breaking rename.** It contains everything users can see, plus every edit that renaming forces:
- **Names.** The identifier pass (§6.1) runs over `src/`, `tests/`, `scripts/`, `noxfile.py` and `docs/` code in this landing. A forced `sut_dir` keyword in a test cannot be told apart from a test-local one, so tests take the code renames here.
- **The environment variable.** `OTTO_SUT_DIRS` → `OTTOPATH` in every file that sets or strips it:
  - `project_env`;
  - `scripts/import_budget.py`, `refresh_docs_captures.py`, `capture_docs_termynal.py` and `wheel_smoke.py`;
  - the environment sanitizers (§5.6), the harnesses, the shim and the docs.

  Fixture paths such as `tests/repo1` keep their names in this landing.
- **Behaviour and contracts:**
  - the behaviour changes of §5;
  - every public name, field and parameter (§4.1);
  - CLI options, help, output and messages;
  - `otto init` scaffolding;
  - the on-disk keys (§4.3);
  - all docs prose.
- **The commit** is marked `!` and carries a `BREAKING CHANGE:` footer naming `OTTOPATH`, the renamed public names, `--report-title` and `otto docker build --project`.

**Landing 2, test-only churn, invisible to users.**
- The fixture trees: `tests/repo1`–`repo5`, `repo_broken` and `repo_e2e` become `tests/project1`–`project5`, `project_broken` and `project_e2e`. Their declared names and their `repoN_*` Python packages change to match.
- Test-only helper identifiers: `make_sut_repo`, `copy_sample_repo`, `sut_repo`, `two_sut_repos`, `repo_double`, `fake_repo`, `generate_repo`, `make_shim_repo`, `default_sut_dir`, `cli_sut_dir` and the like. These helpers keep their names in landing 1; only the code they call is renamed there.
- Test-function and test-file names that say repo or SUT.
- The import-budget surface labels (`bootstrap_repo`, `help_repo`, …) in the ceilings files and in `scripts/import_budget.py`.
- Every file that cites a fixture path: about 38 docs pages, the `Vagrantfile`, `pyproject.toml`'s marker help, the ast-grep `ignores:` lists and `sgconfig.yml`, and `project_env`'s paths.
- The vocabulary guard extends to `tests/` (§6.3).

There is no compatibility layer: no aliases, no old-name fallbacks and no readers for old on-disk keys.

## 4. Name mapping (landing 1)

### 4.1 Public names

Each row renames the definition and every use.

| Today (defined in) | After |
|---|---|
| `Repo` (`otto.config.repo`, exported by `otto.config`) | `Project` (`otto.config.project`, exported by `otto.config`) |
| `Repo.sut_dir` | `Project.root` |
| `otto.bootstrap.get_repos`, `get_ordered_repos` (`declared.py`) | `get_projects`, `get_ordered_projects` |
| `.repos`, `.ordered_repos` on `BootstrapResult` and `OttoContext` | `.projects`, `.ordered_projects` |
| `OttoContext.for_repo(repo_name)` | `OttoContext.for_project(name)` |
| `ProjectContextView(ctx, repo_name)`; `ProjectActions(repo, ctx)` | `ProjectContextView(ctx, project_name)`; `ProjectActions(project, ctx)` |
| `sut_dir` on `BootstrapError`, `BootstrapWarning`, `DependencyError`, `ProjectScopeError` (parameter and attribute) | `root` |
| `OttoEnvSettings.sut_dirs` | `OttoEnvSettings.ottopath`, read from `OTTOPATH` (§5.1) |
| `RepoBuild` (`otto.docker`) | `ProjectBuild` |
| `RepoPlan` (`otto.project`) | `ProjectPlan` |
| `RepoScope` (`otto.project`), the display projection of the internal `ProjectScope` verdict | `ProjectScopeSummary` |
| `RepoCheck`, `check_repos` (`otto.session`) | `ProjectCheck`, `check_projects` |
| `DemotedRepo(error, repo, project, reason, labs)` (`otto.session`). `repo` is the name as discovered; `project` is the normalised name. | `DemotedProject(error, name, normalized_name, reason, labs)` |
| `RepoLoadError` (`otto.session`) | `ProjectLoadError` |
| `RequireRepo`, `registering_repo`, `get_registering_repo`, and `.repo(name)` on `Registry`, `BackendRegistry` and `RegistryView` (`otto.registry`) | `RequireProject`, `registering_project`, `get_registering_project`, `.project(name)` |
| `RepoOptions` (`otto.examples.options`, and the class inside the `otto init` template string) | `ProjectOptions` |
| `check_repo` (`otto.init`, `init/doctor.py`) | `check_project` |
| `InitConfig.for_repo` (`otto.init`) | `InitConfig.for_project` |
| `CleanlinessKind.REPO = "repo"` (`otto.project`) | `CleanlinessKind.PROJECT = "project"` |
| `repo_dir` on `LabSourceEnv`, `ReservationEnv`, `ReservationBackendBase`, `NullReservationBackend`, `build_backend`, `gate_from_settings` and `ExampleReservationBackend` | `project_root` |
| `ReportInputs.repo_root` (coverage) | `ReportInputs.project_root` |
| `DepStatus` / `ResolvedDependency.status` literal values | `"ambiguous"` is removed (§5.3) |
| `otto.testing.assert_lab_repository_conforms(repo=…)` | `(repository=…)` (§2.1) |
| Every `repo` / `repos` parameter or field: Docker's `build_images`, `build_on`, `compose_up`, `compose_down` and `composed`, `BuildReport.repos`, `FailedImage.repo`, `RepoBuild.repo`; coverage's `clean_coverage`, `collect_coverage`, `get_coverage` and `resolve_report_inputs`; `ProjectStatus.repos`, `RepoPlan.repo`, `actions_for`; session's `build_lab`, `merge_host_preferences`, `merge_logging_levels` and `select_projects`; `ProfileContext.from_repos`, `check_data_profiles`, `build_inventory`, `fleet_of_interest`, `MonitorTlsError`, `resolve_monitor_tls`, `serve_review`, `build_lab_sources` and `build_reservation_gate` | `project` / `projects` (`from_repos` → `from_projects`) |
| `get_user_compose_project(repo_name)` | `get_user_compose_project(project_name)` |
| Docker staging `project_root(project)`; coverage `project_name` | `compose_staging_root(project)`; `report_title` (§2.1) |

The public-API golden file (`tests/unit/api_snapshot/public_api.txt`) is regenerated with `make api-snapshot`. Its diff must equal this table and nothing else.

### 4.2 Private names, and the collisions resolved before the pass

The general rules are: `repo` → `project`, `repos` → `projects`, `sut_dir` → `root`, `sut_dirs` → `roots`, a `Repo` prefix or suffix → `Project`, and `SUT_DIRS_ENV_VAR` → `OTTOPATH_ENV_VAR`. The plan lists every name in the script's table.

These rules would collide with names that already exist. Each is resolved by an explicit exception, applied before the general rules:

| Collision | Resolution |
|---|---|
| `collect_repo_names` (`completion_cache.py:1807`): active projects, sorted, completing Docker's `--repo`. `collect_project_names` (`:2216`) already exists: discovered projects in discovery order, completing `-I`/`-E`. | `collect_repo_names` → `collect_active_project_names`; `collect_project_names` is unchanged |
| The completion payload's `repos` key (active, sorted) beside an existing `projects` key (discovered, in order); `write_cache` already has a `projects` parameter | `repos` key and parameter → `active_projects`; `projects` is unchanged |
| `DemotedRepo.repo` beside `DemotedRepo.project` | as in §4.1: `name`, `normalized_name` |
| A local or parameter `project` that holds a parsed pyproject `[project]` table, beside a `repo` in the same scope: `env/preflight.py` `_requirements(repo, project, env)` (:150) and `_check` (:175), and `env/manage.py` `_dist_name` (:346) | that `project` → `pyproject_table`, renamed first |
| `otto.config.repo.get_repos(repos: list[Path])` (`repo.py:634`), which shares its name with the public accessor but builds objects from paths | `load_projects(roots)` |
| `_requirements`-style collisions the plan's dry run finds | every one is listed in the table before the pass runs, and the dry run must report zero duplicate parameters, fields or names in one scope |

### 4.3 User-visible strings and on-disk keys

- **CLI:**
  - `otto docker build --repo` → `--project`, including its `_DOCKER_FLAGS` key (`cli/docker.py:459`);
  - the `--provide` help `CAPABILITY=REPO` → `CAPABILITY=PROJECT`;
  - `--project-name` → `--report-title` on `otto test` and `otto cov`;
  - the "repo" columns in `otto env show` and the host listings → "project";
  - the root help (`cli/main.py:58-64`) describes `OTTOPATH`. Its stale claim that a comma separates entries is removed.
- **Messages, logs and the `otto init` templates** say "project" or "project root", whichever is meant. Examples: "repo {sut_dir}: failed to load …" → "project root {root}: failed to load …"; "cannot run while a repo fails to load" → "cannot run while a project fails to load".
  - The init templates are string content, so they are a pass-2 edit (§6.1). `tests/unit/init/test_init_scaffold.py` pins their output.
- **The completion cache:**
  - the key `repos` → `active_projects` (§4.2);
  - each host-drop entry's `repo` → `project`;
  - the schema stamp moves 27 → 28 in both `completion_cache.py:245` and `_shim_complete.py:42`, so existing caches rebuild themselves. The stale "23" in the module docstring (`:46`) is corrected.
- **Coverage metadata (`.otto_cov_meta.json`):** `repo_name` → `project_name`, `sut_dir` → `project_root`. Old metadata is refused (§5.5).
- **The SPA payload:** `project_name` → `report_title` (§2.1).
- **The empty-workspace slug:** `no-repos` → `no-projects`.
- **The pydantic validation-context key:** `"sut_dir"` → `"root"` (`config/repo.py:182`, read at `models/settings.py:85` and `:467`).

## 5. Behaviour changes

### 5.1 Reading OTTOPATH

- **The field.** It is declared `ottopath: Annotated[list[Path], NoDecode] = Field([], alias="OTTOPATH")`. pydantic-settings applies no `env_prefix` to an aliased field, so the environment variable is exactly `OTTOPATH`. Nothing constructs `OttoEnvSettings` by field name today, so `populate_by_name` is not enabled.
  - Tests pin: the variable is read; unset or empty gives `[]` (`env_ignore_empty`); and `NoDecode` still hands the raw string to the validator.
  - `alias`, not `validation_alias`, so the API snapshot's `input` row is predictable.
- **Splitting.** The value is split by `otto.utils.split_path_list`, exactly as #604 made `OTTO_SUT_DIRS` split: on `os.pathsep` only, empty entries dropped, whitespace kept as part of an entry. The `@field_validator` targets `"ottopath"`.
- **Missing directories.** A listed directory that does not exist still fails at startup (`load_otto_env`).

### 5.2 A repeated root collapses

The collapse happens in the field validator, after splitting. So every construction of `OttoEnvSettings` agrees, including the ones that bypass `load_otto_env` (`config/home.py:66`, `:128`; `docker/compose.py:82`, `:112`).
There is one spelling rule for the paths otto keeps, and a separate key used only for comparison. Both are defined once and mirrored in the stdlib-only shim:
- **The kept spelling:**
  1. Each entry is `~`-expanded in the field validator, before validation and loading. Today `~/x` fails in otto but names `$HOME/x` in the completion children, and that disagreement ends.
  2. A relative entry is anchored to the current directory by joining (`Path.cwd() / p`, in the shim `os.path.join(os.getcwd(), p)`). `..` and symlink spellings are kept exactly; there is no `abspath`, `normpath` or `resolve()`.

  The loader's existing `.absolute()` (`repo.py:426`) already behaves this way. The validator now produces that form for every reader, and validation (`config/env.py:52`) checks the same spelling that is loaded.
- **The comparison key:** `os.path.realpath` of the kept spelling. It is used only to detect repeats and in the workspace-home key (`config/home.py` `_normalized`). It is never used to load, and never passed to a child process.
- **Surviving entry:** the FIRST entry with a given key, in its kept spelling. Later entries with the same key are dropped.
- **The two child-environment builders,** `_shim_complete._child_environ` (stdlib only) and `completion_cache._collect_child_command`, serialize the collapsed list in the kept spelling. They no longer call `os.path.abspath`, which collapses `..` before symlinks are followed: `/bin/..` is `/usr` on disk, but `abspath` gives `/`. The same kept-spelling rule applies to the `OTTO_XDIR` and `OTTO_HOME` entries they also forward.
- **The shim's `locate_cache`** collapses the same way before computing `workspace_key`. That function already de-duplicates, so its answer is unchanged.
- **Tests** run every case through discovery, both child builders and the workspace key, and require all of them to agree:
  - a symlink and its target;
  - a symlink followed by `..` (`<link>/..`);
  - a relative and an absolute spelling of one root;
  - `~` and its expansion;
  - interleaved repeats (`a:b:a:c:b`).

### 5.3 Two roots of one project are refused

The refusal runs inside `discover()`, before the `DiscoveryResult` is cached, after every root has been parsed.
- **The match.** Discovery groups the loaded projects by `normalize_name` (`models/dependencies.py:26-28`). It lowercases and collapses runs of `-`, `_` and `.` to `-`, so `my_proj`, `My-Proj` and `my.proj` are one name.
- **What happens when two or more roots claim one name:**
  - None of those roots loads. otto never guesses which copy is meant.
  - Each of those roots gets a framed `BootstrapError` on the existing per-project containment path. The message names the project and every root that claimed it, for example:

    > project 'repo1' has 2 roots in OTTOPATH: /a/repo1, /b/repo1; keep one
  - Projects with unique names load normally.
- **How the refusal surfaces.**
  - The errors belong to no loaded project, so they are unowned. Every real command fails loudly, whatever `-I`/`-E` say.
  - `--help` and completion still work, as for any project that fails to load.
  - A refused name still counts as a known name for `-I`/`-E` (`session/projects.py:167`). So `-I twin` or `-E twin` reports the duplicate-root error, not "no project 'twin'". The plan defines this precedence and tests it.
- **The dependency pass simplifies.** Its "ambiguous provider" branch (`config/dependencies.py:131-140`) becomes unreachable, because no two loaded projects share a normalized name. It is removed, and so are:
  - `"ambiguous"` from the public `DepStatus` literal (`:25`) and `ResolvedDependency.status`;
  - the `provider_version` docstring note (`:47`);
  - its tests, `test_ambiguous_name_errors_when_referenced` and `test_duplicate_names_unreferenced_no_error`.

  New tests cover the refusal: case and punctuation variants; selected, excluded and unaffected projects; and `--help` still working.
- **No other code changes.** Name-keyed maps that silently kept the last entry (`bootstrap.py` `by_name`, `config/scope.py` scopes, the orchestrator, Docker deployment and build verbs, `host/listing.py`, `suite/run.py`) need no change, because duplicates can no longer reach them.

### 5.4 OTTO_SUT_DIRS

`OTTO_SUT_DIRS` is never read. A shell that still exports it gets no projects from it, and there is no warning (Chris, 2026-10-17: a pure cutover). The CHANGELOG's breaking-change entry is where users learn of the rename. A test pins that an exported `OTTO_SUT_DIRS` has no effect.

### 5.5 Old coverage metadata is refused

`_read_cov_meta` (`coverage/reporter.py:101`) reads only the first input directory's metadata, while reporting reads counters from every input (`:1083`). Every input directory's `.otto_cov_meta.json` is now validated:
- metadata that lacks `project_name` / `project_root`, or that carries the old keys, raises a framed `CoverageInputError`;
- the error names the directory and the stale key, and says to re-collect.

Tests run both input orders (old first, new first), so an old directory can neither crash on a `KeyError` nor slip in silently.

### 5.6 Hermetic environments strip OTTOPATH

The environment sanitizers strip only names that start with `OTTO_`, and `OTTOPATH` does not. In landing 1, each of them strips `OTTOPATH` explicitly:
- `tests/conftest.py:238`;
- `scripts/import_budget.py:737`;
- `noxfile.py:857`;
- the capture and subprocess harnesses (`tests/e2e/_otto_subprocess.py` and the docs-capture scripts).

The hermeticity probe (`tests/unit/test_env_hermeticity.py:58`) plants `OTTOPATH` and asserts it is stripped. One shared constant names the variables to strip, so a future rename cannot miss a sanitizer.

## 6. Mechanics

### 6.1 The rename script (one-off, not committed)

The script lives in the session scratchpad. Its table, collisions first (§4.2), is the plan's appendix, so a reviewer can check the sweep against it and re-run it to an empty diff. It runs under Python ≥ 3.12, not the 3.10 venv: from 3.12, `tokenize` emits the names inside f-strings as `NAME` tokens. There are three passes.

1. **Identifiers.** Only `NAME` tokens are rewritten, including those inside `FSTRING_START` … `FSTRING_END`. `FSTRING_MIDDLE` text, plain strings, comments and docstrings are left alone. The table applies longest name first. A dry run reports every scope with a duplicate parameter, field or name after renaming, and the pass runs only when that report is empty.
2. **Executable strings.** These are strings that code reads as names, so the identifier pass cannot see them. Each kind is listed and edited, with a per-row search proving none is left:
   - `__all__` lists and the lazy-export tables (`otto/config/__init__.py:66` and every package `_LAZY_EXPORTS`);
   - `@field_validator("…")` targets (`models/settings.py:1167`);
   - `getattr(…, "<old>")` and `hasattr` names (`host/listing.py:368`, `completion_cache.py:1665`);
   - `.format(<old>=…)` templates together with their `{<old>}` placeholders (`run_context.py:1169-1173` and `:1287`, `project/actions.py:111` and `:323`, `config/scope.py:972`);
   - dotted monkeypatch and `patch()` targets (`"otto.config.repo.…"`, `"otto.bootstrap.get_repos"`, …);
   - string-literal type annotations (`"Repo"`, `"list[Repo]"`);
   - pytest fixture and `parametrize` names, and Typer option declarations;
   - import-contract strings (`tests/unit/test_import_contracts.py`, `tests/unit/test_no_import_time_lazy_exports.py`);
   - the Sphinx `automodule` target (`docs/api/internals/config.rst:16`);
   - the data keys of §4.3;
   - the `otto init` template strings.
3. **Paths.** `git mv` moves `src/otto/config/repo.py` → `project.py`, and the test files named for that module (`tests/unit/config/test_repo*.py`) to match. Lazy imports stay lazy. The move preserves `otto.config`'s lazy table, not an eager import.

### 6.2 The prose pass (by hand)

A residual search lists every remaining whole-word `repo`, `repos`, `Repo`, `SUT` and `sut_dir` in `src/`, `tests/`, `docs/`, `README.md`, `AGENTS.md`, `Makefile`, `noxfile.py`, `scripts/`, `tach.toml` and `.ast-grep/`. Each hit is either rewritten or confirmed as git's sense of the word. In `tests/`, landing 1 leaves the landing-2 helper and fixture names (§3).
- Left as they are: `docs/superpowers/` (historical specs and plans) and existing `CHANGELOG.md` entries. The CHANGELOG is generated from commit subjects.
- `docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md` keeps its file name, and the docstrings and tests that cite it keep citing it.

### 6.3 Guards

- **The old module cannot return.** `otto.config.repo` joins the gone-module list of `tests/unit/test_moved_name_spellings.py`. That forces every string citing the module to change: the import contracts, the lazy-export tests, the `automodule` and the architecture docs.
- **Renamed public names have a dedicated test.** A new vocabulary-rename test holds the §4.1 table: old name, new name, owning namespace, and kind (class, function, field or parameter). For each row it asserts two things against the regenerated public-API snapshot: the old spelling is absent from its namespace, and the new spelling is present.
  - `test_hook_renames.py` is left as it is. Its inventory only fits renames of underscore-prefixed hook members on unchanged owners.
  - `test_moved_name_spellings.py` covers module moves only.
- **Old words cannot come back.** A new test fails on:
  - `OTTO_SUT_DIRS` anywhere;
  - `sut_dir` / `sut_dirs` in `src/` and `docs/` (landing 2: also `tests/`);
  - a whole-word `repo` / `repos` / `Repo` in `src/` and `docs/` (landing 2: also `tests/`).

  The match is whole-word, so it never fires on `repository` or on the fixture names `repo1`…`repo5`. Its exemptions are by path or by explicit allowlist, each entry commented:
  - historical: `docs/superpowers/`, `CHANGELOG.md`, and citations of the `…-repo-and-scope-inputs-design.md` file name;
  - negative inventories that must spell the old names: `test_moved_name_spellings.py`, the new vocabulary-rename test, and the one test pinning that `OTTO_SUT_DIRS` has no effect;
  - the guard's own file;
  - git-sense lines ("the git repo") in prose.

  The guard proves it can fail: a planted old word in a scratch tree turns it red.
- **Tooling that names the old paths:**
  - the ast-grep rule `cli-command-no-module-scope-heavy-import.yml` (`config\.repo` twice, `^(repo|Repo|…)`), its rule test, and `tests/unit/test_import_ban_rules.py`;
  - `tach.toml` comments (no module entry names `otto.config.repo`);
  - `scripts/render_module_graph.py` prose.

### 6.4 Docs

The four terms get one home: the overview's "Repos and settings" section (`docs/overview.md:99`), renamed. Every other page links to it rather than restating it, which is the docs single-source rule.
- Section headings that say "repo" are renamed: "Multiple repos", "Several repos", "An example repo", "The repo adapter", "Standing in for a Repo", and so on.
- `docs/cookbook/authoring/writing-instructions.md`, which teaches `class RepoOptions`, follows §4.1.

### 6.5 Import cost

Two effects are measured separately:
- **The module move.** `otto.config.repo` → `otto.config.project` replaces one file with one file, so it is expected to add 0 file operations on every surface.
- **The repeat collapse.** §5.2's `resolve()` adds path operations per root, each time `OttoEnvSettings` is built. That growth is measured on every import-budget surface. Resolution is reused rather than repeated, for example by computing each root's key once and sharing it with the workspace-home key.
  - Any growth that remains is reported to Chris with the measured counts before landing. It is never re-baselined silently.

## 7. Verification

- While working: targeted tests for each area touched.
- At each landing's tip: the lanes `make gate-scope` names (the full gate is expected), stamped by `make gate-tip`, then `make gate-fresh`.
- Gate time is coordinated with any other live session, because all worktrees share one stamp store (#650).
- Landing 1's new tests:
  - §5.1: the `OTTOPATH` field contract;
  - §5.2: the collapse and agreement cases;
  - §5.3: the refusal and its precedence;
  - §5.4: the inert old variable;
  - §5.5: both coverage input orders;
  - §5.6: the hermeticity probe;
  - §6.3: the guard, including its planted red, and the vocabulary-rename test;
  - the regenerated API snapshot, whose diff equals §4.1.

## 8. Decisions log

Chris, 2026-10-16/17:
1. The vocabulary in §2, including "project root" for a copy on disk and a path that just points at it.
2. "Workspace" keeps its meaning: the set of projects active together.
3. Rename "repo" everywhere now, while users are few.
4. Two landings, as in §3.
5. Compose projects are qualified and kept; Docker staging's `project_root()` → `compose_staging_root()`; coverage's `project_name` → `report_title`.
6. Duplicate project names are refused, and a repeated identical root collapses.
7. `OTTO_SUT_DIRS` is ignored entirely after the cutover.
8. A table-driven rename. The script is a one-off and not committed; its table is recorded in the plan.

Made in this revision, after the reviews (Chris to confirm):
9. Lab-data code's `repo` abbreviation → `repository`, including the public `assert_lab_repository_conforms(repo=)` (§2.1).
10. The SPA payload key and the web types rename to `report_title` with the flag (§2.1).
11. Old coverage metadata is refused with a framed error (§5.5).
12. `DemotedRepo` → `DemotedProject(name, normalized_name)`; the active-names collector and payload key → `active_projects` (§4.2).
