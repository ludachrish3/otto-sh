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
a file otto reads — a shared `lab.json`, a settings file, a suite someone
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

## What holds these numbers in place

otto's own release gate, `make profile` (`scripts/import_budget.py`),
measures **modules, file I/O and path lookups, never wall-clock**. For each
CLI surface it keeps a cap on the non-stdlib module count, a golden set of the
otto modules that surface may import, and an I/O golden per Python minor,
because the interpreter's own import machinery is part of what gets counted.
The I/O golden holds two kinds of counter.

**Audit-hook counters**, from Python's own audit events: `os.scandir` and
`os.listdir` calls, and the files opened inside the workspace under
measurement (`open_fixture`), of which `open_home` counts the ones under
`$OTTO_HOME`. These are gated exactly.

**strace counters.** CPython has no audit event for a `stat`, and a network
filesystem charges a round trip for each one, so the harness runs every
surface under `strace -f` and counts the stat family (`stat`, `lstat`,
`newfstatat`, `statx`, `access`, `faccessat` and their variants) made by the
measured Python process. Calls made by child processes such as git or ssh
are the command's own work and are not counted. There are two counters, with
two different gates:

- **`stat_workspace`** counts the calls on paths inside the generated repos
  and the surface's `$OTTO_HOME`. The repos' lib directories on `sys.path`
  are left out, because the import system stats those once per later import
  and the module caps already bound that. What remains is otto's own logic,
  which is deterministic: two warm runs agree exactly. So the golden is
  **exact**, and a single new stat inside the workspace fails it.
- **`stat_total`** counts every stat-family call in the process. It is
  dominated by the interpreter's import machinery, which moves by a few
  calls between identical runs, so it cannot be exact. The golden records a
  baseline, and a measurement more than **10%** above it fails. This is the
  net for a large new cost anywhere in the process, which the workspace
  slice alone would not show.

strace is required. Without it the budget tests fail with an install hint
instead of skipping, because a guard that quietly measured less would pass
the very regressions it exists to catch.

For scale: a warm `otto --help` against a generated 50-file corpus opens
exactly **two** files in the workspace — the repo's `.otto/settings.toml`
and the completion cache under `$OTTO_HOME` — where the cold fallback that
rebuilds the cache opens 61 of them and scans 7 directories. Counts like
those are system-agnostic in a way a timing number cannot be: the scan and
workspace-open counts came out identical on CPython 3.10 through 3.14 here,
and identical between two different virtualenvs of the same interpreter — while
across those same two venvs the process-wide `open` total moved by 9 purely
because one had nine more distributions installed for pygments' plugin lookup
to open an `entry_points.txt` in.

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
O(corpus). A `--tests` TAB is the honest counterpoint: it validates against
the *whole* corpus walk, because nothing smaller can answer "what tests exist
right now" truthfully. The key sets, the digests and when the cache is
rebuilt are described on {doc}`subsystems/completion-cache`.

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
loads test files only if it reads suites. On the import budget's generated
repo (50 test files, CPython 3.10) the ordinary-dispatch surface went from 584
to 466 non-stdlib modules, from 118 to 20 stat calls inside the workspace, and
from 3,256 to 2,641 stat calls in total.

The rebuild itself got cheaper at the same time, which matters because a TAB
that finds the cache stale now pays for it. A rebuild used to stat each
nested test file about four times and list each directory five times; it now
walks the corpus once, so between the budget's 50-file and 200-file repos the
150 added files and 15 added directories cost 165 extra stats inside the
workspace instead of 720.

## When `$HOME` is on NFS

Everything above is about otto's own venv, your project tree, and
`sys.path` — filesystems you choose where to put. This section is about the
one otto puts things on for you: its own derived-state home, `$OTTO_HOME`
(default `~/.otto`), which is `$HOME` itself on plenty of real deployments —
the one directory you didn't get to relocate just by moving your checkout.

### The measured footprint

A warm `otto --help` touches the home exactly **three** times for an *empty*
home — no user `settings.toml`, no cached inventory backend, the same
configuration the release-profile golden measures (see [What holds these
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
gated, not just measured: the open by `open_home`, and the two stats by
`stat_workspace`, which strace counts for every path under the surface's
`$OTTO_HOME` as well as inside the repos (see [What holds these numbers in
place](#what-holds-these-numbers-in-place)). A cold invocation, with no
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
