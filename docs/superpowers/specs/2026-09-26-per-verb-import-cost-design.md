# Each command pays only for its own verb — design

**Status:** approved in conversation 2026-09-26, one section at a time: scope and contract (§2–§3), mechanism (§4), and deferred dependencies plus harness (§5–§6). The last approval came with two amendments: the git provenance query is deleted outright (§5.2), and the `asyncssh` fix uses an override, not an import-state check (§5.1).
**Issue:** #455. The single-file packaging and install-time ideas are split out as the spike #470, not in scope here.
**Amends:** `2026-06-29-import-budget-guard-design.md` and `2026-09-25-dispatch-startup-cost-design.md` §3. The import budget moves from module counts and snapshots to file-operation ceilings (§3).

## 1. Problem

The NFS user behind #455 runs otto from a virtualenv on NFS. It is their only practical option. Every import-system lookup (a stat of a candidate path, an open of a `.pyc`, a directory listing) is then a network round trip, so the startup cost of a command is roughly its file operations × RTT (`docs/architecture/startup-performance.md`, "The cost model").

The dispatch-startup work (4ae54ca7) removed the costs that grew with the test corpus. What remains is the import graph. Measured on the dev VM at 4ae54ca7, with a generated realistic repo (50 test files), a warm cache and local disk:

| Command | Wall (median of 5) | File operations (whole process tree) | In the workspace |
|---|---|---|---|
| `otto --version` (stdlib shim) | 27 ms | 603 | 0 |
| `otto --help` | 286 ms | — | — |
| `otto host --help` | 333 ms | — | — |
| `otto -R run noop` | 440 ms | 4,740 | 25 |

`run noop` does no work. Its 4,740 file operations split into:
- site-packages: 1,586
- venv, system library paths and child processes: 1,423
- stdlib: 871
- otto's source: 778

It imports 167 of otto's own modules. The top-level command tree is already lazy: all 14 verbs register with `"module:attr"` loaders in `otto.cli.builtin_commands`. The cost comes from three places the verb never asked for.

1. **Eager package `__init__`s.**
   - Loading settings imports `otto.models`. Its `__init__` imports `otto.host.os_profile`, which runs `otto.host/__init__.py`: 66 eager re-exports, including the host factory. That imports `otto.models.host`, which imports `otto.link`.
   - `otto.models/__init__` also builds the monitor models (~12 ms).
   - `otto.monitor/__init__` eagerly imports the collector and broadcast code, so an init module that only wants `otto.monitor.parsers.MetricParser` pays for all of it.
2. **Import-time registration.** Sixteen registrations, across fifteen modules, fill a `Registry` as a side effect of being imported, for example `register_transfer_backend("nc", NcFileTransfer)` at the bottom of the 2,000-line `nc.py`. Anything that needs a registry to be complete has to import every module that registers into it.
3. **A debug log line.** The command preamble logs `repo_provenance(repo)` at DEBUG (`otto.cli.invoke`). The call chain behind it:
   - `Repo.commit` calls `run_git_command`;
   - that builds a `LocalHost`, which starts a persistent `bash` session (spawning `bash` and `stty`), and runs `git -C … log -1 --format=%H` through it;
   - the session's `run_cmd` imports `asyncssh` on every call, only to name `asyncssh.ConnectionLost` in an `except` clause (`otto/host/session.py`);
   - `asyncssh`'s import in turn probes for `liboqs` with `ctypes.util.find_library`, which runs `ldconfig -p` three times and `gcc` and `ld` twice.

   One DEBUG line costs 15 child processes and the whole SSH and crypto stack on every command.

By import time (`-X importtime`, self time), `run noop` spends about 155 ms in otto's own modules. `otto.models` accounts for about 60 ms of that, mostly pydantic building validators at class definition, and `otto.host` for about 46 ms across 56 modules. `asyncssh` plus `cryptography` add about 60 ms.

## 2. Scope

**Goal.** Each otto command imports only what its own verb needs, so the file operations of startup follow the work done, not the size of otto.

**In scope:**
- Lazy package `__init__`s for `otto.host`, `otto.models` and `otto.monitor` (§4.1).
- Registries that hold their built-ins by reference (§4.2).
- `asyncssh` confined to the SSH session class (§5.1).
- Deleting the git provenance query (§5.2).
- Deferred pydantic schema building (§5.3).
- Reshaping the import budget around file-operation ceilings, with per-verb surfaces (§3, §6).
- Cutting specific dependency edges (for example settings models → host code), but only where the §3 surfaces still show a cost after §4 and §5 land (§5.4).

**Not in scope:**
- The init-module registration manifest (#455's comment). Init modules keep loading for every command, and a broken init module still fails loud at startup. This is deliberately deferred and may never be needed.
- Splitting bootstrap into on-demand parts.
- Everything in #470: single-file packaging, extracting compiled extensions to local disk, unchecked-hash `.pyc` files, `sys.path` trimming, and the `liboqs` probe for verbs that really do import `asyncssh`.
- The shim. It stays exactly as it is.

## 3. The contract: file-operation ceilings

**The metric** is the file operations a command performs. That means every path syscall (the stat family, `openat`, `getdents64`, `readlinkat`, `faccessat`) across the command's whole process tree, child processes included, because spawning `gcc` is real cost. Only the kernel's virtual filesystems (`/proc`, `/sys`, `/dev`) are excluded. The harness does not guess which of a user's mounts are on NFS: for this user the venv and the interpreter's stdlib live under their home directory, so every other path counts.

**Every counter is a ceiling:** a measured baseline plus headroom. There are two counters per surface:
- **file operations:** the whole tree;
- **workspace file operations:** the subset under the repo and `OTTO_HOME`. This replaces the exact `stat_workspace` golden.

**How ceilings behave:**
- **Headroom** is 10% by default, with a small absolute minimum so ±1 jitter can never trip a low counter. That jitter is the import system's directory-listing wobble (#360/#361/#428). A surface may widen its headroom, with a comment saying why.
- **Growing past a ceiling fails.** The fix is a deliberate regeneration, explained in the commit.
- **Shrinking never fails.** Below 80% of a ceiling, the harness prints an advisory note suggesting it be tightened. This is the existing `STAT_TOTAL_STALE_RATIO` behaviour.
- **Ceilings are stored per surface and per Python minor version,** since each interpreter's stdlib differs.

**What the gate no longer checks:** module-set snapshots, module-count caps and module denylists, on every surface, including the existing ones. Unneeded libraries are still caught, through their cost. Pulling `asyncssh` and `cryptography` into `run` costs about 600 file operations and trips any 10% ceiling. A module that costs five lookups is not worth a gate.

**A failure explains itself.** When a ceiling trips, the harness prints the command's file operations grouped by top-level package and by child process, next to the baseline's. For example, `+598 asyncssh, cryptography` or `+12 processes: ldconfig, gcc, ld`.

**Why ceilings over exact counts.** Dependency versions are pinned in `uv.lock`, so dependency drift reaches the budget only on a deliberate lock bump, where regenerating is the expected chore. otto's own growth moves a ceiling only when it is large enough to be real cost. Exact counts and module snapshots failed on every harmless change and said little about cost.

### 3.1 Surfaces

**Gated** (ceilings enforced):

| Surface | Command | Notes |
|---|---|---|
| `run` | `otto -R run noop` | The existing `dispatch_repo_warm` shape, generated realistic repo. |
| `test` | `otto test <suite>` with a one-test suite | Loads the suites. |
| `host_exec` | `otto host local exec true` | The built-in `local` host (`otto.host.builtin_hosts`). It stands in for the ~30 host subverbs that run a shell command, because every host subverb is a `@cli_exposed` method on the same host class (`otto.cli.expose`). |
| `host_put` | `otto host local put <file> <dir>` | The transfer path. |
| `host_get` | `otto host local get <file> <dir>` | The transfer path. |
| `host_login` | `otto host local login`, under a pty the harness opens, fed `exit` | The terminal bridge. |
| `host_exec_ssh` | `otto host <id> exec true` against a fixture lab host at `127.0.0.1` on a closed port | The SSH path, with no real host: the connection is refused at once. The non-zero exit is expected and asserted. If otto retries a refused connection, the surface disables retries, so the count reflects one attempt. |

**Every existing surface** (`version_repo`, `help_repo`, `help_repo_warm`, `bootstrap_repo`, `completion_repo_warm`, `completion_repo_handover`, the `--help` surfaces and so on) stays gated and migrates to the same two ceilings.

**Tracked** (measured and printed in `make profile`'s table, never enforced):
- the other 11 top-level verbs (`init`, `env`, `cache`, `docker`, `link`, `tunnel`, `monitor`, `cov`, `reservation`, `inventory`, `schema`);
- `host probe` and `host power`.

A tracked surface becomes gated when someone optimizes it and wants the win pinned.

### 3.2 The numeric target

The ceilings pin today's cost; they do not say how low it should go. After §4 lands, the first measurement sets a target for each gated host and run surface as a **ratio to `otto --version`'s file operations**, for example "`host_exec` ≤ N× `version_repo`". It is recorded as an amendment to this section. A ratio survives dependency updates that make every import cheaper or dearer, because both sides move together.

### 3.3 The before measurement

Before any product change, the first task records the pre-change numbers, so the final report can compare before and after on the same terms. It measures two things on the unchanged tree:

- **Old metrics:** for every existing surface, today's snapshot figures: the module count, `stat_workspace`, `stat_total` and the other I/O goldens, exactly as the current harness reports them.
- **The new metric:** for every surface in §3.1, gated and tracked, including the ones that do not exist yet, the two §3 counters (file operations and workspace file operations) plus the breakdown by package and child process. The new surfaces are measured by running their commands under the new counting rule against the old code.

The measurement uses at least CPython 3.10, and 3.14 as well. The table is committed with the harness change, so it outlives the plan's workspace. It is reproduced in the final report and in the squash message next to the after numbers.

## 4. Mechanism

### 4.1 Lazy package `__init__`s

`otto.host`, `otto.models` and `otto.monitor` follow `otto/__init__.py`'s existing PEP 562 pattern:
- a module-level `__getattr__` backed by one table mapping each exported name to its module;
- `__all__` preserved;
- `__dir__` answering from the table;
- a `TYPE_CHECKING` block of the real imports, so `ty`, IDEs and Sphinx see the real types.

Every name importable from these packages today stays importable. `from otto.host import LocalHost` imports `otto.host.local_host` and nothing else.

`otto.models/__init__`'s eager `import otto.host.os_profile` is the edge that turns "load settings" into "load the host package". It goes lazy with the rest.

If an eager import in one of these `__init__`s turns out to have a side effect other than registration, it is fixed at its source, and the fix is named in the plan.

### 4.2 Registries hold built-ins by reference

**The rule, uniform across every `Registry`:** built-in entries are registered as references, `"module:attr"`, in the module that defines the registry, and resolved on first lookup. `CLI_COMMANDS` already works this way for the 14 top-level verbs. This makes the mechanism general.

- **The reference type.** A small explicit wrapper (a `Ref`-style value type), so a reference is never confused with a registry whose values really are strings (`Registry[str]` exists in the module doctest).
- **How lookups behave:**
  - `names()` and `in` answer without importing anything, so help and completion stay cheap.
  - `get(name)` imports that one entry and replaces the reference with the object, so later lookups are plain dictionary reads.
  - `items()` resolves every entry.
- **Plugins keep registering real objects,** exactly as today.
- **Scope:** the sixteen import-time registrations found at 4ae54ca7 move to references:
  - `binary_loader`, `os_profile` (host classes and OS profiles), `login_proxy`, `embedded_filesystem`, `connections` (term backends), `power`, `command_frame`;
  - the seven transfer backends (`ftp`, `shell`, `scp`, `console`, `tftp`, `sftp`, `nc`);
  - `monitor.snmp` metrics.

  The plan re-surveys this list before starting.
- **Unchanged:** the class-level `loader=` that `SUITES` gained in the dispatch-startup work answers a different question (when test files load) and is not affected.

A registry whose built-ins are defined in the same module as the registry gains nothing from references, because importing the registry already imports them. It follows the rule anyway, for uniformity.

### 4.3 Guards

- **Every reference resolves.** A unit test imports every built-in reference in every registry, and checks each resolves to the expected kind of object (a class, a callable). This catches typos and renames.
- **Order independence.** For each registry, a fresh subprocess imports only the registry's defining module and asserts the full built-in name set is present before any implementation module is imported. This catches a built-in that still depends on an import side effect.
- **The §3.1 ceilings.** These catch an eager import re-introduced anywhere, which the two tests above cannot see.

## 5. Deferred dependencies

### 5.1 `asyncssh` belongs to the SSH session

`ShellSession.run_cmd` gets the set of exception types that mean "connection lost" from a method. The base implementation returns the transport-neutral ones (`asyncio.IncompleteReadError`, `BrokenPipeError`). `SshSession` overrides it to add `asyncssh.ConnectionLost`, importing `asyncssh` inside the override. `SshSession` is the SSH path, so that import is the one that genuinely needs it. The later `isinstance(exc, asyncssh.ConnectionLost)` check moves behind the same override. `LocalSession` and `TelnetSession` never mention `asyncssh`.

**All function-scope `import asyncssh` sites get the same audit:** `session.py` (six sites), `connections.py`, `interact.py`, `survey/login.py` and `transfer/scp.py`. Each one either sits on a genuine SSH path and stays, or moves behind an override on the class that owns the SSH behaviour.

### 5.2 The git provenance query is deleted

The only consumer of the per-command git query is the DEBUG line in the command preamble. Deleted:
- `repo_provenance` and its log line (`otto.cli.invoke`);
- `Repo.commit`, `Repo.description` and `Repo.commit_name` (the last has no callers);
- `Repo.set_commit_hash`, `Repo.set_git_description` and `Repo.run_git_command`, with their private fields and their tests.

If a commit hash is wanted for display later, whatever displays it reads it on demand.

### 5.3 Pydantic builds each model on first use

`OttoModel` sets `defer_build=True` in its model config, so a model's validator and serializer are built the first time they are used, not at class definition. A model definition error would then surface at first use rather than at import. One unit test forces a build of every `OttoModel` subclass, so it still surfaces in CI.

### 5.4 Dependency edges, only where the numbers say so

After §4 and §5.1–§5.3, the plan re-measures the gated surfaces with the §3 failure breakdown. A dependency edge (for example `otto.models.host` → `otto.link`, or settings models → host code) is cut only if the breakdown shows it still costs file operations on a gated surface. A cut edge is recorded in `tach.toml`.

### 5.5 Code-shape rule

Optimizations here stay structural, never conditional:
- **Allowed:**
  - declarative tables (§4.1, §4.2);
  - one configuration line (§5.3);
  - an override on the class that owns the behaviour (§5.1);
  - deletion (§5.2);
  - a function-scope import in the one function that genuinely needs a heavy dependency (already the codebase's idiom).
- **Not allowed:**
  - branching on import state: `sys.modules` probes, `try: import … except ImportError` for speed, or flags recording what is loaded;
  - helpers or guards whose only purpose is dodging an import.
- **A deferral must earn its place.** Each change names its measured saving on a gated surface. A change the ceilings cannot see is not made. An optimization that cannot be expressed structurally is not made.

## 6. The harness

`scripts/import_budget.py` and `tests/unit/import_budget/` change as follows:

- **Counting.** The strace parse counts file operations across the whole process tree, child processes included. This reverses today's `parse_strace` rule of excluding tids that `execve`. `/proc`, `/sys` and `/dev` are excluded.
- **Ceilings.** One ceilings file per Python minor version (replacing the per-surface I/O goldens and module snapshots), with baseline and headroom per counter per surface. `make import-snapshot` becomes "regenerate ceilings" for the current interpreter; the other minors need their own run, as today.
- **Removed:**
  - module caps (`Surface.cap`);
  - module-set snapshots;
  - denylists (`_ALL_HEAVY` and the per-surface tuples);
  - the exact `stat_workspace` / `open_home`-style goldens.

  The `EXACT_IO_COUNTERS` / `CEILING_IO_COUNTERS` split collapses to ceilings.
- **Diagnostics.** A tripped ceiling prints the breakdown by top-level package and by child process against the baseline's breakdown. The baseline's breakdown is stored alongside the ceiling.
- **New surfaces** per §3.1:
  - the local-host surfaces need no fixture beyond the built-in `local` host;
  - `host_login` runs the child under a pty (`pty.openpty`) and writes `exit`;
  - `host_exec_ssh` uses a fixture lab file with one SSH host at `127.0.0.1` on a port the harness proves closed before running.
- **Tracked surfaces** run in `make profile` and print in its table, with no ceiling.
- **Unchanged:** the corpus-scaling tests from the dispatch-startup work (`test_dispatch_io_does_not_scale_with_corpus_size`, `test_cold_rebuild_walks_the_corpus_once`). They assert a shape, not a count.

## 7. Documentation

- **`docs/architecture/startup-performance.md`, "What holds these numbers in place":**
  - rewritten for the one-ceiling model: the metric, headroom, the breakdown on failure, and gated versus tracked surfaces;
  - a short new section on how startup stays proportional to the verb: lazy packages, built-ins by reference, and the code-shape rule.
- **`docs/architecture/quality-gates.md` and `docs/contributing.md`:** the import-budget descriptions updated to match. `contributing.md` gains two rules for contributors:
  - register built-ins by reference;
  - never branch on import state (§5.5).
- **`docs/architecture/subsystems/registries.md`:** references (`names()` is free, `get()` resolves) and the order-independence guard.
- **Any docs page that shows `from otto.host import …` or `otto.models` imports:** no change needed, because the public names are unchanged. The plan greps for that to confirm.

## 8. Acceptance

- All gated surfaces pass their ceilings on every supported interpreter (3.10–3.14).
- `run` no longer imports `asyncssh`, `cryptography`, `otto.monitor.collector`, `otto.docker` or `otto.link`, and spawns no child processes. This is verified once by the §3 breakdown and recorded in the plan's final report, not gated as a denylist.
- `host_exec` (local) does not import `asyncssh`.
- The §4.3 guards pass, and each is proven able to fail. A deliberately re-introduced import-time registration turns the order-independence test red.
- The §3.2 target is set and recorded.
- A before/after table for every gated and tracked surface, from the §3.3 before measurement and the same measurement after, is in the final report and the squash message. For existing surfaces it shows the old snapshot metrics (module count, `stat_workspace`, `stat_total`) as well as the new counters.
- The full gate suite is green before the squash: `make coverage`, `nox -s tests_hostless-3.14`, `make typecheck`, `make docs` and `make gate-fresh`.

## 9. Coordination

- **Other branches.** The import-budget reshape touches `scripts/import_budget.py`, its tests and every I/O golden. Any other branch that regenerates import-budget goldens rebases after this lands and regenerates ceilings instead.
- **#457 lands after this, and its cost is bought knowingly** (decided 2026-09-26). #457 (plain pytest tests and per-verb options) changes what `otto test` costs and touches `registry.py`, `host/session.py` and the budget. It starts from this work's ceilings and reuses its `Ref` type. Its plan must report, per surface, its file operations before and after against this work's ceilings. Any ceiling it raises is presented for explicit approval, with the breakdown, before the new ceiling becomes the baseline. A regression is accepted deliberately or fixed, never absorbed by a silent regeneration.
- **Plugins.** `otto.host`'s public names are unchanged, so third-party init modules that import from it keep working. A plugin that relied on a built-in being registered because it happened to import `otto.host` gets the same built-ins through the reference, on first lookup.
