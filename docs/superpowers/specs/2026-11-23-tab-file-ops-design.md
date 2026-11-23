# Every kind of TAB site is measured in file operations (#1257)

## Intent
The file-operation gate (`scripts/import_budget.py`, with ceilings in `tests/unit/import_budget/ceilings/`) measures only two TAB sites today: `otto <TAB>` and one handover, `otto tunnel remove <TAB>`. A regression on any other kind of site passes it. One example would be a completer that starts importing the CLI where the shim used to answer.

This change measures one warm surface for each cost profile a TAB can take. It proves each measured run took the path it claims to take, and it makes a new site fail a test until it is covered. Completion behaviour does not change; this only measures it.

## Goals
- One warm, gated surface for each cost profile a TAB can take. A warm TAB on the shim's path stays O(1) in corpus size.
- A measured run counts only when it can show the path it took. It must show who answered (which modules loaded and which never did) and what it answered. This verdict is the same one that normal checks, `make import-snapshot` and the delta reports use, so a broken fixture can never become a baseline.
- A coverage test that classifies every completion site, and fails when a new site would take a path no surface measures.
- The places where the shim and the completers decide the same thing twice are listed. Nothing changes because of the list.

## Non-goals
- Changing what any TAB answers, or which path answers it.
- Measuring the remote-path cache **miss**. It is the one TAB that contacts a host, and its cost depends on that host and its hops (maintainer, 2026-11-22). The gate names it as deliberately unmeasured.
- Wall-clock time. Every count is a file operation.

## Cost profiles and their surfaces
A TAB's file-op cost is decided by the path that answers it, not by the declared kind of the parameter:
- **Shim profiles.** The shim answers from the cache after validating it. The validation is one of two kinds: the `names` stat pass, or the test tables. Within one validation, the kind dispatch (`payload`, `static`, `none`, `observed`, `tests`, `markers`; `src/otto/_shim_complete.py:741-790`) is in-memory work over data the validation has already loaded.
- **Handover profiles.** The full CLI answers. Its cost depends on what the completer does once it runs.

Every surface below is warm: a seed run on the same fresh home writes the cache first. Every surface is gated in every minor's ceilings file.

| Surface | TAB | Profile | Proof of path (all part of the measurement's validity) |
|---|---|---|---|
| `completion_project_warm` (exists) | `otto <TAB>` | shim, command menu, `names` validation. It stands for every command-name, option-name and host-verb menu. | deny the import roots below; `expect_answer`: a command name |
| `completion_host_warm` (new; replaces the tracked `tracked_tab_warm_templated`) | `otto -l unix+templated host <TAB>`, with a jsonnet lab source beside the json one. The `tmpl-*` hosts live only in lab `templated` (`tests/_fixtures/generated_project.py:92-99,120`); the lab flag does not change the stat pass. | shim, `payload` from the `names` payload, including the lab-file and import-root stat pass | deny; `expect_answer`: one json-source host and one `tmpl-*` host, each as a whole candidate line |
| `completion_test_warm` (new; rebases the ad hoc `test_a_test_name_tab_does_not_scale_with_corpus_size`) | `otto test T<TAB>` | shim, test-table validation (`table_view`) | deny, plus `subprocess`, so a due refresh that starts a collect child fails; `expect_answer`: a seeded test name |
| `completion_docker_observed_warm` (new) | `otto docker logs --parent test1 <TAB>` | shim, `observed` from `__docker_observed__` | deny; `expect_answer`: a distinctive seeded container name |
| `completion_project_handover` (exists) | `otto tunnel remove <TAB>` | handover, `live` declaration | `expect_loaded`: `otto.cli.tunnel` |
| `completion_hosts_past_sep_handover` (new) | `otto tunnel add --hosts test1,<TAB>` | handover, conditional: `payload` with `live_past_sep`. The completer runs host and address discovery and L2 narrowing (`src/otto/cli/tunnel.py:103-141`). | `expect_loaded`: `otto.cli.tunnel`; `expect_answer`: the exact candidate set, spelled with the typed prefix (`test1,test2`, `complete_separated_list` keeps it, `src/otto/utils.py:542`). The set must exclude a host outside test1's L2 segment. Otherwise the completer's catch-all fallback to every host (`tunnel.py:135-140`) would pass. If the generated project has no such host, the fixture adds one. |
| `completion_remote_path_hit` (new) | `otto host budget-ssh get <TAB>`, with the listing cached | handover, `live` declaration, cache hit | `expect_loaded`: `otto.cli.remote_completion`; deny `asyncssh.connection`, since a hit opens no connection; `expect_answer`: a seeded entry, as a whole candidate line; the remote sidecar is unchanged across the measured run, so the hit neither refetched nor rewrote |
| `completion_remote_path_hit_reserved` (new) | the same TAB, with `[reservations]` declared and the reservations cached | handover, `live` declaration, cache hit through the cached reservation gate | as the hit, plus deny `otto.reservations.registry`; a control without the cached reservations fails |

With `completion_remote_path_hit_reserved` (below), that makes 8 surfaces: the 2 that exist plus 6 new ones, and `tracked_tab_warm_templated` goes away. At 6 minors that is 48 baselines, 36 of them new.

**Two validity checks every warm completion surface carries** (not the verb surfaces):
- **Unchanged cache.** `completion_cache.json` is unchanged (mtime and size) across the measured run. A handover that found the cache stale and rebuilt it fails, instead of being baselined. A scaling pin can't catch this, because a cold rebuild is itself pinned O(1) (`tests/unit/import_budget/test_import_budget.py:973`).
- **Stat leg.** On the shim surfaces, the measured run takes the `names` stat leg. A `names` marker that is still fresh (60 s window) skips the stat pass, so the surface would measure the cheaper leg. The harness backdates the marker after the seed, as `_warm_tab` does (`tests/unit/config/test_completion_templated.py:644`), and the check proves it.

### Dispatch branches inside a shim profile
The `static`, `none`, `markers` and `payload` sub-variants answer from the payload or tables their validation already read. They are covered by one unit test (`tests/unit/shim/`), not by 4 more measured surfaces. An in-process audit hook cannot hold this guarantee: CPython raises no audit event for `os.stat`, and an import of an already-loaded module raises no `import` event. The test therefore records whole TABs in child processes:
- **One recorder.** `_RECORDED_TAB` / `_recorded_tab` (`tests/unit/config/test_completion_templated.py:584-642`) already runs a TAB through the real shim entry in a child and records opens, listings and sockets by audit hook, and stats by wrapping `os.stat`. It moves to a shared fixture that both tests use. A second recorder would be the same decision made twice.
- **One recording helper, one starting state.** The shared helper establishes the same state before EVERY recorded TAB, the representative's and each sub-variant's:
  - One unrecorded priming TAB after the seed, so the `names` marker exists. Otherwise the first recorded TAB opens it to create it (`_shim_complete.py:924`), and later ones only `utime` it.
  - The `names` marker is backdated before each recorded TAB, as `_warm_tab` does (`test_completion_templated.py:644`), and each recording proves it took the stat leg.
  - For the `tests`/`markers` profile, the `tests` marker is fresh, so no TAB finds the refresh due and spawns a collect child that changes the next recording.
  - The helper drops only its own report-file open (`<name>.ops.json`). It keeps every real path and multiplicity.
- **One TAB per sub-variant**, on the same warm cache as its profile's measured representative. The validation is chosen in `_answer_items` (`_shim_complete.py:1192-1195`), outside the kind dispatch, so a whole TAB exercises it.
- **Assertions:**
  - the sub-variant's recorded ops equal the representative's;
  - no denied root is in the child's `sys.modules` (a fresh process, so the check is real);
  - the answer is right, and `none` answers exactly `[]`.
- **Positive controls:** an injected extra `os.stat` and an injected forbidden import each make the test fail.
- **What it does not see:** the wrapper counts `os.stat` only. A new `os.lstat` or a `pathlib` stat would pass the unit test, but the measured representatives still count it under strace, if it is on their path.

`echo` has no production parameter today, so it is not mapped. Its first appearance fails the coverage test until someone maps it.

### The remote-path hit (maintainer, 2026-11-23)
The gate measures the warm-cache cost of providing a remote path: everything from the TAB press to the printed list of remote entries, with the listing already cached.

Today the site is declared `live`. The shim hands over, and the full CLI does four things before it reads the cached listing (`src/otto/cli/remote_completion.py:193-260`):
1. bootstraps the projects;
2. runs the reservation gate;
3. builds the lab;
4. checks the host's term.

The first baseline records that whole path. If a later change answers cached listings sooner, the ceiling comes down by measurement.

**Reservations.** The generated project declares no `[reservations]`, so the gate returns at once (`remote_completion.py:506-507`), and the surface measures the unreserved hit. When reservations are configured and the reservation answer is cached, the hit is still entirely local:
- a second lab load (`_required_for`, `:406`);
- the username resolution;
- one sidecar read (`cached_reservation_ok`, `:515`).

No backend is built or contacted. So the reserved hit is measurable and does not depend on the backend.

**A second surface, `completion_remote_path_hit_reserved` (maintainer, 2026-11-23).**
- Same TAB, with `[reservations]` declared.
- The post-seed writer stores both the listing and a covering reservation through `store_reservations`.
- The project knobs (`[reservations]` and any host added for the surface) are per-surface fixture options, so the other surfaces' projects, and the existing ceilings, do not change.
- The backend is a concrete non-null one, the JSON backend. `expect_not_loaded` adds `otto.reservations.registry`, which only building a backend loads. It cannot deny the `otto.reservations` root, because `check`, `factory`, `identity` and `null_backend` load on every path (`remote_completion.py:498`).
- The writer derives the username and the required set through otto's own resolvers (`resolve_username`, `_required_for`). It requires that set to be non-empty: lab `unix` declares test1–3 (`tests/_fixtures/goldens/tech1_lab.json`). It then stores one active reservation per required resource, with no start or end.
- **Control:** the same surface without the stored reservations fails its verdict. This proves the `[reservations]` table applies, and that the surface does not quietly measure the unreserved hit.

The unreserved surface stays too: only it would catch a lost early return, which would charge every user without reservations the second lab load. Only a reservation-cache miss stays unmeasured, beside the listing miss, because its cost is the backend's.

**Fixture:**
- The fixture uses the generated project's closed-port `budget-ssh` host (`ssh_lab`). It is in `unix`, has `term="ssh"` and sits on loopback, so a miss can never dial a real address.
- The fixture sets `OTTO_LAB=unix`.
- The seed runs `otto --help` with the completion variables (`_OTTO_COMPLETE`, `COMP_WORDS`, `COMP_CWORD`) **removed**, keeping the same project, home and lab. Otherwise the shim would still complete (`src/otto/_shim.py:120`) and attempt a live listing.
- The listing is written after the seed by the post-seed step, through `otto.config.remote_completion_cache.store_listing()`. The directory key comes from `otto.cli.remote_completion.split_incomplete` (`"~"` for an empty fragment), so the format and the key are each decided in one place.

**Scaling pins:** `completion_host_warm` gets a new O(1)-in-corpus pin, built like `completion_project_warm`'s (`test_import_budget.py:877`). Its replaced tracked surface had none. The remote hit builds a lab, so it gets one too, and so does the reserved hit. Its listing is seeded separately for each of the two measurements.

## Harness changes (`scripts/import_budget.py`)
- **A per-surface seed environment.** A surface may remove variables from its seed's environment. This is one field on `Surface`, applied inside the existing `measure_surface` sequence. It is not a second measurement sequence.
- **A post-seed step.** A surface may name a writer that runs between the seed and the measured run, after `_prove_port_closed`. It runs as a child process in the measured venv with the surface's environment, so it resolves the same home and projects.
  - Each writer calls otto's own writer: `store_listing`, or `record_docker_containers` with the projects from `otto.config.project.discover()`, as `src/otto/cli/docker.py:258` does. It never writes JSON by hand.
  - A writer that writes nothing, because it was given no projects or the fingerprint was ephemeral, is a failure.
  - Any failure of the step becomes the surface's `ending` verdict, so the release delta reports a base tag that lacks a writer instead of crashing.
- **One validity verdict.** These checks feed the one verdict that `check_surface`, `make import-snapshot` (`_update_every_minor`) and the JSON delta reports all use today through `check_exit`:
  - `expect_loaded`, now a tuple;
  - the new `expect_not_loaded`;
  - the new `expect_answer`.

  Modules match by root, so `typer` also matches `typer.*`.

  `expect_answer` has two forms: one or more whole candidate lines that must be present, or an exact candidate set (used for the past-separator surface). It reads the stdout that `_run_child` already captures (`:1561-1570`), with no second invocation.

  Tests show that a forbidden import and a missing candidate each make regeneration refuse.
- **The deny list for shim surfaces:** `typer`, `rich`, `pydantic`, `otto.cli`, `otto.config`. Typer vendors click, so `click` would prove nothing.
  - The builder confirms each root really is absent on the shim's path today. `tests/unit/test_shim.py` pins the warm module set in-process.
  - The list lives once, in the harness. The docstring at `_shim_complete.py:12-14` points at it instead of restating it.
- **Existing ceilings do not move.** The new fields default to "no check", so the 2 existing surfaces and every verb surface measure exactly what they do today. A test shows that the existing ceilings are unchanged.
- **Bookkeeping.**
  - Add the 6 new keys to `KNOWN_SURFACES` and `REPO_SURFACES` in `scripts/import_budget_delta.py`, to the `gated` set in `test_surfaces_table_well_formed`, and to all 6 ceilings files via `make import-snapshot`.
  - Remove `tracked_tab_warm_templated` from its sites.

## The coverage test
**One map.** The site-to-profile map lives once, in `scripts/import_budget.py`, next to `SURFACES`. The test derives from it. It asserts that every surface the map references exists and is gated.

**The walk.**
- It reuses the command-tree walker in `tests/unit/shim/test_differential.py` (`_walk`, `:309`). Two gaps there: `_walk` stops at scoped groups, and `_corpus` (`:370`) uses 4 hard-coded host ids and returns strings.
- So the builder extracts one enumeration over every `tree["host_classes"]` entry that keeps each site's class identity. The differential test and the coverage test both call it. A test with a parameter that exists on one host class only proves it reaches every class.
- It reuses that file's `EXPECTED_HANDOVER_REASONS` (`:27`).
- Each site is keyed by a canonical identity: command path, host class, parameter name.

**Classification.** A site's profile depends on its declaration and its shape, not on its kind alone. A site is a handover when its declared source is `live`, when it has `live_past_sep` (past the separator), or when its shape is one the shim hands over (`nargs > 1`, a multi-value option; `_shim_complete.py:768,796,803`). Every other site is a shim profile, with its validation profile as the surface:
- `tests` and `markers` map to `completion_test_warm`;
- `observed` maps to `completion_docker_observed_warm`;
- `payload`, `static` and `none` map to `completion_host_warm`, together with the sub-variant test.

**Handover sites.** An exact list maps each handover site to its surface:
- `tunnel remove TUNNEL_ID` maps to the handover surface;
- the `tunnel add/check --hosts` sites past the comma map to the past-separator surface;
- the `get`/`put` remote-path sites map to the remote hit;
- shape-driven handovers (`nargs > 1`, multi-value options): none exists today. The first one fails the test until it is mapped, as for `echo`.

**It fails on:**
- a kind or shape with no mapping;
- a handover site missing from the list;
- a listed site that no longer exists;
- a listed site that is no longer a handover.

## Decided twice (noted, not changed)
The PR body and `docs/architecture/subsystems/completion.md` list the places where the shim and the completers decide the same thing twice. These are the survey's list:
- **Completer bodies against the shim's interpreter of the same declaration:**
  - `filter_logins` and `_host_logins`
  - `selected_lab_names` and `selected_labs`
  - `lab_scoped_host_ids` and `_lab_host_set`
  - `_default_parent_for_tab` and `_default_parent`
  - `_image_ref_completer` and `_observed_values`
  - `_fresh_lists` and `_fresh_sub_entry`
  - `_log_level_completer` and `_static_values`
  - `link_attaches` and `_link_attaches`
- **Two sources for the links site:** the link completer's live body ignores the cached `links` key.
- **The past-the-comma handover** is decided in two places.
- **Separated-list completion:** `complete_separated_list` and the shim's copy of it.
- **Caches the shim never reads:** the remote-path and `tunnel remove` sites each have one.
- **Four walks of the context chain.**
- **Host-class verbs:** the serialised views and `HostGroup.scoped_names`.
- **Staleness:** the shim's stat pass against the Python readers' digests.

## Docs
- `docs/architecture/subsystems/completion.md`. Its kind list gains `observed`. Its surfaces paragraph names every surface, the deliberately unmeasured paths (the listing miss and the reservation-cache miss), and the decided-twice list.
- `docs/architecture/subsystems/completion-cache.md`, for its pins.
- `docs/architecture/startup-performance.md`: one sentence on the TAB profiles.
- `docs/contributing.md`'s import-budget section: a new completion path needs a surface or a map entry, and new surfaces need `make import-snapshot` over every minor.
- The `scripts/import_budget.py` docstring and its per-surface comments.

## Left to the builder
- The exact field names and helper shapes.
- The writers' entry points.
- The comment text.
- Confirming the deny list against today's shim path.

## Approaches considered
1. **One surface per cost profile, plus a whole-TAB sub-variant test (chosen).** The measured surfaces count every file operation on each profile's path. The unit test proves that each sub-variant takes the same path as its representative. It needs 6 new surfaces and one unit test of a few child processes.
2. **One surface per declared kind** (Astra's first option). This would mean 4 more surfaces (`static`, `none`, `markers`, `payload` split out), so 24 more baselines and more `make import-snapshot` time. Each of them would measure the same validation over again.
3. **One surface per site, about 55 of them.** Mostly duplicates; `make import-snapshot` would take several times as long.
4. **The shim answers the remote-path hit, which is then measured.** Rejected: it changes behaviour, and the maintainer asked to measure today's warm-cache path.
