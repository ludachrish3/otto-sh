# Thin-CLI series — kickoff briefs for the parallel items

The thin-CLI series moves every rule out of `otto.cli` into the library it
wraps, one verb family per item, so `import otto` users get identical
behaviour and the CLI is a parse, complete, call, render layer. Items 1
(`otto run`, #502) and 2 (`otto test` + the coverage report, #505/#506/#507)
have landed; item 3 (`otto docker`, #493/#494) is in flight in the worktree
`docker-use-case-library`.

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

## Item 4 — `otto cov get` / `otto cov clean`: leaf input checks move to the library

**Issues:** #536 (refs #525). **Subsystem:** `otto.coverage`, `src/otto/cli/cov.py`.

**What the audit found.** `src/otto/cli/cov.py` keeps a residual `is_dir`
check on the coverage directory and validates tier names before calling
the library, so a Python caller of `get` / `clean` does not get the
refusals the CLI gives. `otto cov report` already follows the contract
(item 2): `run_coverage_report` owns its destination, `usage_error_from`
spells `DestinationError`.

**Shape to aim for.** Public library entry points for `get` and `clean`
(whatever `otto.coverage` already exposes, extended rather than
duplicated) that validate their inputs first with field-named errors;
`cov.py`'s two leaves become parse, call, render; one `usage_error_from`
arm with the flag map; differential rows for `get`, `clean` and `report`.
Check whether `otto.coverage.tiers` and the tier-name spelling
(`spell_flags(str(e), {"tier=NAME": "--tier NAME"})` in `cov.py`'s
`_do_get`) can become a field-named error instead of a text rewrite.

**Likely scope creep to refuse:** the coverage report's own behaviour
(done), kmodcov export/check verbs (own series).

---

## Item 5 — `otto monitor` and `otto test --monitor` share one monitor library

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
