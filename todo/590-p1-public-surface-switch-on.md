# #590 P1: switch the public-surface declaration on

**Status (2026-10-09):** P1 landed as 14caec5b on top of the erratum f37ff1e9, and spec 4
commit 4 (`OttoContext.scopes` never widens silently) is done. What is left is the other
"After P1" commits: spec 2 commits 4-6, spec 3a commits 4-6, spec 3b commits 3b-1 to 3b-5, and the marked commit
that adds the writer refusals P1 deferred (`Capture.save`, `archive_edit`).

P0 built the tooling and left it dormant: no `api/public.toml`, the golden is
still v1, nothing gates on the validator. P0 was rebuilt (dump spec
`docs/superpowers/specs/2026-10-05-api-dump-design.md` §12): the v2 golden
is the API dump, and there is no `name`-line format. P1 is the one commit
that switches it on (spec `docs/superpowers/specs/2026-10-04-public-api-manifest-design.md`, §7
"Cutover" and §6). This is that commit's checklist. Line numbers are as of
the end of P0.

## Switch-on checklist

- [x] ✅ (14caec5b) (a) `git mv scripts/api_public_preview.toml api/public.toml` and add
  `otto.lab`; drop the "not before P1" assertion at
  `tests/unit/scripts/test_api_teaching.py:409` and repoint the preview paths
  (`Makefile:1549-1550`, `tests/unit/scripts/test_api_teaching.py:408`, `:420`).
- [x] ✅ (14caec5b) (b) `make api-snapshot` / `make check-api-snapshot` pass
  `--manifest api/public.toml` (`Makefile:1540-1546`).
- [x] ✅ (14caec5b) (c) The golden test, the floor test and the two red surface tests read
  the dump (`scripts/api_regen.generate_worktree`) instead of
  `compute_surface` (`tests/unit/api_snapshot/test_public_api_snapshot.py`:
  golden :41, floor :138, red :321 and :386). Then commit the first dump; its commit message is the
  conversion's (dump spec §8).
- [x] ✅ (14caec5b) (d) Delete the v1 producer: docs stop producing lines
  (`scripts/api_snapshot.py`: `IMPORT_LINE` :78, `_extract_lines` :83,
  `documented_deep_imports` :209, `SURFACE_FLOOR` :308, `_HEADER` :320).
- [x] ✅ (14caec5b) (e) A pytest module that runs the agreement checks over the live
  manifest (spec §6 "Agreement tests"; `scripts/api_agreement.py:405`
  `agreement_failures`).
- [x] ✅ (14caec5b) (f) A gating (no `--report`) Make target for `scripts/api_teaching.py`,
  wired into CI and the pre-push lanes (`Makefile:1548-1550`,
  `scripts/gate_fresh.py:82` `GATED_TARGETS`); `--root` exists
  (`scripts/api_teaching.py:940`). Once it gates, `main` must report a bad
  manifest (`ManifestError`) or a failed namespace report (`AgreementError`)
  as a `FAIL <reason>` line and exit 1, not a traceback, as `_main_dump` does
  (`scripts/api_snapshot.py:407`).
- [x] ✅ (14caec5b) A `python` fence holding a `def` whose docstring has a doctest keeps
  only prompt lines after the first `>>> `, so a `return` or an import below
  the closed docstring is dropped without a finding. Count prompt-free lines
  after a closed docstring as code again (`scripts/api_teaching.py`
  `_block_source`; the one live case is `docs/contributing.md:804`).
- [x] ✅ (14caec5b) (g) Rewrite the v1-centric text: `docs/contributing.md:821-823` (setup
  names are no longer taught), `:340-352` and `:910-917` (the docs no longer
  declare the surface), and `scripts/check_breaking_marks.py`'s `RULE`
  (:119) and module docstring (:1-93).
- [x] ✅ (14caec5b) (h) Say in the P1 docs that merging `main` into a v2-era PR after a
  marked removal landed on `main` needs its own mark: a v2 merge is judged
  against its first parent.
- ✅ (i) `_main_dump` never tracebacks: a failed namespace report, an unreadable
  golden and a bad manifest are each a `FAIL <reason>` line and exit 1, and a
  passing `--check` prints `api snapshot: OK` (`scripts/api_snapshot.py:407`;
  done in P0).
- [x] ✅ (14caec5b) `_main_dump --update` writes a dump of any size: give it a floor like
  v1's `SURFACE_FLOOR` (a minimum binding count), or say why the dump needs
  none (`scripts/api_snapshot.py:441`).

- [x] ✅ (14caec5b) **Stability page, banners and a site-wide notice** (owner decision,
  2026-10-05):
  - a stability page states each tier's promise and the entry-tier rule;
  - each module whose public symbols are all non-stable gets a banner;
  - a site-wide notice says otto's API is provisional before 1.0;
  - stable is never marked.

  Per-symbol badges wait for the first mixed module: while every symbol
  carries the same tier, a badge on each of them carries no information.

  The rule, the docs test and the later per-symbol and experimental work are
  in `todo/590-api-stability-visible.md`.

## Spec 4's P1 work (repo and scope inputs)

Spec `docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md` §2, §9 and §10.

- [x] ✅ **Before P1:** commit 2 (6c817717), `feat(config)`: `fleet_of_interest` in `otto.config.fleet`, the
  shared membership-flag helper (`OttoContext.all_hosts` refactored onto it), and the differential
  test (spec 4 §3, §8).
- [x] ✅ (14caec5b) Fold `get_repos`, `get_ordered_repos` and `get_env` into `src/otto/bootstrap.py` (it already
  has `is_bootstrapped` and `get_completion_names`; keep the wrapper's `get_completion_names`
  docstring) and delete `src/otto/config/bootstrapped.py`. Internal callers and test patch targets
  use `otto.bootstrap` (364 lines in 33 src and 68 test files at `d0839893`, plus
  `tests/repo3/tests/test_embedded_coverage.py:39` and commit 2's patch target).
- [x] ✅ (14caec5b) A static guard refuses any old spelling of the five accessors, relative imports included.
  It is the primary defence for the six swallowed-import sites (spec 4 §2.1): `config/scope.py`
  `scope_for_repo`, `lifecycle.py`, `declared.py`, `context.py` `scopes` and `_detect_cov`, and
  `cli/remote_completion.py`. Those six also run unpatched against a real bootstrap.
- [x] ✅ (14caec5b) `otto.config`'s `__all__` and lazy table drop the five accessors (ten names remain).
  `otto.bootstrap`'s first `__all__` is the twelve names in spec 4 §2; `otto.lab` adds
  `EmptySelectionError` and `fleet_of_interest`.
- [x] ✅ (14caec5b) The lazy-getter guard (`tests/unit/test_no_import_time_lazy_exports.py`) covers an explicit
  set of `otto.bootstrap` getters; `tests/_fixtures/_lazy_exports.py`,
  `tests/unit/test_patch_targets.py`, `tests/unit/test_lazy_packages.py`'s expected message and
  `docs/contributing.md`'s two rules follow.
- [x] ✅ (14caec5b) Ratchet re-baseline (S-5, owner-approved): `tach.toml` and
  `tests/unit/test_import_cycle_ratchet.py` gain coverage/docker/host/lifecycle/monitor.live/
  project/suite → `otto.bootstrap` and lose `otto.lifecycle → otto.config`;
  `docs/architecture/modules.md` is regenerated.
- [x] ✅ (14caec5b) The preview declaration (`scripts/api_public_preview.toml`) carries spec 4's delta and drops
  its `pending = "spec 4 …"` note.
- [x] ✅ (14caec5b) Docs build: `docs/api/config.rst` drops `otto.config.bootstrapped`; `src` roles naming
  `otto.config.bootstrapped.*` are re-pointed; `docs/api/bootstrap.rst` renders the public
  `__all__`, with an Internals `:ignore-module-all:` entry for `discover`, `DiscoveryResult` and
  `set_completion_names`.
- [x] ✅ (14caec5b) Re-point the docs: `getting-started/boards-of-interest.md` (teaches `fleet_of_interest`),
  `cookbook/python-library.md:535`, `configuration/settings.md:399`,
  `contributing.md:582-611` (patch targets), the `EmptySelectionError` roles at
  `cli/run/defaults.md:235` and `cookbook/authoring/writing-instructions.md:89`,
  `architecture/subsystems/bootstrap.md`, the `otto.config` row of `architecture/overview.md`, and
  the Project scope section of `configuration/lab-config.md`.
- [x] ✅ (14caec5b) The footer: retired `otto.config.scope:{resolve_scopes,scoped_ids}` and the five
  `otto.config` accessors; added `otto.lab:{fleet_of_interest,EmptySelectionError}` and the
  `otto.bootstrap` names; the ratchet and `tach.toml` delta.
- [x] ✅ (`fix(context)!: scopes never widens silently`) **After P1**, its own commit
  (spec 4 §4). Rule 1: verdicts over `get_repos()`; D3 sees a skipped first repo; `status --full`
  lists skipped repos. Rule 2: the classifier is shared with `check_repos`; the refusal is cached;
  the readers' table; remote completion carries `-I`. The unit tests that relied on the removed
  `except` are named and migrated.

## Spec 2's P1 work (run-state contracts)

Spec `docs/superpowers/specs/2026-10-06-run-state-contracts-design.md` §2, §5 and §6.

- [x] ✅ **Before P1:** commit 2 (b0243c4d), `fix(cli)`: the CLI's resets run on Click's `call_on_close`;
  `_cli_token`, `_variant_token`, `set_cli_context`, `set_cli_variant` and `reset_cli_context`
  are deleted; the root conftest stops snapshotting them (spec 2 §5).
- [x] ✅ (14caec5b) `otto.context`'s first `__all__`: the eleven names in appendix F (spec 2 §2). The footer notes
  the narrowed `from otto.context import *`.
- [x] ✅ (14caec5b) Its docs (spec 2 §6.1): `docs/api/context.rst` renders the `__all__` plus an Internals entry
  (`:ignore-module-all:`) for the names the architecture docs still link; the
  `pending = "spec 2 …"` note in `scripts/api_public_preview.toml` is removed.
- [ ] **After P1**, each its own marked commit (spec 2 §6): commit 4 (`otto.invocation`,
  `RunPolicy`, `HostResolver`, `ContextBinding`, the host layer off `otto.context`), commit 5
  (loop-owned registrations, counted boundaries), commit 6 (`ctx.repos`, `scopes_of`, S-5's edges
  retired per an import-site inventory; after spec 4's commit 4).

## Spec 3a's P1 work (registries)

Spec `docs/superpowers/specs/2026-10-06-registry-catalog-design.md` §8 and §9.

- [x] ✅ **Before P1:** commit 2 (708cd4c5), `fix(host)`: #601. Embedded hosts build through the term registry;
  `_connection_factory` and its teaching (`extending-backends.md:136`) are deleted, so P1's
  underscore-rename inventory (dump spec §7.4) no longer contains it. The golden is regenerated,
  with a mark only if the producer records the init keyword (spec 3a §5).
- [x] ✅ (14caec5b) `otto.registry`'s first `__all__`: the four names in appendix F.
- [x] ✅ (14caec5b) The three retirements in the appendix G addendum: `get_inventory_backend_class`,
  `get_creds_backend_class` and `reset_half_ported_warnings` leave their facades' `__all__`. The
  two teaching sentences (`inventory-backends.md:103`, `creds-backends.md:99`) are rewritten to
  say an unregistered name raises when otto builds the store; no replacement is taught. The
  footer lists all three.
- [ ] **After P1**, each its own marked commit (spec 3a §9): commit 4 (strict engine, legacy list,
  conformance and discovery suites, isolation through `instances()`), commits 5.1–5.13 (one per
  seam group), commit 6 (allowances and `origin=` deleted, `@final`, ast-grep rules on).

## Spec 3b's P1 work (host construction)

Spec `docs/superpowers/specs/2026-10-06-host-construction-design.md` §2 and §7.

- [x] ✅ (14caec5b) `otto.host.__all__` gains `HostSpec`, `UnixHostSpec`, `EmbeddedHostSpec` and `host_identity`;
  `otto.models.__all__`, its typing exports and lazy bindings drop the three specs (appendix G
  addendum). Docs re-point.
- [ ] **After P1**, each its own commit (spec 3b §7), marked except 3b-5: 3b-1 (structural gaps,
  after 3a 5.1 and 5.8), 3b-2 (field kinds, coverage rule, profile-eligible inputs; after 3a 5.9),
  3b-3 (the pipeline, readiness, power preparation; after 3a 5.12), 3b-4 (one stock builder),
  3b-5 (`refactor`: the loader's duplicate validation goes).

## The dump's P1 work (dump spec)

- [x] ✅ (14caec5b) **Producer-refusal triage.** Run `make api-surface-report` on the
  pre-cutover tree. Every refusal is fixed or declared before the switch.
  At the end of P0 it reports 22:
  - 6 hidden obligations, each renamed to a public name (§7.4):
    `Host._login`/`_logout`; `ShellSession`'s
    `_open`/`_read_until_pattern`/`_write`; the file transfer's abstract
    `_run_get`/`_run_put`, reported on `BaseFileTransfer`,
    `UnixFileTransfer` and `EmbeddedFileTransfer` (twice: it is bound in
    `otto.host` and `otto.host.transfer`);
  - 10 unreadable signatures of C types bound under `--assume-dir`
    (`ContextVar`, `Token`, `datetime` x4, `timedelta` x2, `timezone`
    x2). They are not in `builtins`, so `@builtin` does not cover them:
    stop binding them in a declared namespace, or amend the dump spec;
  - 5 pytest `FixtureFunctionDefinition` members on
    `otto.suite:OttoFixturesPlugin`;
  - 1 `typing.Protocol` binding that cannot be recorded.

  Also watch for undeclared enum defaults ("declare the enum", §2.4) and
  provenance refusals.
- [x] ✅ (14caec5b) **The rename inventory** (§7.4), the sixth P1 footer inventory, frozen
  from the pre-cutover tree. It holds every underscore name in a declared
  class's `abstract`/`requires` set, plus every `taught-private-member`
  finding on the pre-cutover corpus (6 at the end of P0:
  `BaseFileTransfer._run_put`/`_run_get` overridden,
  `BaseFileTransfer._dispatch_per_file` used twice,
  `ZephyrFrame._region_before_end` used twice). Add a test that the footer
  names every entry.
- [x] ✅ (14caec5b) **The `Host` conversion** (§8). Declare `otto.host`. Every v1 `Host`
  line maps to `member otto.host:Host.<m>`;
  `check_breaking_marks.conversion_breaks` already judges them.
- [x] ✅ (14caec5b) **Formats** (§13). Declare each "yes" row of §13.4 in
  `api/public.toml`. Each `reads`/`writes` constant is a literal list in a
  dependency-light module. Wire every acceptance, dispatch and emission path
  to the constant, including the browser's monitor-export reader. Add the
  §13.5 conformance samples and tests: every `reads` version gets a
  populated sample through every reader; every `writes` version gets an
  emission test; every mutable format gets a mutation test on a copy of
  each older sample.
- [ ] **After P1, one marked contract commit adds the writer refusals dump spec §13.1 asks for, which P1 leaves out under S-1 (ruling X21):** `Capture.save` refuses a `schema` outside `CAPTURE_WRITE_VERSIONS` (it flips FORMATS-1's `test_capture_save_writes_the_stamp_it_holds` to a refusal test, and `test_the_manual_store_rewrite_of_each_read_sample_keeps_it` regains a refusal branch for a version otto reads but does not write); `otto.monitor.archive_edit` refuses an archive whose `user_version` is outside `MONITOR_DB_WRITE_VERSIONS` (it flips FORMATS-3's `test_a_review_edit_checks_no_version_yet`, and `test_a_review_edit_on_a_copy_of_each_archive_keeps_it` regains the same refusal branch), and review mode serves such an archive read-only through the server's existing 403 "editing impossible" path (`server.py:432`) instead of a 500.
- [x] ✅ (14caec5b) **The version-invariance lane** (§6). Add a CI job that produces
  HEAD's dump on 3.10-3.14, each under a different `PYTHONHASHSEED`, and
  compares it byte for byte with the committed dump. The fixture half
  already runs in the nox matrix
  (`tests/unit/scripts/test_api_dump_invariance.py`).
- [x] ✅ (14caec5b) **CI's v2 header check** (`.github/workflows/ci.yml:437`, `:439`) also
  checks line 2, `# producer-schema <n>`.
- [x] ✅ **The erratum: a per-release correction event** (f37ff1e9) (owner decision, 2026-10-05,
  after a Codex design review). A mistake can reach `main` unmarked, through
  `git push --no-verify` or a PR whose red CI was ignored. CI flags it only once,
  because the next push's range starts after that commit. Published history is
  never rewritten. Re-marking the original commit afterwards cannot work either:
  if it already shipped before the last tag, the current release would still
  miss the bump and the note. So the correction is its own release event.
  - **The shape.** An ordinary commit carries the mark itself (`!` or
    `BREAKING CHANGE:`), plus a `Corrects: <full sha> <original subject>` footer.
    It is a normal marked commit, so squash, rebase and cherry-pick preserve its
    meaning. The footer only links it to the commit it repairs.
  - **One event model.** `check_breaking_marks.py`, `scripts/release_bump.py`
    (`is_breaking_commit`) and the cliff changelog all read the same validated
    list of events: marked commits plus corrections. Whatever release contains the
    correction takes the bump, and its notes list the correction under the
    original subject.
  - **What may be corrected.** Any change to a non-experimental symbol that should
    have been marked: one with a dump finding, or a behavioural break with no
    finding. The checker refuses a `Corrects:` whose target is not an ancestor. A
    target that already carried a mark may still be corrected, because a marked
    commit can carry a second, unmarked break. A lifecycle breach, such as a
    protected removal with no deprecation window, is never correctable: it is
    reported as a breach (`todo/590-api-stability-visible.md`).
  - **Duplicates** are keyed by target and correction kind (breaking, or, once
    tiers are per symbol, a missed promotion acknowledgement). A promotion-only
    correction causes no breaking bump.
  - **Tests:**
    - a correction after a tag still bumps the next release;
    - every refusal case;
    - a correction brought in by a merge;
    - a cherry-picked correction whose target is not an ancestor is refused.
  - **Docs:** document it in `docs/contributing.md` (Branching and commits) as the
    one way to repair a mark after the fact.

## File-operation accounting (spec §7)

- [x] ✅ (14caec5b) Run `scripts/import_budget.py --report-json` on P1's base and tip under
  Python 3.10; record the `file_ops` and `workspace` delta for every surface in
  the hand-back and the commit body, with the packages behind any growth.
- [x] ✅ (14caec5b) Zero growth on `import_otto`, `version_repo`, `completion_repo_warm`,
  `completion_repo_handover` and `help_repo_warm`; growth elsewhere only where
  the spec names the surface and says why.
- ~~A surface that shrank: regenerate the ceilings in the same commit (`make import-snapshot`)
  on every gated interpreter (3.10-3.14).~~ Overruled by the owner (2026-10-06): no ceiling
  edits in the #590 series; the committed ceilings stay the reference.
- [x] ✅ (14caec5b) Every import site P1 adds or switches imports the defining module, never
  a public facade (D1: fleet names come from their implementing module, not
  `otto.lab`).

## Closed in P0

- ✅ Two marked commits could roll v2 back to v1 (delete the v2 golden, then
  add a v1 golden under the v1 rules), and a merge from a v1 first parent
  (`git merge -s ours`) could drop the v2 golden its other parent carried.
  Spec §6 now refuses both, marked or not, as a rollback is refused
  (`scripts/check_breaking_marks.py` `evaluate_commit`).
- ✅ P0 rebuilt on the API dump (dump spec §12): records, per-commit
  regeneration, the call invariant, D-5's validator check and versioned
  formats are built, and dormant. Cost per commit, in file ops
  (dump spec §5.5): 28,180 cold, 24,299 for a new dependency key, 18,883 warm;
  the archive holds only regeneration's inputs (67,485 ops for the whole tree).
