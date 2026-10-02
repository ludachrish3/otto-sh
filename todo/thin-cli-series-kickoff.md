# Thin-CLI series — kickoff briefs for the parallel items

The thin-CLI series moves every rule out of `otto.cli` into the library it
wraps, one verb family per item, so `import otto` users get identical
behaviour and the CLI is a parse, complete, call, render layer. Items 1
(`otto run`, #502), 2 (`otto test` + the coverage report, #505/#506/#507)
and 3 (`otto docker`, #493/#494, pushed at cb23b63f) have landed; item 4 is
done and awaits Chris's squash. Items 5 and 6 remain.

The items below are **independent of each other and of item 3**: each lives
in one subsystem with its own CLI module, tests and doc pages. They can run
concurrently, one session per item, each in its own worktree off
`origin/main`. Run at most three heavy sessions at once: the dev VM's real
bottleneck is `make coverage` on the test bed, which serializes through bed
coordination.

**Serial, after all of the above land (do not start in parallel):**
item 7 (`open_context` / `load_lab_for_repos`, #508) touches every verb's
preamble, and item 8 (options metadata leaves typer, #513) touches every
option declaration. #553 (`otto docker ps`) depends on item 3.

## The contract every item follows

Read first: `docs/architecture/principles.md` "Input rules live in the
library entry point", and the two landed specs as worked examples:
`docs/superpowers/specs/2026-09-29-run-options-own-their-rules-design.md`
(item 2) and `docs/superpowers/specs/2026-09-30-docker-verbs-align-with-docker-design.md`
(item 3, including §7a's four-step recipe for adding a verb).

1. **Process.** Brainstorm (architectural path) → spec in
   `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`, committed ALONE →
   Chris reviews → writing-plans → Chris picks execution (subagent-driven
   has been the choice; Opus reviews per task, Fable whole-branch review at
   the end) → one squash by Chris. Specs commit separately from product work.
2. **Where rules live.** Pure input rules in the library entry point
   (`__post_init__` for an options class, leading validation for
   parameters). I/O preflights in a library `prepare_*` with a check-only
   mode for `--dry-run`. The CLI constructs the library object straight from
   parsed flags, calls, and translates field-named errors at ONE site:
   `otto.cli.invoke.usage_error_from(exc, flags={"field": "--flag"})`.
   Library errors carry a `field` attribute; messages are written so no
   user-supplied text needs rewriting (see item 2's `destination_message`
   lesson: format from fields, never rewrite text).
3. **Container follows the existing library API**, never the verb: no
   dataclass invented to host a hook.
4. **Composition in the library function**, never the leaf: a verb that
   calls another library composes it inside its own library function.
5. **Reports, not None.** A library verb returns something the caller can
   branch on; the CLI's exit code is read from it (`report.ok`), never from
   a counter in the leaf.
6. **The differential.** Each item lands one CLI-vs-library differential
   test (#525) shaped like `tests/unit/cli/test_test_differential.py`: every
   leaf hands the library exactly the parsed flags; every library refusal
   reaches the output in flag spelling; the module docstring records the
   mutation used to prove it turns red.
7. **Exports.** Every new public name is lazy: `_LAZY_ATTRS` +
   `TYPE_CHECKING` import + `__all__`; `make api-snapshot` regenerates the
   golden; the import-budget ceilings are never regenerated (fix the import).
8. **Gates.** Per task: the touched tests + `ruff check` / `ruff format
   --check` on changed files. Once, before hand-back: `make typecheck`,
   `nox -s tests_hostless-3.14`, `make gate-fresh` (clean tracked tree),
   `make coverage` (coordinate the bed with every busy peer session via
   `ListAgents` + a message first; a session waiting on Chris yields). Run
   long gates detached with a sentinel (the Bash tool's 10-minute ceiling
   kills a foreground gate). Zero failures AND zero errors is done.
9. **Conventions.** Commit messages via `git commit -q -F <file>`, trailer
   `Assisted-by: Claude <model that ran>` only. One git command per shell
   call. Never run anything under `tests/integration` or `tests/e2e`
   locally (they reach the lab; `make coverage` runs them). Never `tach
   sync`. No plan coordinates ("Task 3") in src/docs/tests. Main checkout is
   read-only from a worktree. Only Chris pushes and squashes. Fresh
   worktrees need `uv sync`, `make web-install && make web`, and the gcov
   submodule snapshot (see memory `reference_worktree_uv_sync`).
10. **Hand-back.** Branch, commit list, every gate's summary line verbatim,
    bed released, a `squash-message.txt` draft (gitignored
    `docs/superpowers/plans/`) with `Fixes #...` / `Refs #525` and the
    BREAKING paragraph's migration recipe, and the exhaustive list of
    rulings made during execution.

---

## Item 4 ✅ — `otto cov get` / `otto cov clean` / `otto cov report`: one public entry point per verb

**Done** (branch `worktree-cov-get-clean`, awaiting the squash): `get_coverage`,
`clean_coverage` and `run_coverage_report` own their rules, the `llext` kind is
now `embedded` with a real `reset_fn` reset, and one differential covers all
three leaves. Nothing is left in this item.

**Issues:** #536 (refs #525). **Subsystem:** `otto.coverage`, `src/otto/cli/cov.py`,
`src/otto/suite/run.py` (the `--cov-clean` caller).

**What the audit found.** Wider than the issue's "residual `is_dir` and tier
check". `otto cov get` and `otto cov clean` are whole pipelines in
`src/otto/cli/cov.py`:

- `get`: the manual-tier `--ticket` rule, the not-instrumented refusal, the
  repository preflight, output-dir resolution, tester identity, the write
  into the committed manual-capture store, the no-captures refusal and the
  scoped `--clean` live only in `_do_get`. A Python caller of
  `collect_coverage` with a manual tier gets none of them; its captures
  never reach the manual store. The tier error is spelled by rewriting
  text (`spell_flags(str(e), {"tier=NAME": "--tier NAME"})`).
- `clean` has two owners: the library's `clean_remote_gcda` (used by
  `otto test --cov --cov-clean`, silent when nothing is configured or
  matched) and the CLI's `_do_clean` (refuses). In both, a failed per-host
  reset is only logged, so `otto cov clean` exits 0 when every host failed
  (the #494 defect class).
- The CLI's embedded-board guard for clean (`_unix_only_pattern`) is dead:
  the library's per-host clean already skips embedded boards and the runner.
- `report`: a residual `is_dir` check on the output dirs stays in the leaf.

**Shape to aim for (Chris's rulings, 2026-10-01).** Each verb gets its own
public library entry point; the CLI leaf parses, calls one function,
renders its report, and translates field-named errors at the one
`usage_error_from` site.

- `get` → a new `otto.coverage.get_coverage(output_dir, *, tier, ticket,
  note, tester_name, tester_email, clean, repos)` that validates its inputs
  first with field-named errors (tier, ticket), owns every rule above, and
  composes the unchanged `collect_coverage` engine (still what
  `otto test --cov` calls). Returns a report.
- `clean` → `clean_remote_gcda` becomes the single `clean_coverage(repos=None)`
  returning a per-host report (item 3's `HostReport`); `otto cov clean` and
  `otto test --cov --cov-clean` both call it. A failed reset REFUSES in both
  commands, naming the host (`otto test` stops before any test runs).
  ANY inability to clear counters is a failure, never a warning (Chris):
  `otto test --cov`'s post-collection clean fails the run naming the host
  even when every test passed (the captures stay written); `get --clean`
  keeps the captures it wrote and exits 1 naming the host. A missing `[coverage]`
  section refuses in both (today `--cov-clean` silently skips).
- Embedded boards get a real reset: the LLEXT kind overrides its counter
  reset to call a `reset_fn` (default `cov_reset`, mirroring `dump_fn`)
  that wraps embedded-gcov's `__gcov_clear()`, so clean walks embedded
  boards like any other host. A product that does not export it fails its
  reset with a message naming the fix. The `tests/repo3` demo product gains
  the export and the `GCOV_OPT_PROVIDE_CLEAR_COUNTERS` build define.
- The product kind `llext` is renamed `embedded` in the same squash: it is
  already loader-agnostic (Zephyr's `llext-hex` is only the first
  `BinaryLoader`). `kind = "llext"` refuses naming the new kind; "llext"
  stays only where it names Zephyr's format, loader or shell commands.

**Spec:** `docs/superpowers/specs/2026-10-01-cov-verbs-own-their-rules-design.md`.
- `report` → `run_coverage_report` stays its entry point and absorbs the
  leaf's `is_dir` check.
- One differential (#525) with rows for `get`, `clean` and `report`.

**Likely scope creep to refuse:** the coverage report's rendering
behaviour, kmodcov export/check verbs (own series).

---

## Item 5 ✅ — `otto monitor` and `otto test --monitor` share one monitor library

**Done** (branch `worktree-monitor-library`, squashed onto local main): one
`MonitorSession` builds, opens, runs and tears down for `otto monitor --live`,
`otto test --monitor` and the `monitor` fixture; `otto.monitor.live`
(`select_monitor_hosts`, `run_live`) is a nested lab-aware tach module;
`resolve_monitor_tls`, `serve_review` and typed field-named errors live in
`otto.monitor`; the leaf translates at one site behind a differential. The
audit's "TLS used by all three servers" premise changed by ruling: only
`otto monitor` serves a dashboard (the fixture and `--monitor` collect only).
Nothing is left in this item.

**Spec:** `docs/superpowers/specs/2026-10-02-monitor-one-library-design.md`.

**Issues:** #503, #504 (refs #525). **Subsystem:** `otto.monitor`,
`src/otto/cli/monitor.py`, `src/otto/suite/plugin.py`,
`src/otto/suite/monitor_fixture.py`.

**What the audit found.**
- #503: the repo's declared monitor TLS (`[monitor] tls_cert`/`tls_key`)
  is resolved only in `cli/monitor.py:_resolve_monitor_tls`; `otto test
  --monitor` and the `monitor` fixture serve without it.
- #504: `otto monitor` samples `UnixHost` or any host with an `snmp` block;
  the test plugin samples `UnixHost` only, with a wrong comment;
  `_no_monitorable_hosts_message` is defined twice.

**Shape to aim for.** `otto.monitor.factory.monitorable(hosts)` (one
predicate, one message) and a library `resolve_monitor_tls(repos)` with a
typed, field-named error, used by all three servers; the audit's drift
ranking also lists `otto monitor --live` as CLI-only logic (`run_live`
belongs in `otto.monitor`), so the spec should decide whether `--live`'s
orchestration moves in this item or is split out. Differential rows for
`otto monitor` flags. Constraint from the codebase: no threads with
asyncio; the Typer subcommands run inside an event loop.

---

## Item 6 — `otto init`: the doctor and the scaffolder become a library

**Issues:** #497, #498, #499, #500 (refs #525). **Subsystem:**
`src/otto/cli/init.py` (the worst drift in the audit: doctor and
scaffolder are CLI-only), `otto.config.repo`, `otto.cli.init_templates`.

**What the audit found.**
- #497: the doctor validates settings with a partial copy of
  `Repo.parse_settings` (no `compile_lab_sources`), and `_lab_file_groups`
  swallows the compile error; a repo passes `otto init` and fails to load.
- #498: an orphaned schema file is a failure whose stated remedy never
  clears it (neither writer deletes).
- #499: detect and validate disagree on single-file init modules;
  `--all` scaffolds a duplicate.
- #500: `otto init --tests` overwrites an existing `tests/conftest.py`;
  `_scaffold_kmodcov` appends to settings; a scaffolder prints.

**Shape to aim for.** A library module (e.g. `otto.init` or
`otto.project.init`) with a side-effect-free `validate_settings(root) ->
list[str]` shared with `Repo.parse_settings`, one `write_schemas` /
`schema_drift` pair shared with `otto schema export`, one
`importlib`-based init-module predicate for detect and validate, one
write-policy helper every scaffolder goes through, and scaffolders that
return notices. The doctor and the scaffolder each return a report the CLI
renders. Note the item-3 lesson: `otto init`'s scaffold text names other
verbs (`otto docker compose up` after item 3), so rebase before gating.

**Likely scope creep to refuse:** the kmodcov scaffold's content (own series).

---

## Suite follow-ups — two small `RunOptions` / `prepare_run` rules

**Issues:** #534 (bug), #535. **Subsystem:** `otto.suite.run`. Small enough
for the bounded path (short in-chat design, no spec), one worktree, one
squash or two.

- #534: `run_tests` calls `prepare_run(opts)` (which clears overwrite-flagged
  destinations) before `resolve_coverage` decides whether coverage runs; a
  declined coverage run has already emptied the directory. Keep the
  refusal early (`prepare_run(opts, dry_run=True)` before the decision) and
  the clearing mode after it. Pin with a test that a declined coverage run
  leaves `cov_dir` untouched.
- #535: `RunOptions` accepts `cov_report_dir == cov_dir`. One
  `__post_init__` rule (resolved paths equal → `OptionsValidationError`
  naming both fields) and one row in `tests/unit/cli/test_test_differential.py`.
