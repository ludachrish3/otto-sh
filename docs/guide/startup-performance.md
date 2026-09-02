# Startup performance on network filesystems

otto's own engineering already removes most of what used to make startup
slow: the console script's front door, `otto._shim:main`, answers a bare
`otto --version` without ever importing the CLI (see
{doc}`../architecture/lifecycle`), and warm `otto --help` is served from a
cache that validates cheaply instead of walking your test corpus (see the
last section below). What's left after those fixes is genuinely yours to
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

otto's own release gate, `make profile`, measures **modules and file I/O,
never wall-clock**: a cap on each CLI surface's non-stdlib module count, a
golden set of the otto modules that surface may import, and — keyed per Python
minor, because the interpreter's own import machinery is part of what gets
counted — golden `os.scandir` and `os.listdir` counts plus the number of files
opened *inside the workspace under measurement*. That last one is the number
this page is about: a warm `otto --help` against a generated 50-file corpus
opens exactly **two** files in the workspace — the repo's `.otto/settings.toml`
and the completion cache under `$OTTO_HOME` — where the cold fallback that
rebuilds the cache opens 61 of them and scans 14 directories, and those same
two are also all a steady-state TAB completion costs. Counts like those are
system-agnostic in a way a timing number cannot be: the scan and
workspace-open counts came out identical on CPython 3.10 through 3.14 here,
and identical between two different virtualenvs of the same interpreter — while
across those same two venvs the process-wide `open` total moved by 9 purely
because one had nine more distributions installed for pygments' plugin lookup
to open an `entry_points.txt` in. That is exactly why wall-clock is kept as a
manual diagnostic rather than a gate: `make hyperfine` installs the tool, and
`python scripts/import_budget.py --hyperfine` reports per-surface timings for
the machine you run it on, which is the only machine they describe.

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
which point the "one open" is a network round trip like any other. Point
`OTTO_HOME` at local disk explicitly if that's your setup; see [the
workspace home](cli/index.md#the-workspace-home) for what else lives there,
and how `--clear-autocomplete-cache` clears the cache files by hand — worth
knowing precisely because `actimeo`/`nocto` (above) raise the odds of a
stale stat-based digest going unnoticed for longer.

What determines whether that one open is enough is what validating it has
to touch. The `names` section, which serves root help and completion, is
keyed to the small set of files that can actually register something: init
trees, `.otto/settings.toml`, pytest configs, top-level test files, and the
lab files — the same `lab.json` the `actimeo`/`nocto` section above warns
can go stale. That set stays small regardless of corpus size, so a warm
`otto --help` costs roughly **O(key set)**, not O(corpus) — validating it
never walks the hundreds of test files a rebuild would have to. The `tests`
section, which serves `--tests` completion, is the honest counterpoint: it
validates against the *whole* corpus walk, because nothing smaller can
answer "what tests exist right now" truthfully.

A cached payload isn't always worth writing, and otto skips it rather than
paying for it anyway in two cases: no repos were discovered to register
anything from, or the workspace's host inventory can't report a stable
freshness fingerprint to validate against later. `otto --version` is a
third, simpler case — it touches neither the cache nor the corpus, because
there's nothing in either one worth opening a file for. The cache is a
lever that pays automatically when it can; it is never a tax charged on
invocations that can't use it.
