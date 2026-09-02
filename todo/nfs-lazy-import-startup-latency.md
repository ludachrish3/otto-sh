> **SUPERSEDED 2026-09-01** by `docs/superpowers/specs/2026-09-01-startup-io-budget-design.md`.
>
> Two claims below were FALSIFIED by review and must not be acted on:
> 1. "Moving `DEFAULT_COMMAND_TIMEOUT` out of `otto.host.host` cuts the majority of the 442
>    modules" — FALSE. `otto/config/__init__.py` also eagerly re-exports `.lab`, and
>    `config/lab.py:339-340` -> `labs/json_repository.py:11` pulls `otto.host.factory`
>    independently. `import otto.config.lab` alone loads 46 `otto.host.*` modules. `importtime`
>    charges a shared subtree to whichever module reaches it FIRST.
> 2. "This deletes the tach `config -> host` DEBT edge" — FALSE. `tach.toml:143-147` records three
>    causes for that edge, and tach counts function-scope imports as edges anyway.
>
> Also MISSED below: the dominant per-repo term is `otto/cli/main.py:878-895`, which rebuilds and
> rewrites the completion cache — reading and `ast.parse`-ing the whole corpus — on EVERY command.

# Startup latency on network filesystems — lazy imports + environment guidance

## The report

Chris ran `otto --version` on an air-gapped system whose install lives on NFS. It took **~3 s**.
On the dev VM (local disk, `.venv` at `/home/vagrant/otto-sh/.venv`) the same command takes **0.21–0.28 s**.
A ~12× gap on the cheapest command otto has.

## What I measured, and how

All numbers below are from the dev VM, `otto 0.9.0`, CPython 3.10, local disk, warm cache.
Reproduce any of them the same way on the slow box to get a comparable row.

### 1. Split interpreter startup from otto's imports

```
/usr/bin/time -f "%e s" .venv/bin/python -c pass          -> 0.01 s
/usr/bin/time -f "%e s" .venv/bin/python -c "import otto" -> 0.02 s
/usr/bin/time -f "%e s" otto --version                    -> 0.21-0.28 s
```

Interpreter startup is noise. `import otto` (the bare package) is also noise. The cost is entirely
the CLI entry point's transitive import tree.

### 2. `-X importtime` on the real entry point

`[project.scripts]` is `otto = "otto.cli.main:entry"`, so the thing to profile is `otto.cli.main`:

```
.venv/bin/python -X importtime -c "import otto.cli.main" 2> it.txt
```

**442 modules, 150 ms cumulative.** Cumulative tree (µs):

```
150280  otto.cli.main
143473    otto.cli.main (body)
123422      otto.config
119037        otto.config.fleet
116577          otto.host.host      <-- 78% of total import time
 44048            otto.host.connections
 35877            otto.host.factory
 19939            otto.host.embedded_host
 16836              otto.host.transfer
 11413                rich.progress
 10433      typer
```

Self time, top offenders (µs):

```
10751  otto.models.host          4731  pydantic_core.core_schema
 9837  otto.models.monitor       3036  annotated_types
 7730  otto.host.options         2717  pydantic.types
 7402  otto.models.options       1451  rich.console
```

Modules by top-level package: `otto` 93, `rich` 54, `pydantic` 40, `asyncio` 27, `typer` 24, `email` 15.

### 3. The measurement that actually predicts NFS cost

`importtime` measures CPU-bound work. That is **not** what is slow on NFS. The predictive number is the
count of path-resolution syscalls, because on a network filesystem every cache miss is a round-trip:

```
strace -c -e trace=openat,newfstatat,stat,lstat,access .venv/bin/otto --version
```

```
 67.86%  0.012916s   7us/call   1779 calls   159 errors   newfstatat
 32.14%  0.006116s   9us/call    648 calls    93 errors   openat
                                2427 total,  252 ENOENT
```

**~2,400 path syscalls for `--version`**, ~250 of them ENOENT misses from `sys.path` probing.
Locally: 19 ms, all page cache. On NFS at a typical 0.3–1 ms RTT: **0.7–2.4 s of pure syscall latency**,
which reproduces the reported 3 s almost exactly.

**Model to work against: `wall_clock ≈ interpreter_startup + (path_syscalls × fs_RTT) + import_CPU`.**
On local disk the middle term vanishes and only the last matters. On NFS the middle term dominates and
the last is irrelevant. Optimising import CPU is close to useless here; **reducing filesystem touches is
the whole job.**

## Root cause on otto's side

`otto --version` does not need a single host, lab, model, or transport. It needs `importlib.metadata.version`.
`src/otto/version.py` is 6 lines and imports nothing else. Yet we import 442 modules to print it.

The chain, and the specific line that costs the most:

- `src/otto/cli/main.py:18` — `from ..config import (...)` at module top level.
- `src/otto/config/__init__.py` — re-exports eagerly from `.env`, `.fleet`, `.lab`, `.repo`, `.version`.
- `src/otto/config/fleet.py:14` — **`from ..host.host import DEFAULT_COMMAND_TIMEOUT`**.

That one import of a single float constant pulls in `otto.host.host`, which is 116 ms of the 150 ms total
and the large majority of the 442 modules — the entire host/transport/model/pydantic/rich subtree, for a
number. This is the highest-leverage line in the codebase for this issue.

## Recommendation

Goal, in the order the wins arrive:

### A. Kill the constant-drags-the-world imports (cheap, do first)

1. Move `DEFAULT_COMMAND_TIMEOUT` out of `otto.host.host` into a leaf module with no otto imports
   (e.g. `otto/host/defaults.py` or an existing constants module), and have `otto.host.host` re-export it
   for compatibility. Then `otto.config.fleet` imports the leaf. Expect this alone to cut the majority of
   the 442 modules off the `--version` path.
2. Audit every other cross-package top-level import that exists only to fetch a constant or a type used in
   an annotation. `TYPE_CHECKING` guards + string annotations cover the annotation-only cases.
   Note: this repo bans `from __future__ import annotations` (trips Sphinx `-W`), so quote annotations
   individually rather than reaching for the future-import.

### B. Make `otto.config.__init__` lazy

`otto/config/__init__.py` eagerly re-exports from five submodules. Give it a module-level `__getattr__`
(PEP 562) so `from otto.config import X` resolves the submodule on first attribute access instead of at
import. Keep the names in `__all__` and under `TYPE_CHECKING` so static tooling and `ty` still see them.

### C. Defer command-tree construction in the CLI

`otto/cli/main.py` calls `register_builtin_commands` and imports the config surface at module scope.
Typer only needs the callback for the command the user actually typed. Options worth costing out:
lazy command registration (import the subcommand module inside its own callback), or a `TyperGroup`
subclass that resolves a command's module on demand. `--version` and `--help` should touch neither
`otto.host` nor `otto.models`.

### D. Guard it so it does not regress

There is already an import-budget guard in this repo (see `project_import_budget_guard` — and the rule
attached to it: fix at the source, never by raising the cap). Extend that idea with a **syscall budget**,
because module count alone will not catch a `sys.path` regression:

- Assert a ceiling on `len(sys.modules)` after `import otto.cli.main`.
- Assert a ceiling on path syscalls for `otto --version` via `strace -c` where available, skipped with a
  named reason where not (never a silent skip — per repo convention, failures name the missing thing).
- Assert `otto.host` and `otto.models` are **absent** from `sys.modules` after a `--version` run. That is
  the real contract and it is a guard that can genuinely fail, not a tautology.

### E. Ship user-facing environment guidance

Docs page (user guide, installation/troubleshooting area — one home, link from anywhere else that mentions
slow startup) covering, in payoff order:

1. **Put the venv on local disk.** Biggest single win. If the project tree must live on NFS, the
   environment does not have to.
2. **Make sure `__pycache__` is writable.** If bytecode cannot be cached on the share, every invocation
   recompiles all 442 modules — compile CPU *plus* extra I/O, forever. Check for
   `.venv/lib/python*/site-packages/otto/cli/__pycache__/`. If read-only by design, precompile at install
   time with `python -m compileall` and/or set `PYTHONPYCACHEPREFIX` to a local-disk directory.
3. **Keep `sys.path` short.** Each extra entry multiplies the ~250 ENOENT probes. Dev VM has 5 entries.
4. **NFS mount options.** Generous attribute caching (`actimeo`, `nocto`) helps a read-mostly install
   substantially; state the tradeoff honestly rather than recommending blindly.
5. **`PYTHONDONTWRITEBYTECODE` is the wrong lever here** — call that out, people reach for it.
6. Give them the two diagnostic commands verbatim (`-X importtime`, the `strace -c` line) so a user can
   self-diagnose which of the above they are hitting.

## Metrics — the acceptance criteria

Baseline (dev VM, local disk, 0.9.0):

| Metric | Baseline | Target |
|---|---|---|
| `otto --version` wall clock, local | 0.21–0.28 s | ≤ 0.10 s |
| modules imported by `otto.cli.main` | 442 | < 120 |
| cumulative importtime | 150 ms | < 40 ms |
| path syscalls for `--version` | 2427 | < 600 |
| ENOENT probes | 252 | < 80 |
| `otto.host` in `sys.modules` after `--version` | yes | **no** |
| `otto.models` in `sys.modules` after `--version` | yes | **no** |
| same, on the NFS box | ~3 s | ≤ 0.8 s |

Derived expectation: at 600 path syscalls and a 1 ms NFS RTT the floor is ~0.6 s, so ~0.8 s on the slow
box is the honest target for `--version`. Getting below that means cutting syscalls further, not tuning CPU.

Measure the same metrics for at least one real command (`otto host list` or similar) as well — the point is
a faster CLI, not a special-cased `--version`.

## Open questions for Chris

- What are the actual NFS mount options and RTT on the air-gapped box? That pins the RTT term and tells us
  whether the target above is right or too soft.
- Does `__pycache__` exist / is it writable there? If not, that alone may be most of the 3 s and reorders
  the whole priority list — environment fix first, lazy imports second.
- Is `--version` representative of what is actually painful there, or is the real complaint a longer command
  whose latency is dominated by something else entirely (config discovery, host probing)?

## Provenance

Diagnosed 2026-08-31 on the dev VM. The syscall-count × RTT model is captured for the wiki at
`~/wiki/inbox/2026-08-31-nfs-startup-latency-syscall-model.md`.
