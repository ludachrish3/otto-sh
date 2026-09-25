# Ordinary commands stop paying for completion and test suites — design

**Status:** approved in conversation 2026-09-25 (scope, stat counter, repo shape, packaging, the §2b reversal and its guard were each decided explicitly).
**Reverses:** the 2026-08-06 ruling recorded in `todo/churn-review-remaining-work-2026-08-05.md` §2b ("a module that declines to load gates dispatch"). See Part 3.
**Amends:** `2026-06-29-import-budget-guard-design.md` (adds strace counters and a dispatch surface, removes `--hyperfine`), and `2026-09-04-shim-completion-design.md` (a stale handover now repairs the cache).
**Follow-ups filed:** #446 (rebuild only the stale sections) and #447 (rebuild in a detached process). Neither is in scope here.

## 1. Problem

A user running otto from an NFS-mounted install reported that `otto host <id> exec whoami` takes 3–5 s before anything visible happens. On NFS, every path syscall is a round trip (see `docs/architecture/startup-performance.md`, "The cost model"), so startup cost is roughly syscall count × RTT. Measured on the dev VM at `f2158c3b` with the two fixture repos (`tests/repo1`, `tests/repo2`) and a warm cache:

| Phase of `otto host test1 exec whoami` | Python-process path syscalls |
|---|---|
| Interpreter + CLI import | ~2,200 |
| `bootstrap()` | ~2,600 |
| Completion-cache validity check | 431 |
| Dispatch and exec | ~1,600 |

Three costs are paid by every command without being needed by it:

1. **The completion-cache validity check runs on every dispatch.** `otto.cli.main.entry()` calls `cache_rebuild_is_worthwhile` after `bootstrap()` on every command, not only on TAB or root help. The check validates `names`, `tests` and `shim`, and so stat-hashes every test source in every repo. It grows linearly with the corpus: on a synthetic repo with 0, 500 and 2,000 nested test files it cost 239, 2,239 and 8,239 syscalls. At a 1 ms RTT, 2,000 test files cost about 8 s on every command.
2. **Half of that check is redundant.** The `shim` section's key set is `names ∪ tests` (`cache_sections._shim_key_paths`), so after `tests` digests the corpus, `shim` walks it again. Each test file is stat'd twice and each directory listed twice.
3. **Every repo's test files are imported on every command.** `bootstrap()` executes every top-level test file so that `OttoSuite.__init_subclass__` fills `otto.suite.register.SUITES`. Real test files `import pytest`, so `otto host exec` loads pytest and its dependencies plus `otto.suite`: about 125 of the 851 modules on that command.

The import-budget guard (`scripts/import_budget.py`, `tests/unit/import_budget/`) exists to catch exactly this and stayed green, for three reasons:

- **No surface runs an ordinary dispatch through `entry()`.** The real-entry surfaces are `--version`, root `--help` and TAB. `run_bootstrapped` and `bootstrap_repo` call the composition root directly.
- **Stats are invisible to it.** Its I/O counters are Python audit events (`open`, `os.scandir`, `os.listdir`), and CPython has no stat audit event. `test_completion_io_does_not_scale_with_corpus_size` says so in its docstring. The cache check is mostly stats: 296 of its 431 syscalls are `newfstatat`.
- **Its generated repo is not shaped like a real one.** Its test files do not import pytest, and no init module touches `otto.monitor`, so neither cost ever appeared in a snapshot.

The guard's `--hyperfine` wall-clock option is a manual diagnostic that nothing runs.

## 2. Scope

One worktree, four parts, in this order, one commit per part, squashed at the end:

1. **Guard expansion.** The guard learns to see this class of cost. It lands with an enumerated expected-red test that proves it can.
2. **Completion-cache fix.** That test goes green.
3. **Test files load on demand.** This reverses §2b.
4. **Remove hyperfine.**

## 3. Part 1 — guard expansion

### 3.1 strace counters

The harness runs each surface's measured command under `strace -f -e trace=%file,getdents64` and attributes only syscalls made by the measured Python process. Child processes (git, ssh) are the command's own work and are excluded. The **stat family** is `stat`, `lstat`, `newfstatat`, `fstatat64`, `statx`, `access`, `faccessat` and `faccessat2`, i.e. the calls `startup-performance.md`'s Diagnose section already names.

| Counter | What it counts | Gate |
|---|---|---|
| `stat_workspace` | Stat-family calls on paths under the fixture root (the generated SUT repos and the surface's `OTTO_HOME`), **excluding** calls whose path is exactly one of the repos' lib dirs on `sys.path` | **Exact**, per interpreter, in the existing `<key>.io.<major.minor>.txt` goldens |
| `stat_total` | Every stat-family call in the measured process | **Ceiling:** the golden records a baseline, and a measurement above baseline × 1.10 fails |

Why this split:

- **`stat_workspace` is otto's own logic,** so it is deterministic. Two back-to-back warm runs of the fixture command both measured exactly 281, so an exact golden catches a single new constant workspace touch. The lib-dir exclusion matters because the import system re-stats each directory on `sys.path` for every later import. That count follows the module graph, which the module caps already gate.
- **`stat_total` is dominated by the interpreter's import machinery,** which drifts with bytecode state and `sys.path`. It moved by 4 between two identical runs, so it cannot be exact. The ceiling is the safety net for a large unnecessary constant cost anywhere in the process. The workspace slice alone (8% of the total on the fixture command) would pass under any loose ceiling, which is why it is gated exactly and separately.

The existing audit-hook counters stay as they are.

**strace is required.** When it is missing, the budget tests FAIL with an install hint and never skip. CI installs it (`apt-get install strace`) in every job that runs `tests/unit`. strace is Linux-only, and so is every lane that runs the budget today.

**Corpus-scaling tests.** Every existing `*_does_not_scale_with_corpus_size` test also asserts that the `stat_workspace` delta between its small and large corpus is at most a small constant (≤ 5, the same tolerance its `open` delta uses). Both measurements come from one environment, so drift cancels.

### 3.2 New surface: `dispatch_repo_warm`

This surface measures the branch of `entry()` that every real command takes.

- **Setup:** real `entry()`, a generated repo, and a warm cache (measured on its second run against one home, like `help_repo_warm`).
- **argv:** `otto run <noop>`, where `<noop>` is a lab-free instruction the generated repo registers and whose body returns immediately. If planning finds that a lab-free instruction cannot run hostless, the fallback argv is `otto run --help`, which takes the same `entry()` branch (not root help, so it bootstraps). The plan records which one it used and why.
- **Denylist:** `_ALL_HEAVY` minus `pytest` until Part 3 adds it back (see §3.3).
- **Cap:** measured on 3.10 + 15, the #303 policy for a full-path surface.
- **Scaling test:** `test_dispatch_io_does_not_scale_with_corpus_size` compares 50 files/5 dirs with 200 files/20 dirs on `open`, `scandir` and `stat_workspace`.

**Expected red.** On the Part 1 commit, `test_dispatch_io_does_not_scale_with_corpus_size` FAILS (the dispatch cache check scans and stats the corpus), and it is the only test that does. Part 1 is gated on exactly that one failing id. That red is the proof that the guard sees the defect, per the "a guard must inject the hostile condition" rule.

### 3.3 Realistic generated repo (record only)

The generated repo gains:

- test files whose module bodies `import pytest` and `from otto.suite import OttoSuite` and define one `Test*` suite;
- one init module that imports `otto.monitor.parsers`.

The goldens of every repo-bearing surface change to show pytest and the `otto.monitor` → `aiosqlite` chain, and their caps are re-baselined under the #303 policy.

**Record only, so pytest is allowed where it now really loads.** `_ALL_HEAVY` includes pytest, so two surfaces would otherwise fail their denylist on the realistic repo:

- `bootstrap_repo`, because bootstrap imports test files;
- cold `help_repo`, because its rebuild collects suites.

Part 1 removes `pytest` from exactly those two denylists, and `dispatch_repo_warm` starts without it. Each removal carries a comment naming Part 3.

**Part 3 then locks in the gain.** It puts `pytest` back on `bootstrap_repo` and adds it to `dispatch_repo_warm`: after the reversal, pytest on those paths is a regression. Cold `help_repo` keeps pytest allowed permanently, because a cold rebuild loads suites by design.

A lazy `otto.monitor` package `__init__` is a separate change and is not in scope.

## 4. Part 2 — completion-cache fix

### 4.1 The shim digest is composed, not re-walked

- `cache_sections.Section` gains an optional `derived_from: list[str]`. `shim` declares `["names", "tests"]`.
- A derived section's digest is `sha256("names:<digest>\ntests:<digest>\n")`, built from the child digests. Those come through the same `known`/`digests` memo `section_digests` already threads, so each key path is stat-hashed **once** per invocation.
- `_shim_key_paths` is removed. Its only reader is the `shim` section's digest; the shim payload's stored `keys` are built from the `names` and `tests` key-path functions directly (`completion_tree.build_shim_payload`). `Section.key_paths` becomes optional, and a section declares exactly one of `key_paths` or `derived_from`, checked when the section is constructed.
- Invalidation is unchanged. The shim's key set was exactly the union, so the composed digest moves if and only if either child digest moves.
- The stdlib shim validator (`otto._shim_complete`) is unaffected. It validates from the stat triples stored in the shim payload and never reads the section digest.
- No `SCHEMA` bump. An existing cache's shim digest mismatches once and is rebuilt on the next TAB or root help that checks.

### 4.2 Ordinary dispatch stops checking the cache

`entry()` runs the post-bootstrap `cache_rebuild_is_worthwhile` check, and a rebuild on a miss, **only** on the paths that read the cache: completion mode and root help (`ROOT_HELP_ARGV`). Every other invocation does no completion-cache I/O at all.

### 4.3 A stale TAB repairs the cache

Without dispatch-time rebuilds, nothing would repair a cache whose `tests` or `shim` section went stale while `names` stayed valid. Measured at `f2158c3b` after a nested test-file edit: three full-path TABs and a root `--help` all left the shim stale, and only an ordinary dispatch repaired it. Until the 24-hour TTL ran out, every `--tests`/`-m` TAB would take the slow path and live-scan the corpus.

The repair moves to the TAB that finds the cache stale:

- `Handover` and `Outcome` in `otto._shim_complete` gain `stale: bool`.
- It is `True` only for cache-state reasons: `no cache file`, `schema mismatch`, `no sections`, `no shim section`, `no names section`, `no tests section`, `expired`, and `stale: <path> …` (gone, appeared or changed).
- It is `False` for everything else: resolution reasons (unknown option or command, `nargs`, `live source`, list fragments), `collected set cold`, `opaque inventory`, `tainted`, `not bash`, `no SUT dirs`, and `error: …`. Those keep today's cost.
- `otto._shim.main` passes the flag through: `entry(cache_stale=outcome.stale)`. `entry()` gains `cache_stale: bool = False`.
- When `cache_stale` is set in completion mode, `entry()` skips the `names` fast path, so it bootstraps, runs the check-and-rebuild of §4.2, and then Typer answers. The cost is one cold TAB per change; the next TAB is served by the shim again.

Not covered, both unchanged from today: a root `--help` with a valid `names` section does not repair `tests`. zsh and fish TABs never reach the shim, and only bash completion is supported.

`logins_by_host` (schema 20) stays in both `write_cache`'s call and `cache_sections._collect_names`. `tests/unit/shim/test_differential.py` (shim == Typer) must stay green.

## 5. Part 3 — test files load on demand

### 5.1 The reversal

§2b ruled that a test file that fails to load must fail **every** command, so a broken suite is never silently missed. Chris reversed that on 2026-09-25: the marginal benefit does not justify the cost. Every command pays for importing every repo's test files and pytest, and one broken test file blocks unrelated commands such as `otto host exec` and `otto schema export`.

After this part, a broken test file fails loudly on **the commands that read suites**. Everything else neither pays for it nor is blocked by it. The reversal covers test files only: an init module that fails to load still fails every command, because init modules are where extensions register and every command depends on them. §2b in the todo file is rewritten to record the reversal, its date and its rationale, so a future pass does not restore it. The "fail loud" framing itself is unchanged: one `warning:` line naming the file and the cause, one summary line, exit 1, no traceback.

### 5.2 Mechanism

- **`bootstrap()` stops importing test files.** Init modules still load on every command.
- **`Registry` gains an optional `loader`,** a callable run exactly once before the registry's first read (`items`, `names`, `get`, `__contains__`, `__iter__`, `__len__`), and never re-entered.
- **`SUITES` declares `loader=load_test_suites`**, a new function in `otto.bootstrap`. It calls `bootstrap()` (idempotent), then imports each repo's test files under that repo's registering marker, with today's per-file containment. Each failure is appended as a framed `BootstrapError` to the live `BootstrapResult.errors` list.
- **Every current `SUITES` reader** goes through the loader without being changed: `cli/test.py` (the suite subcommand group and the `--list` panels), `suite/run.py` (resolving a suite by name, which covers library use), `config/repo.py` (`registered_suites` and the suite-restricted run), and `completion_cache.collect_current_commands` (the cache rebuild).
- **Fail loud where suites are read.** The `otto test` command path calls the existing `fail_loud_on_bootstrap_errors` after triggering the load.
- **The rebuild stays honest.** In `entry()`'s rebuild, `tainted` is computed **after** collection, so errors from a suite load during the rebuild still taint the cache. That is what today's taint rule promises.

### 5.3 Test files register suites, and nothing else

A test file runs under the same registering-repo marker as an init module, so today it can register instructions, products, dev tools and so on. Once test files load lazily, such a registration would silently disappear from every command that does not read suites. This is made loud instead:

- `otto.registry` gains a suite-loading phase marker (a context variable) that `load_test_suites` sets around each test-file import.
- `Registry.register` refuses during that phase unless the registry opted in. `SUITES` is the only one that opts in, via a constructor flag.
- The two provider lists that bypass `Registry`, `register_product_provider` (`host/product.py`) and `register_dev_tool_provider` (`host/dev_tool.py`), check the same marker.
- A refusal is a framed, contained `BootstrapError` naming the file and what it tried to register: "register <kind> from an init module, not a test file".
- **Guard test:** every `Registry` instance in the imported `otto` package is enumerated (the class records its instances), and each one except `SUITES` refuses during the phase. Any future `_PROVIDERS`-style list must be added to the test explicitly; the test names the two it knows about.

## 6. Part 4 — remove hyperfine

Remove:

- `--hyperfine` and `_run_hyperfine` from `scripts/import_budget.py`;
- the `hyperfine` target and `HYPERFINE_VERSION` from the Makefile, and the `dev` target's call to it;
- `scripts/install_hyperfine.sh`;
- the mentions in `docs/contributing.md` and `docs/architecture/startup-performance.md`, and the `profile` target's comment.

Historical specs are records and stay as they are.

## 7. Documentation

Each topic has one home, and other pages link to it:

- **`docs/architecture/startup-performance.md`:** "What holds these numbers in place" describes the strace counters and their two gates. The "no stat audit event" caveats become "counted by strace". The page names Part 2's per-command saving.
- **`docs/architecture/subsystems/completion.md`:** a new "Who refreshes the cache" section covering the readers, the stale-TAB repair and the fact that dispatch never touches the cache.
- **`docs/architecture/subsystems/bootstrap.md`** and **`docs/architecture/lifecycle.md`:** test files load on demand, not in phase 2.
- **Test-suite author docs,** wherever `OttoSuite` registration is explained: a test file registers suites only; extensions go in init modules; a broken test file fails the commands that read suites.
- **`entry()`'s docstring and `Repo.iter_test_files`' docstring** are rewritten to match.

## 8. Acceptance

- **Part 1:** the new counters are live, meaning `stat_workspace` is greater than 0 on every repo-bearing surface and the harness fails without strace. `dispatch_repo_warm` exists. On the Part 1 commit, `test_dispatch_io_does_not_scale_with_corpus_size` is the only failing test.
- **Part 2:** that test passes. On `dispatch_repo_warm`, the `stat_workspace` golden drops to the repos' settings and lab reads (no corpus term). A dispatch calls `completion_cache.hash_file` zero times and writes no cache file. A names + tests + shim validation hashes each key path exactly once. The shim digest moves if and only if the `names` or `tests` digest moves (differential test). A stale bash TAB after a nested test-file edit repairs the cache, and the next TAB is answered by the shim. A non-stale handover (unknown option, `live source`) does not bootstrap.
- **Part 3:**
  - `dispatch_repo_warm` and `bootstrap_repo` lose pytest and `otto.suite` from their goldens, and both deny `pytest` again (§3.3).
  - A broken test file makes `otto test …` fail loud and leaves `otto run <noop>` and `otto schema export` exiting 0.
  - `otto test` still lists and runs every suite.
  - A cold cache rebuild still records every suite and is tainted by a broken test file.
  - A test file that registers an instruction fails with the framed "from an init module" error.
  - The registry-enumeration guard passes.
  - The §2b record is rewritten.
- **Part 4:** there are no hyperfine references outside historical specs, and `make dev` no longer installs it.
- **Re-measurement:** the §1 scaling probe (0/500/2,000 nested test files) shows a dispatch's cache cost at 0 for all three sizes. The before and after numbers go in the squash commit message.
- **All parts:** the full gate suite passes before the squash (`make coverage`, `nox -s tests_hostless-3.14`, `make typecheck`, `make docs`, `make gate-fresh`).

## 9. Not in scope

- Rebuilding only the stale sections (#446), and rebuilding in a detached process (#447).
- A lazy `otto.monitor` package `__init__`, and any other import-graph trimming the realistic repo reveals.
- Making root help repair a stale `tests` section.
- zsh or fish completion.

## 10. Coordination

At the time of writing, two peer sessions were active. console-term (`.claude/worktrees/console-term`) touches none of these files. tunnel-check has not started; its Plan 2b adds a CLI command and modules and will regenerate import-budget snapshots after rebasing onto this change. Before this lands, the tunnel-check session is told that the budget tests now need strace and gain the `stat_workspace`/`stat_total` counters.
