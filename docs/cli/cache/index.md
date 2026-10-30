# otto cache

`otto cache` inspects, clears, and prunes the per-workspace caches otto keeps
under [the workspace home](../index.md#the-workspace-home) — the completion
cache and its remote-path sidecar (with the shim's two marker files), and the
two directories otto's pytest sessions write: `pycache/` (their bytecode) and
`pytest-cache/` (pytest's cache). Nothing else is ever removed. It can also
switch the cache off and on again: see
[Turning the cache off](#turning-the-cache-off).
Everything it removes is rebuildable: deleting it changes nothing about what
otto does next, only how much it has to redo. What the completion cache holds,
when otto trusts it, and which invocations rebuild it is explained on
{doc}`the completion-cache architecture page <../../architecture/subsystems/completion-cache>`.

```{raw} html
:file: ../../_static/generated/termynal/help-cache.html
```

## Synopsis

```text
otto cache info
otto cache enable
otto cache disable
otto cache clear [--all]
otto cache prune [--age DAYS] [--dry-run]
```

| Subcommand | Description |
| ---------- | ----------- |
| `info` | List every workspace under the home, oldest cache first: key, cache size (the two session directories included), cache age, whether an `env/` virtualenv lives there, and any other entries |
| `enable` | Turn the cache on: set `[cache] enabled = true` in the user settings file where a `[cache]` table exists, even an empty one; never creates the file |
| `disable` | Turn the cache off: write `[cache] enabled = false`. Deletes nothing; old cache files stay until `clear` or `prune`. See [Turning the cache off](#turning-the-cache-off) |
| `clear` | Remove **this** workspace's cache files and session directories (the workspace `OTTOPATH` resolves to); never removes the workspace directory itself. `--all` clears every workspace regardless of age |
| `prune` | Remove cache files older than `--age` days (default 60) across every workspace, and the session directories of each workspace left with no younger cache file; `--dry-run` reports what would be removed without touching anything |

`otto cache` is **lab-free** — it needs no `--lab` — and every verb acts
purely on the filesystem under `OTTO_HOME`.

## Safety model

Every verb reaches a workspace only through one narrow matcher: a candidate
must be a directory directly under the home whose name looks like a real
`workspace_key` (8 hex characters, a dash, then a slug) — `settings.toml` and
the `inventory-cache/` directory can never match that shape, so they are never
reached, and a symlinked entry is skipped rather than followed, so a candidate
can never be used to reach files elsewhere on disk. The same holds for the
current workspace that bare `clear` and every root help act on: if it is a
symlink, nothing in it is touched. Inside a matched
workspace, the only entries ever removed are the ones named above — nothing
else there is touched, named or not. The two session directories are emptied
from the bottom up: each file is unlinked, each directory removed with
`rmdir` once it is empty, and a symlink is unlinked, never followed (a
session directory that is itself a symlink is left alone). A workspace
*directory* is only ever removed with `rmdir`, which refuses to touch a
non-empty directory; there is no recursive delete of anything else. So an
`env/` virtualenv (from `otto env create`) survives every `clear` and
`prune`: emptying a workspace of its caches still leaves `env/` behind, so
`rmdir` fails and the directory — venv included — stays exactly where it
was.

## info

```console
$ otto cache info
cache: on (default)
home: /home/me/.otto (default)
╭────────────────────────────┬────────────┬───────────┬──────┬───────────────╮
│ key                        │ cache size │ cache age │ env? │ extra entries │
├────────────────────────────┼────────────┼───────────┼──────┼───────────────┤
│ a1b2c3d4-project1-project2 │        28B │       74d │      │             0 │
│ 9c0d1e2f-project4          │        28B │       12d │      │             0 │
│ e5f6a7b8-project3          │        14B │        3h │ yes  │             0 │
╰────────────────────────────┴────────────┴───────────┴──────┴───────────────╯
3 workspace(s), 70B total, 1 older than 60d
this workspace: e5f6a7b8-project3
  completion names: fresh — TAB is served from it (written 3h ago)
  shim: served (validated 12s ago)
  test names: served (42 names; checked 95s ago, the next check is due in 505s)
  collect child: idle
  docker observed: images on test3; containers on test3
  inventory: json:/home/me/.otto/inventory.json
  lab files (project3/local): /home/me/project3/lab/lab.json, /home/me/project3/hosts.json
  hosts offered: 3 — dut1 dut2 local
  dropped: 1 — not offered, and why:
    [project3] /home/me/project3/hosts.json: element 'dut3' hosts[0]: inventory key 'dut-3' ...
```

The first line is the cache switch: `cache: on` or `cache: off`, then in
parentheses where that was decided: `default` (nothing set it),
`settings.toml: <path>` (the user settings file has a `[cache]` table, even one without `enabled`), or, when that file
does not parse or validate, or cannot be read at all (a directory where the
file should be, a permission error), the one-line error itself, since a broken
file means the cache is off.

The home line names the resolved path and says whether `$OTTO_HOME` decided it
— `(default)` above, or `(from $OTTO_HOME)` when that variable is set (and
non-empty; an empty `OTTO_HOME` counts as unset, the same as everywhere else
otto reads it). Rows are sorted oldest-cache-first, and the age column is the
sort key: it is the OLDER of the two cache files in that workspace, not the
newer one, because `prune` decides per file and still acts on a workspace with
one fresh cache and one stale one. The caption totals the workspace count
and cache bytes, then counts how many workspaces have a cache older than the
default 60-day threshold — the same number `prune` (with no `--age`) would
remove from. A workspace `otto cache` cannot read (permission trouble, most
often) is omitted from the table rather than aborting the rest of the listing,
and an empty or missing home prints `no cached workspaces` and exits 0.

The block after the caption is about the workspace `otto` is running *in* —
the projects on `OTTOPATH` — and is the answer to "why does my host not
complete". Completion skips a lab entry it cannot build without printing a
warning into the shell, so this block is where those skips are explained.
`completion names` is the standing of this workspace's cache entry — `fresh`
means TAB is served from it; `stale`, `expired` and `outdated` mean the next
TAB rebuilds it; `tainted` means it was written while startup reported errors
and is never served, so every TAB runs the full load until the error is
fixed.

`shim` is the standing of the fast answer — a warm TAB is answered by the
console script from the cache alone when the entry's recorded files and
directories still stat the same (or were checked within the last minute);
`handing over — <reason>` names why the next TAB will run the full path instead
(which still answers correctly, just slower). See
[the CLI page's completion section](../index.md#shell-completion) for how the
shim fits into completion overall.

`test names` is what the next test-name TAB would do: answer from the
test-names cache, with how many names it offers, when the test files were
last checked and when the next check is due; or hand over, and why (for
instance, no cache yet for a project, which the TAB then collects).
`collect child` is the background process that collects the test files:
`idle`, `running`, or `cooling down` after it failed, with the reason it
recorded. A background collection has no terminal to print to, so this line
is where its failure shows. How the test-names cache is checked and
refreshed is on
[Tab-completing names](../test/selection.md#tab-completing-names).

`docker observed` lists the docker hosts whose last-seen images or containers
the cache still vouches for, or `nothing observed`; what records them and
for how long is on [docker's completion section](../docker/index.md#completion).

The two `.ok` marker files beside the cache record the last check of each
kind (the one-minute window of the `shim` line, and the last check of the
test files); `clear` and `prune` remove them with the cache.

`inventory` is the host inventory as completion resolved it — once per process,
the way commands do, so a broken `[inventory]` table shows as `BROKEN` and
empties every project — followed by the lab files each declared source actually
read (a source on a custom backend is marked `not file-backed`). While the
inventory is broken, or cannot report freshness, nothing is written for the
workspace at all, and the standing line says so instead of promising a rebuild.
`hosts offered` lists the ids the entry holds, and `dropped` every entry the
enumeration left out, with where it was and why: a reference to an inventory
key that does not exist, a lab file that did not parse, a source that failed or
did not answer in time. The block reads the entry whatever its standing — a
stale entry still records the last enumeration, which is what the question is
about — and says `hosts offered: unknown` when no entry has been written yet.
Without a workspace (no `OTTOPATH`) the listing above is all there is.

## enable and disable

```console
$ otto cache disable
cache: off (settings.toml: /home/me/.otto/settings.toml)
/home/me/.otto/settings.toml
$ otto cache enable
cache: on (settings.toml: /home/me/.otto/settings.toml)
/home/me/.otto/settings.toml
```

`disable` writes `[cache] enabled = false` into the user settings file
({doc}`../../configuration/settings`), creating the file and the table when
they are missing, and edits in place, so comments and other tables are kept.
It prints the new state and the settings file path. It deletes nothing: the
cache files already in each workspace stay where they are, unread, until
`otto cache clear` or `otto cache prune` removes them.

`enable` sets `enabled = true` only where a `[cache]` table already exists
(an empty `[cache]` table gets the key written) and never creates a file,
since no table already means on. Both verbs are idempotent, and both refuse
a settings file that does not parse or validate, or cannot be read: they
print its one-line error, exit 1 and leave the file as it was.
`clear` and `prune` work whether the cache is on or off.

## Turning the cache off

`[cache] enabled = false` in the user settings file (`$OTTO_HOME/settings.toml`,
else `~/.otto/settings.toml`) turns otto's cache off; the default is on.
`otto cache disable` and `otto cache enable` are the verbs for it, and
`otto cache info` shows the state on its first line.

**What off covers.** Off bypasses the cache: nothing reads, writes or deletes
a completion file in any workspace. That is `completion_cache.json` (the names
TAB offers), `remote_completion_cache.json` (remote paths TAB has seen), the
two `.ok` marker files, and the `.completion_collect.lock` and
`.completion_collect.failed` files of the background process that collects
test names. It also covers `otto test`'s record of which test files changed:
every `otto test` run collects the whole test tree. Files written while the
cache was on stay where they are, unread, until `otto cache clear` or
`otto cache prune` removes them.

**What it does not.** `pycache/` and `pytest-cache/` stay: they keep bytecode
out of test directories, which is not a cache you can do without. The
inventory snapshot has its own switch, `cache_ttl = "0"`
(see {doc}`../../configuration/inventory`).

**Consequences.**

- `otto -h` and every TAB take the full path.
- Test names and markers are collected live on every test-name or `-m` TAB:
  the collect child runs, waited for, stores nothing and hands back what it
  found. It is bounded by the same 15-second cap as a cold TAB with the cache
  on, but with no cooldown, since there is nowhere to record one: a project
  that cannot collect costs up to the cap on each such TAB, and offers
  nothing.
- Docker container and image names come only from the cache's observed state
  and a TAB never asks a host, so none are offered.
- `otto test` collects everything on every run.

**When it takes effect.** `otto -h` and every other full command see the
change on their next run. A bash TAB may go on answering from the old cache
for up to about 60 seconds after the change (until its next check of the
files the cache depends on, `settings.toml` among them); from then on every
TAB takes the full path, because off writes nothing the check could match
again. A settings file that does not parse or validate, or cannot be read,
means the cache is off, and `otto cache info` shows why.

**Comparing with and without the cache.** Count file operations, never wall
clock:

```console
$ otto cache disable
$ strace -c -f -e trace=%file,getdents64 otto -h > /dev/null
$ otto cache enable
$ otto -h > /dev/null          # rebuilds the cache: the settings change made it stale
$ strace -c -f -e trace=%file,getdents64 otto -h > /dev/null
```

Compare the `calls` figure on the `total` row (`strace -c` prints to stderr,
so `> /dev/null` keeps the table visible). The import budget's reference
project gives the scale: the harness's measured baselines in
[`3.12.json`](https://github.com/ludachrish3/otto-sh/blob/main/tests/unit/import_budget/ceilings/3.12.json)
are about 3.3k file operations for `help_project`, a cold first run (no cache
yet, a full bootstrap that also writes one), against about 2.8k for
`help_project_warm`, a warm run. A cache-off run never writes the cache, so its
cost is at or below the cold figure. The gap grows with what your projects'
init modules import, since a cache-off run imports them every time.

## clear

```console
$ otto cache clear
removed /home/me/.otto/13739bf0-project1/completion_cache.json
removed /home/me/.otto/13739bf0-project1/remote_completion_cache.json
removed /home/me/.otto/13739bf0-project1/pycache
removed /home/me/.otto/13739bf0-project1/pytest-cache
```

Bare, `clear` acts on exactly one workspace — the one the current
`OTTOPATH` resolves to — and unlinks both `completion_cache.json` and its
remote-path sidecar `remote_completion_cache.json` if present, then removes
the `pycache/` and `pytest-cache/` directories. It never removes the workspace
directory itself. The next `otto test` compiles its test files again. It
names each file it could not remove on stderr (`could not remove <path>`)
and exits 1.

```console
$ otto cache clear --all
removed /home/me/.otto/9c0d1e2f-project4/completion_cache.json
removed /home/me/.otto/9c0d1e2f-project4/remote_completion_cache.json
removed /home/me/.otto/a1b2c3d4-project1-project2/completion_cache.json
removed /home/me/.otto/a1b2c3d4-project1-project2/remote_completion_cache.json
removed /home/me/.otto/e5f6a7b8-project3/completion_cache.json
5 file(s), 70B freed, 2 dir(s) removed, 1 workspace(s) kept: 1 non-empty
```

`--all` is age-blind — every workspace's cache files go regardless of how
recently they were written — and reports both halves of what it did: files
removed and, wherever removing them left a workspace directory empty, the
directory removed with it. The one workspace kept above is the one with an
`env/`: its cache file is gone, but the directory survives.

## prune

```console
$ otto cache prune --dry-run
would remove /home/me/.otto/a1b2c3d4-project1-project2/completion_cache.json
would remove /home/me/.otto/a1b2c3d4-project1-project2/remote_completion_cache.json
2 file(s), would free 28B, 1 dir(s) would be removed, 2 workspace(s) kept: 2 young
```

`prune` is the age-aware GC: it removes cache files older than `--age` days
(default **60**, `DEFAULT_MAX_AGE_DAYS`) across every workspace under the
home, then `rmdir`s any workspace directory that removal left empty. Age is
decided per file, not per workspace, so a workspace with one stale cache file
and one fresh one still loses only the stale one. The session directories
(`pycache/`, `pytest-cache/`) have no age of their own: they go with the
workspace's cache, removed wherever no cache file younger than `--age` is
left. The kept count above splits
its reason — **young** (under the age cutoff) versus **non-empty** (an `env/`
virtualenv, or a file `prune` could not remove) — the same two causes `clear
--all` reports. `--dry-run` runs the exact
same walk and reports the same totals, but every clause describing an action
is tensed as hypothetical — "would remove", "would free", "would be
removed" — and nothing on disk changes; run it before a real `otto cache
prune` to see what a given `--age` would touch. Concurrency needs no special
handling: an invocation that prunes a cache another process is mid-read of is
still safe, because the reader either holds an already-open file descriptor
(unaffected by the unlink) or misses and falls back to a full rebuild — there
is no locking anywhere in this command group.
