# Startup performance on network filesystems

otto's own engineering already removes most of what used to make startup
slow: the console script's front door, `otto._shim:main`, answers a bare
`otto --version` without ever importing the CLI (see
{doc}`lifecycle`), warm `otto --help` is served from a
cache that validates cheaply instead of walking your test corpus, and an
ordinary command neither checks that cache nor imports your test files (see
[the cache's economics](#the-caches-economics-on-a-network-filesystem)
below). What's left after those fixes is genuinely yours to
control — which disk your interpreter and otto's own venv sit on, how many
places Python has to look before it finds a module, and how your network
filesystem is mounted. This page covers all three, in the order they pay
off, plus the two commands that tell you which cost you're actually paying.

## The cost model

On a network filesystem, wall time is dominated by round trips, not bytes:

```
wall ≈ fs_RTT × path_syscalls
```

Every `open`, `stat`, or directory scan is one round trip to the
filesystem's server unless the client's own attribute cache can answer it
locally — the same cache `actimeo`/`nocto` tune, below. This model
reproduced the observed cold-start time on a real air-gapped deployment:
RTT ≈ 1.2 ms, and 2,427 path syscalls × 1.2 ms ≈ 2.9 s against an observed
~3 s cold `otto --version` — agreement at the one significant figure the
field observation itself carries, not a claim of three-digit precision.
Warm was closer to 1 s — about 830 effective round trips, since the client
absorbs most attribute lookups but not file reads or writes.

That 2,427 was the *pre-fix* framework-import cost, measured on a local-disk
dev VM (otto 0.9.0, CPython 3.10.20) with no repos discovered at all:

| Measurement | Before | After |
|---|---|---|
| `otto --version` syscalls, no repos | 2,427 | 592 |
| `otto --version` syscalls, 500-file repo | 3,862 | 592 — repo-independent |
| `otto --version` opens under the repo tree | 553 | 0 |
| cache writes per `otto --version` | 1 | 0 |

Applying the cost model above, that syscall drop predicts the deployment's
warm `otto --version` moving from ~1 s toward ~0.3 s, and cold from ~3 s
toward ~1 s. The levers below are what get the rest of the way there, and
they stay worth tuning even after otto's own fixes: 592 path syscalls — the
`newfstatat` + `openat` rows in the Diagnose section's `strace -c` output,
below — is a floor against a 200-syscall bare-interpreter baseline (`%file`
there adds a handful of `readlinkat`/`faccessat`/`execve` on top), and every
one of them still pays `fs_RTT` if it lands on a network mount.

## Put otto's venv on local disk

**This is the highest-payoff lever, even when your project tree has to live
on NFS.** The bare-interpreter floor splits across two different things:
only 38 of its 200 syscalls touch `site-packages` at all — the rest are
interpreter startup, shared libraries, and the standard library, resolving
against the *base* interpreter, not the venv. Framework imports above that
floor — otto itself and its third-party dependencies — are the part that
lives in `site-packages`, so it's otto's own venv, specifically, that's
worth moving: if it sits on the same network mount as your data, each of
those imports becomes a network round trip; on local disk, each one is a
local stat costing microseconds instead of milliseconds. (A base
interpreter that is itself on a network mount — some netboot or
minimal-container setups do this — needs the same treatment, but that's a
separate disk to check, not the venv.) The project tree your commands
actually operate on can stay on NFS — that's a cost paid once per command
body, not a cost paid on every invocation before argv is even parsed.

## Verify `__pycache__` is writable, and use `PYTHONPYCACHEPREFIX` when it isn't

Python only skips recompiling a module from source if it can *read* a
matching `.pyc` — normally right next to the source, in `__pycache__`. A
pip- or uv-installed venv is already byte-compiled at install time, so its
own bytecode is usually there before otto ever runs; it's your **project's**
source tree, if any of it lives on the network share, that's exposed here.
If that tree is a read-only NFS export, or otto's process lacks write
permission there, nothing can ever write the missing or stale `.pyc`, so
every run recompiles from source: an extra parse stacked on the extra read,
paid every single time instead of once. Check writability once for your
deployment; if it's read-only, don't disable caching for it — redirect it
instead:

```console
$ export PYTHONPYCACHEPREFIX=/var/cache/otto-pycache  # local disk, persistent
```

Setting this stops Python from reading the adjacent `__pycache__` at all —
it looks *only* under the prefix from then on, so the very first run
recompiles everything into it even where valid `.pyc`s already sit beside
the source. The prefix therefore has to be a genuinely persistent location:
point it at tmpfs, or at a fresh container layer rebuilt on every start, and
you've bought a full recompile on every single run instead of avoiding one.

Every pytest session otto starts already does this: an `otto test` run, a
listing, a dry run, and the background collection behind a test-name TAB.
While a session runs, otto points `sys.pycache_prefix` at the workspace
home's `pycache/` directory, `$OTTO_HOME/<workspace key>/pycache/`
(`~/.otto/<hash8>-<slug>/pycache/` by default; see
[The workspace home](../cli/index.md#the-workspace-home)), so your test
files, and anything else first imported during the session, are compiled
there rather than into a `__pycache__` beside them. The prefix is set for
the session only and removed when it ends. If you export
`PYTHONPYCACHEPREFIX` yourself, otto uses your prefix instead. The reason is
correctness, not speed: a `__pycache__` appearing in a test directory moves
the directory's stat, and the test-name cache reads that stat as "a file
here was added or removed". The first run after upgrading compiles into the
new location once, and so does the first run after
[`otto cache clear`](../cli/cache/index.md#clear), which removes the
directory. Only the sessions are covered: your repos' init modules, which
otto imports when it starts, before any session, compile into a
`__pycache__` beside themselves unless you export `PYTHONPYCACHEPREFIX`.

## Keep `sys.path` short

Only **top-level** imports consult `sys.path` at all — a submodule resolves
through its parent's already-known `__path__`, never back through the whole
path again. For each top-level import, CPython's `PathFinder` walks the
`sys.path` entries in order, obtaining a per-directory `FileFinder` for each
one until it finds (and caches) the directory that has the module, and that
per-entry cost is bigger than a simple failed-lookup count suggests:
adding 10 existing-but-irrelevant directories to `sys.path` moved
`import otto.cli.main` from 1,489 to 2,629 path syscalls — **+1,140, about
114 per added entry** — while the ENOENT count stayed flat. Most of that
cost is `FileFinder` *successfully* revalidating an already-cached
directory listing with one `newfstatat` per top-level import, not a failed
lookup; a nonexistent directory, by contrast, is negative-cached by
`PathFinder` and costs nothing extra on a miss. Custom `pylib` directories
and stray `PYTHONPATH` entries are still worth trimming for exactly this
reason — each surviving entry is walked, and on success revalidated, by
every top-level import that isn't satisfied by an entry ahead of it.

## NFS mount options: `actimeo` and `nocto`

Both options tell the NFS client to trust its cached attributes longer
instead of re-validating them with the server on every access, and both cut
round trips substantially in exchange for the same thing: **staleness**.
`actimeo=N` caches file and directory attributes for `N` seconds; `nocto`
(no close-to-open) skips the revalidation NFS normally forces at `open()`.
State the tradeoff plainly to whoever administers the mount: if a peer edits
a file otto reads — a shared `lab.json`, a settings file, a test file someone
just pushed — otto may not see that edit until the attribute cache expires.
That's usually a fair trade for otto's own read-mostly startup path; it is
not something to reach for on a mount several people are actively editing at
once.

## `PYTHONDONTWRITEBYTECODE` is the wrong lever

It only suppresses the **write** described above — it never disables
*reading* a `.pyc` that already exists, and it does nothing for a venv
whose bytecode was already compiled at install time. What the write buys is
a **one-time** cost: paid once, the first time a module runs with no
matching bytecode cache, never again after. Setting
`PYTHONDONTWRITEBYTECODE` doesn't remove that cost — it turns it from
*pay-once* into *pay-on-every-run*, and only for the modules that would
otherwise have cached cleanly. On a network filesystem that is the wrong
trade in exactly the case it looks like it's helping. Leave bytecode
caching on; point it at local disk with `PYTHONPYCACHEPREFIX` (above) if
the source tree itself can't take the write.

## Diagnose which cost you're paying

Two commands, run against your own deployment, tell you which of the above
actually matters for you:

```console
$ python -X importtime -c "import otto.cli.main"
$ strace -c -e trace=%file $(which otto) --version
```

(`-e trace=%file` — a portable syscall class covering `open`/`openat`,
`stat`/`newfstatat`/`statx`, `access`/`faccessat`, and friends. Three of the
five call names spelled out individually — `stat`, `lstat`, `access` —
silently trace nothing on aarch64, where glibc emits
`newfstatat`/`faccessat` instead.)

`-X importtime` reports where import *time* goes, module by module — but it
charges a shared subtree entirely to whichever module happens to import it
*first*, and it cannot separate path-search cost from read-and-parse cost.
Use it to see **which modules load**, never to attribute their cost to a
particular importer — trimming what it blames for a shared subtree removes
nothing, because the next module down the list now imports it instead.
`strace -c` answers the question importtime can't: it gives a per-syscall
count for one invocation, and on a network filesystem each call is a round
trip, so the total is a direct proxy for `wall ≈ fs_RTT × path_syscalls`
above. Use it to see **where the syscalls actually go** — run it once
before a change and once after to confirm the change moved the number that
matters, rather than assuming it did.

## Startup follows the verb

Each command should pay only for its own verb: `otto host local exec`
imports the host code it runs, but not the SSH stack (`asyncssh`,
`cryptography`), `otto.coverage`, `otto.tunnel` or pytest, which other verbs
need. Three structural choices keep it that way.

- **Lazy packages.** `otto` and every one of its packages export their
  names through PEP 562; an `__init__` with nothing to export holds only its
  docstring. A `_LAZY_*` table maps each exported name to the module that
  defines it, a generic module-level `__getattr__` resolves a name from that
  table, `__dir__` answers from the table, a literal `__all__` serves star
  imports, and a `TYPE_CHECKING` block of the real imports is what `ty`, IDEs
  and Sphinx read. An `__init__` imports nothing else at module scope and
  defines no code of its own, which sits instead in a named submodule the
  table points at (`otto.env.manage`, `otto.kgcov.library`). The exceptions
  are few: `otto`, `otto.logger`, `otto.config` and `otto.host.transfer`
  keep the eager imports they cannot work without (and `otto` the library
  `NullHandler` its `logging` import attaches), each an entry, with its
  reason, in [the lazy-init rule's allow-list](https://github.com/ludachrish3/otto-sh/blob/main/.ast-grep/rules/lazy-package-init-stays-lazy.yml).
  `from otto.host import LocalHost` imports `otto.host.local_host` and
  nothing else, and importing a submodule runs its package's table plus
  those allowed pieces. `tests/unit/test_lazy_packages.py` holds every
  package init to that shape and keeps each table and its `TYPE_CHECKING`
  block in step.

  The house shape, trimmed from `src/otto/monitor/__init__.py` (the smallest
  full example, and the one to copy):

  ```python
  from typing import TYPE_CHECKING

  if TYPE_CHECKING:
      from .collector import MetricCollector as MetricCollector

  # name -> the module that defines it, imported on first access.
  _LAZY_ATTRS: dict[str, str] = {
      "MetricCollector": "otto.monitor.collector",
  }


  def __getattr__(name: str) -> object:
      import importlib

      if name in _LAZY_ATTRS:
          return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
      raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


  def __dir__() -> list[str]:
      return sorted(set(globals()) | set(_LAZY_ATTRS))


  __all__ = ["MetricCollector"]
  ```

  The table's values take three shapes. Most packages map a name to a
  module-path string (`_LAZY_ATTRS`, as above), and the attribute of the
  same name is read from it. `otto` and `otto.config` map a name to a
  `(module, attr)` tuple (`_LAZY_EXPORTS: dict[str, tuple[str, str]]`),
  because an exported name may differ from, or live outside, the module
  that defines it (`otto.options` is `pydantic.dataclasses.dataclass`).
  `otto.logger` also keeps a module-valued `_LAZY_EXPORTS`, whose entries
  resolve to the submodule itself (`otto.logger.management`), beside its
  `_LAZY_ATTRS`.
- **Built-ins registered by reference.** A registry lists its built-in
  entries without importing any of them, and a lookup imports only the
  entry it names; see {doc}`subsystems/registries`.
- **The code-shape rule.** Every saving is structural, never conditional;
  the rule, and the import bans that keep a cut edge cut, are in
  [Contributing](../contributing.md#never-branch-on-import-state).

The `otto` command also switches off pydantic's plugin lookup, which would
otherwise open a file in every installed package; `PYDANTIC_DISABLE_PLUGINS`
under [Environment variables](../cli/index.md#environment-variables) says how
to turn it back on.

## What holds these numbers in place

The import budget, `scripts/import_budget.py`, measures **file operations,
never wall-clock**, for a table of CLI surfaces (`SURFACES` in the script).
`make profile` runs it with `--check`, and `tests/unit/import_budget/` runs the
same check under pytest, so it gates in CI too. Most surfaces run a real
command through the real console entry. The rest run a harness child
instead: the bare-import surface only imports `otto`, and the `--help`
surfaces measured without a repo and the two bootstrap surfaces import otto
(the bootstrap ones also run the composition root) and resolve the command
without rendering help, so they measure only the import-and-dispatch path. Every surface runs in a
fresh subprocess with every `OTTO_*` variable stripped, a private, empty
`OTTO_HOME`, and, where the surface needs one, a generated repo shaped like a
real one, so the count depends on otto and not on the machine's labs.

**The metric.** A file operation is any path syscall: the stat family, opens,
directory listings, link reads, access checks and execs (strace's `%file`
class plus `getdents64`). The harness runs the command under `strace -f` and
counts them across the **whole process tree**, child processes included,
because a program the command starts costs the same round trips: asyncssh's
import, for example, starts `ldconfig` and `gcc` to look for a library. Only
the kernel's virtual filesystems (`/proc`, `/sys`, `/dev`) are left out. Every
other path counts, since the harness cannot know which of your mounts are
remote. Each surface has two counters:

- **`file_ops`** is the whole count.
- **`workspace`** is the subset under the generated repo and the surface's
  `OTTO_HOME`: what otto itself reads and writes there, plus the import
  system's probes of the repo's lib directories.

**Ceilings.** Each counter is gated by a ceiling: a recorded baseline plus
headroom. The baselines are stored per surface and per Python minor, in
`tests/unit/import_budget/ceilings/<major>.<minor>.json`, because each
interpreter's stdlib and import machinery are part of every count. A check
reads only the running interpreter's file, and a gated surface with no
baseline there fails by name rather than skipping. The ceiling is
`max(int(baseline × 1.1), baseline + 5)`: 10% headroom, with a floor of five
operations so that the import system's one- or two-operation wobble (a
`FileFinder` re-listing a directory another process touched) cannot trip a
small counter. A surface may widen its own headroom, with a comment saying
why. The files store baselines, not ceilings, so changing the headroom needs
no regeneration.

- **Growing past a ceiling fails,** and the failure says what grew: the
  command's file operations grouped by where they landed (the workspace,
  each otto module, each site-packages package, the stdlib) and by child
  program, against the baseline's own breakdown. `+598 site:asyncssh (0 → 598)`
  names a dependency a verb should not load; `+12 process gcc (0 → 12)` a
  program it should not start.
- **Shrinking never fails.** When a counter measures under 0.8× its ceiling
  and more than five below its baseline, `--check` prints an advisory `NOTE`:
  the baseline is stale-high, and every later regression up to it would pass
  unseen until someone regenerates it.

**Gated and tracked surfaces.** A gated surface has baselines and is
enforced. A tracked surface (its key starts with `tracked_`) is measured and
printed in `make profile`'s table, and the test suite still checks that its
command exits as expected, but it has no baseline and no ceiling. A verb nobody has optimized
yet is tracked; it becomes gated when someone optimizes it and wants the win
pinned.

**Target ratios.** A ceiling pins today's cost; it does not say how low a
verb should go. A gated surface may also carry a `target_ratio`: its
`file_ops` may be at most that multiple of `otto --version`'s (the
`version_repo` surface, an interpreter start plus the shim: the least any
command can cost), with both measured in the same run. A ratio survives a
dependency update that makes every import cheaper or dearer, because both
sides move together, so one target serves every interpreter. Each target is
derived from the largest ratio measured across CPython 3.10 to 3.14 when it
was set, rounded up to one decimal, plus 10%. A breach lists the surface's
largest groups and every child process. The current targets sit beside each
surface in `SURFACES`.

**The bytecode cache.** A module with no cached `.pyc` costs more file
operations than one with, so a count would otherwise follow whatever bytecode
the machine happens to hold. Deleting the in-tree bytecode, as a fresh CI
checkout lacks it (`uv` compiles none at install), once moved one surface
from 2,738 to 3,030 on unchanged code, over a 10% ceiling. The
harness therefore keeps its own cache at
`$XDG_CACHE_HOME/otto/import-budget-pycache` (`~/.cache/otto/import-budget-pycache`
by default, or the system temp directory when there is no home). Once per
process, before the first measurement, it compiles every module the measured
interpreter can import into that directory with
`compileall --invalidation-mode timestamp` (skipping what is already
current), and every measured command reads its bytecode from there through
`PYTHONPYCACHEPREFIX`, with bytecode writing off. Every measurement sees the
same warm cache, the steady state of any installation after its first run.
The cache mirrors each measured venv's paths, about 60 MB per interpreter per
venv, and nothing prunes it; `make clean` does not touch it. Deleting the
directory is safe: the next measurement compiles it again, which makes that
first run slower. If the directory cannot be written, the harness fails and
names it rather than record cold counts.

strace is required. Without it the budget fails with an install hint instead
of skipping, because a guard that quietly measured less would pass the very
regressions it exists to catch.

The numbers before the per-verb work, in the old metrics and the new, are
recorded in
[`tests/unit/import_budget/measurements/before.md`](https://github.com/ludachrish3/otto-sh/blob/main/tests/unit/import_budget/measurements/before.md),
and the numbers after it, with the change per surface, in
[`tests/unit/import_budget/measurements/after.md`](https://github.com/ludachrish3/otto-sh/blob/main/tests/unit/import_budget/measurements/after.md).
How to run the budget and regenerate its ceilings is in
[Contributing](../contributing.md#the-import-budget).

## The cache's economics on a network filesystem

A cache only pays for itself on a network filesystem if *validating* it
touches asymptotically fewer files than *rebuilding* it — otherwise you've
traded one round-trip cost for another with bookkeeping on top. otto's
completion cache is built around that constraint: it lives in **one file**,
so a warm read is **one open** — the floor for anything stored on a network
mount at all.

That floor only holds if the file is reachable locally in the first place.
It lives under `$OTTO_HOME` (default `~/.otto`), and on plenty of NFS
deployments `$HOME` sits on the very mount you're trying to get off of — at
which point the "one open" is a network round trip like any other. See
[When `$HOME` is on NFS](#when-home-is-on-nfs) below for the measured cost
of that and the relocation experiment it motivates.
[`otto cache`](../cli/cache/index.md) (`info` / `clear` / `prune`) is the
management story for what accumulates under the home — worth knowing
precisely because `actimeo`/`nocto` (above) raise the odds of a stale
stat-based digest going unnoticed for longer.

What determines whether that one open is enough is what validating it has
to touch. Root help and most TABs validate only the cache's `names` section,
which is keyed on the files that can register something — including the
`lab.json` the `actimeo`/`nocto` section above warns can go stale — and not
on the test corpus. So a warm `otto --help` costs roughly **O(key set)**, not
O(corpus). A test-name TAB in bash costs the same whatever the corpus size:
it answers from the names pytest last collected and stats only a handful of
paths that decide collection for a whole repo (its pytest configs, its
settings file, the installed packages' directory). Checking every test file
costs a stat per file, so otto does it where that cost is paid anyway or
off the keystroke: in `otto test`'s own runs, which import test files
regardless, and in a background process a TAB starts at most every ten
minutes. The price is that a TAB can offer names up to one check out of
date; a run never does. The key sets, the digests, the test-names cache and
when each is rebuilt are described on {doc}`subsystems/completion-cache`.

A cached payload isn't always worth writing, and otto skips it rather than
paying for it anyway in two cases: no repos were discovered to register
anything from, or the workspace's host inventory can't report a stable
freshness fingerprint to validate against later. `otto --version` is a
third, simpler case — it touches neither the cache nor the corpus, because
there's nothing in either one worth opening a file for. The cache is a
lever that pays automatically when it can; it is never a tax charged on
invocations that can't use it.

That includes every ordinary command. Previously, every command checked the
cache after bootstrap, whether it read the cache or not, and imported every
repo's test files to register suites. On otto's two fixture repos the check
was 431 of `otto host test1 exec whoami`'s path syscalls, and it grew by about
four syscalls per test file: 239, 2,239 and 8,239 at 0, 500 and 2,000 nested
test files, which is about 8 s per command at a 1 ms round trip. The test
files brought in pytest, about 125 of that command's 851 modules. Now an
ordinary command does no completion-cache I/O whatever the corpus size, and
loads no test file: only `otto test`'s pytest session imports them. On the import budget's generated
repo (50 test files, CPython 3.10) the ordinary-dispatch surface went from 584
to 466 non-stdlib modules, from 118 to 20 stat calls inside the workspace, and
from 3,256 to 2,641 stat calls in total.

The rebuild itself got cheaper at the same time, which matters because a TAB
that finds the cache stale pays for it. A rebuild used to stat each nested
test file about four times and list each directory five times. It then
walked the corpus once, for a static scan of the test names. Now it does not
read the corpus at all: test names come only from pytest's collections, so
between the budget's 50-file and 200-file repos a cold rebuild's workspace
file operations hold within five.

## When `$HOME` is on NFS

Everything above is about otto's own venv, your project tree, and
`sys.path` — filesystems you choose where to put. This section is about the
one otto puts things on for you: its own derived-state home, `$OTTO_HOME`
(default `~/.otto`), which is `$HOME` itself on plenty of real deployments —
the one directory you didn't get to relocate just by moving your checkout.

### The measured footprint

A warm `otto --help` touches the home exactly **three** times for an *empty*
home — no user `settings.toml`, no cached inventory backend, the same
configuration the import budget measures (see [What holds these
numbers in place](#what-holds-these-numbers-in-place)): a fresh, empty
`OTTO_HOME` per surface. Those three: one `open` — reading
`completion_cache.json` whole, once, the floor described
[above](#the-caches-economics-on-a-network-filesystem) — and two `stat`s:
one confirming that file exists before the open, and one probing for an
optional user-level `~/.otto/settings.toml`. That probe runs on invocations
that consult the completion cache, not simply whenever a repo is active —
`otto --version` has one active and still touches the home zero times,
because it [never opens the cache at
all](#the-caches-economics-on-a-network-filesystem) — whether or not the
settings file is there, finding out it isn't *is* the stat. All three are
gated, not just measured: they fall in the import budget's `workspace`
counter, which covers every path under the surface's `$OTTO_HOME` as well as
inside the repo (see [What holds these numbers in
place](#what-holds-these-numbers-in-place)). That counter is a ceiling, so it
bounds the home's touches rather than pinning each one. A cold invocation, with no
valid cache to validate, cannot use the floor at all and pays the rebuild
instead (write path included).

That three-touch figure is a floor, not a ceiling: a present settings file
turns its probing `stat` into a `stat` *and* an `open` — four touches — and a
configured, cached `[inventory]` backend adds the biggest one of all, because
its [snapshot
cache](../cookbook/extending/inventory-backends.md#opting-into-the-snapshot-cache)
content-hashes the stored snapshot against
`<home>/inventory-cache/<slug>.meta.json` on every invocation that consults
the cache — cold reads and completion, not just a warm `otto --help` — the
largest home-side read there is, and the adder an NFS-home operator most
needs to plan around.

### Shared-home safety

Nothing here assumes the home has one machine to itself. otto is safe to
point several machines at the same NFS-mounted `$OTTO_HOME` because of how
the cache is written and validated, not because of care taken at any one
site:

- **Key-file freshness digests are stat-based, not content-based.** Each
  cache section's fingerprint folds in every key file's path, mtime, and
  size — never its bytes. NFS mtime and size are server state: every client
  sees the same values for the same file (modulo the attribute-cache
  staleness `actimeo`/`nocto` already cover, above), so a digest computed on
  the machine that wrote the cache and a digest computed on a different
  machine reading it agree without either one re-reading the source files.
  (The one content-based link in the chain is a cached inventory backend's
  snapshot hash — a raw-byte sha256, not a stat — and it agrees cross-machine
  at least as well: see [Opting into the snapshot
  cache](../cookbook/extending/inventory-backends.md#opting-into-the-snapshot-cache).)
- **Writes are atomic.** Every cache write lands in a tempfile beside the
  target and `os.replace`s it into place; a reader — on any machine — sees
  either the complete old file or the complete new one, never a partial
  write straddling the two.
- **The read-validate-serve path takes no locks**, so there is no lock
  daemon involved and nothing for one to wedge. Two machines racing a write
  settle by last-`replace`-wins: both versions were independently valid
  documents, and the loser's update is superseded, not corrupted. The one
  exception lives outside this path: tab-time test collection guards itself
  with a short-lived `.completion_collect.lock`, a plain `O_EXCL` file
  create with staleness-steal — still no lock daemon, just atomic file
  creation instead of a read.
- **Two otto installations sharing one repo re-collect its tests.** Each
  repo's table in the test-names cache records the Python, virtualenv and installed packages
  that wrote it, and a table another installation wrote vouches for
  nothing. So two installations (two virtualenvs, or two machines whose
  local virtualenvs differ) that run against the same repos with the same
  `$OTTO_HOME` replace each other's tables, and each one's next run or TAB
  after the other's collects the whole test tree again. Give each
  installation its own `OTTO_HOME` to avoid it.
- **A stale read of the cache file costs a rebuild, never a wrong
  screen.** Under `nocto` or a high `actimeo`, a client can hold attributes
  past the point another machine wrote a newer cache. That just makes the
  digest comparison miss, so the worst case is one redundant rebuild, never
  stale data served as current. The dangerous direction runs the other
  way — stale attributes on a *source* file, not the cache — and this page
  already flags it: see [NFS mount options](#nfs-mount-options-actimeo-and-nocto)
  above, where a peer's edit can go unseen until the attribute cache
  expires, bounded on the outside by the cache's own day-long TTL.

### Accumulation

None of the above shrinks the one cost that genuinely is NFS's fault:
nothing removes a workspace's cache directory on its own, and every distinct
`OTTO_SUT_DIRS` set a machine has ever run against leaves one behind
forever. Inspecting, clearing, and bounding that by age is
[`otto cache`](../cli/cache/index.md)'s job — see that page for `info`,
`clear`, `prune`, and the safety argument for why pruning can never touch
anything but the two cache files.

### The `OTTO_HOME` relocation experiment

If `$HOME` itself is the NFS mount, the three-touch *empty-home* floor above
is three network round trips no local lever removes — more once a settings
file or a cached inventory backend is in the mix, per the adders described
above. Pointing `OTTO_HOME` at local disk instead removes them outright:

```console
$ export OTTO_HOME=/local/disk/otto
```

Two things do not follow automatically from setting it:

- **User settings move with it, but the file itself doesn't.**
  `~/.otto/settings.toml` is read from wherever `OTTO_HOME` currently points,
  so the moment you relocate it otto looks in the new place — copy the file
  there yourself if you had one, or it reads as absent.
- **`env/` activation state moves too.** The orchestration virtualenv
  `otto env create` builds lives under the same per-workspace directory as
  the caches, so relocating `OTTO_HOME` means every workspace's `env/` has
  to be rebuilt at the new location; nothing carries an existing one over.

Both come down to the same thing: each machine pays one cold start per
workspace the first time it runs against the relocated home — a rebuilt
cache, exactly like any other cache miss, and, for a workspace that had one,
an `env/` that now reads as *absent* rather than rebuilt: nothing recreates
it automatically, so it stays gone until someone runs `otto env create` or
`otto env sync` again. That one-time cost, weighed against every round trip it
removes from every invocation after, is the evidence a dedicated cache-dir
option — splitting derived state out from under `$HOME` without relocating
`$HOME` wholesale — would need before it's worth building.
