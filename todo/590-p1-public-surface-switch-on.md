# #590 P1: switch the public-surface declaration on

P0 built the tooling and left it dormant: no `api/public.toml`, the golden is
still v1, nothing gates on the validator. P0 was rebuilt (dump spec
`docs/superpowers/specs/2026-10-05-api-dump-design.md` §12): the v2 golden
is the API dump, and there is no `name`-line format. P1 is the one commit
that switches it on (spec `docs/superpowers/specs/2026-10-04-public-api-manifest-design.md`, §7
"Cutover" and §6). This is that commit's checklist. Line numbers are as of
the end of P0.

## Switch-on checklist

- [ ] (a) `git mv scripts/api_public_preview.toml api/public.toml` and add
  `otto.lab`; drop the "not before P1" assertion at
  `tests/unit/scripts/test_api_teaching.py:409` and repoint the preview paths
  (`Makefile:1549-1550`, `tests/unit/scripts/test_api_teaching.py:408`, `:420`).
- [ ] (b) `make api-snapshot` / `make check-api-snapshot` pass
  `--manifest api/public.toml` (`Makefile:1540-1546`).
- [ ] (c) The golden test, the floor test and the two red surface tests read
  the dump (`scripts/api_regen.generate_worktree`) instead of
  `compute_surface` (`tests/unit/api_snapshot/test_public_api_snapshot.py`:
  golden :41, floor :138, red :321 and :386). Then commit the first dump; its commit message is the
  conversion's (dump spec §8).
- [ ] (d) Delete the v1 producer: docs stop producing lines
  (`scripts/api_snapshot.py`: `IMPORT_LINE` :78, `_extract_lines` :83,
  `documented_deep_imports` :209, `SURFACE_FLOOR` :308, `_HEADER` :320).
- [ ] (e) A pytest module that runs the agreement checks over the live
  manifest (spec §6 "Agreement tests"; `scripts/api_agreement.py:405`
  `agreement_failures`).
- [ ] (f) A gating (no `--report`) Make target for `scripts/api_teaching.py`,
  wired into CI and the pre-push lanes (`Makefile:1548-1550`,
  `scripts/gate_fresh.py:82` `GATED_TARGETS`); `--root` exists
  (`scripts/api_teaching.py:940`). Once it gates, `main` must report a bad
  manifest (`ManifestError`) or a failed namespace report (`AgreementError`)
  as a `FAIL <reason>` line and exit 1, not a traceback, as `_main_dump` does
  (`scripts/api_snapshot.py:407`).
- [ ] A `python` fence holding a `def` whose docstring has a doctest keeps
  only prompt lines after the first `>>> `, so a `return` or an import below
  the closed docstring is dropped without a finding. Count prompt-free lines
  after a closed docstring as code again (`scripts/api_teaching.py`
  `_block_source`; the one live case is `docs/contributing.md:804`).
- [ ] (g) Rewrite the v1-centric text: `docs/contributing.md:821-823` (setup
  names are no longer taught), `:340-352` and `:910-917` (the docs no longer
  declare the surface), and `scripts/check_breaking_marks.py`'s `RULE`
  (:119) and module docstring (:1-93).
- [ ] (h) Say in the P1 docs that merging `main` into a v2-era PR after a
  marked removal landed on `main` needs its own mark: a v2 merge is judged
  against its first parent.
- ✅ (i) `_main_dump` never tracebacks: a failed namespace report, an unreadable
  golden and a bad manifest are each a `FAIL <reason>` line and exit 1, and a
  passing `--check` prints `api snapshot: OK` (`scripts/api_snapshot.py:407`;
  done in P0).
- [ ] `_main_dump --update` writes a dump of any size: give it a floor like
  v1's `SURFACE_FLOOR` (a minimum binding count), or say why the dump needs
  none (`scripts/api_snapshot.py:441`).

- [ ] **Stability page, banners and a site-wide notice** (owner decision,
  2026-10-05):
  - a stability page states each tier's promise and the entry-tier rule;
  - each module whose public symbols are all non-stable gets a banner;
  - a site-wide notice says otto's API is provisional before 1.0;
  - stable is never marked.

  Per-symbol badges wait for the first mixed module: while every symbol
  carries the same tier, a badge on each of them carries no information.

  The rule, the docs test and the later per-symbol and experimental work are
  in `todo/590-api-stability-visible.md`.

## The dump's P1 work (dump spec)

- [ ] **Producer-refusal triage.** Run `make api-surface-report` on the
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
- [ ] **The rename inventory** (§7.4), the sixth P1 footer inventory, frozen
  from the pre-cutover tree. It holds every underscore name in a declared
  class's `abstract`/`requires` set, plus every `taught-private-member`
  finding on the pre-cutover corpus (6 at the end of P0:
  `BaseFileTransfer._run_put`/`_run_get` overridden,
  `BaseFileTransfer._dispatch_per_file` used twice,
  `ZephyrFrame._region_before_end` used twice). Add a test that the footer
  names every entry.
- [ ] **The `Host` conversion** (§8). Declare `otto.host`. Every v1 `Host`
  line maps to `member otto.host:Host.<m>`;
  `check_breaking_marks.conversion_breaks` already judges them.
- [ ] **Formats** (§13). Declare each "yes" row of §13.4 in
  `api/public.toml`. Each `reads`/`writes` constant is a literal list in a
  dependency-light module. Wire every acceptance, dispatch and emission path
  to the constant, including the browser's monitor-export reader. Add the
  §13.5 conformance samples and tests: every `reads` version gets a
  populated sample through every reader; every `writes` version gets an
  emission test; every mutable format gets a mutation test on a copy of
  each older sample.
- [ ] **The version-invariance lane** (§6). Add a CI job that produces
  HEAD's dump on 3.10-3.14, each under a different `PYTHONHASHSEED`, and
  compares it byte for byte with the committed dump. The fixture half
  already runs in the nox matrix
  (`tests/unit/scripts/test_api_dump_invariance.py`).
- [ ] **CI's v2 header check** (`.github/workflows/ci.yml:437`, `:439`) also
  checks line 2, `# producer-schema <n>`.
- [ ] **The erratum: a per-release correction event** (owner decision, 2026-10-05,
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

- [ ] Run `scripts/import_budget.py --report-json` on P1's base and tip under
  Python 3.10; record the `file_ops` and `workspace` delta for every surface in
  the hand-back and the commit body, with the packages behind any growth.
- [ ] Zero growth on `import_otto`, `version_repo`, `completion_repo_warm`,
  `completion_repo_handover` and `help_repo_warm`; growth elsewhere only where
  the spec names the surface and says why.
- [ ] A surface that shrank: regenerate the ceilings in the same commit
  (`make import-snapshot`) on every gated interpreter (3.10-3.14).
- [ ] Every import site P1 adds or switches imports the defining module, never
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
