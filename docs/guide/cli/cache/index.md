# otto cache

`otto cache` inspects, clears, and prunes the per-workspace caches otto keeps
under [the workspace home](../index.md#the-workspace-home) — the completion
cache and its remote-path sidecar, the only two files it ever removes.
Everything it removes is rebuildable: deleting it changes nothing about what
otto does next, only how much it has to redo.

```{raw} html
:file: ../../../_static/generated/termynal/help-cache.html
```

## Synopsis

```text
otto cache info
otto cache clear [--all]
otto cache prune [--age DAYS] [--dry-run]
```

| Subcommand | Description |
| ---------- | ----------- |
| `info` | List every workspace under the home, oldest cache first: key, cache size, cache age, whether an `env/` virtualenv lives there, and any other entries |
| `clear` | Remove **this** workspace's cache files (the one `OTTO_SUT_DIRS` resolves to); never removes the directory itself. `--all` clears every workspace's cache files regardless of age |
| `prune` | Remove cache files older than `--age` days (default 60) across every workspace; `--dry-run` reports what would be removed without touching anything |

`otto cache` is **lab-free** — it needs no `--lab` — and every verb acts
purely on the filesystem under `OTTO_HOME`.

## Safety model

Every verb reaches a workspace only through one narrow matcher: a candidate
must be a directory directly under the home whose name looks like a real
`workspace_key` (8 hex characters, a dash, then a slug) — `settings.toml` and
the `inventory-cache/` directory can never match that shape, so they are
structurally unreachable rather than merely skipped, and a symlinked entry is
skipped outright rather than followed, so a candidate can never be used to
reach files elsewhere on disk. Inside a matched workspace, the only files
ever unlinked are the two named above — nothing else there is touched, named
or not — and a workspace *directory* is only ever removed with `rmdir`, which
refuses to touch a non-empty directory; there is no recursive delete anywhere
in this command group. That is what makes an `env/` virtualenv (from `otto
env create`) survive every `clear` and `prune` by construction rather than by
a case that happens to notice it: emptying a workspace of its two cache files
still leaves `env/` behind, so `rmdir` fails and the directory — venv
included — stays exactly where it was.

## info

```console
$ otto cache info
home: ~/.otto (default)
╭──────────────────────┬────────────┬───────────┬──────┬───────────────╮
│ key                  │ cache size │ cache age │ env? │ extra entries │
├──────────────────────┼────────────┼───────────┼──────┼───────────────┤
│ a1b2c3d4-repo1-repo2 │        28B │       74d │      │             0 │
│ 9c0d1e2f-repo4       │        28B │       12d │      │             0 │
│ e5f6a7b8-repo3       │        14B │        3h │ yes  │             0 │
╰──────────────────────┴────────────┴───────────┴──────┴───────────────╯
3 workspace(s), 70B total, 1 older than 60d
this workspace: e5f6a7b8-repo3
  completion names: fresh — TAB is served from it (written 3h ago)
  shim: served (validated 12s ago)
  inventory: json:/home/me/.otto/inventory.json
  lab files (repo3/local): /home/me/repo3/lab/lab.json, /home/me/repo3/hosts.json
  hosts offered: 3 — dut1 dut2 local
  dropped: 1 — not offered, and why:
    [repo3] /home/me/repo3/hosts.json: element 'dut3' hosts[0]: inventory key 'dut-3' ...
```

The home line names the resolved path and says whether `$OTTO_HOME` decided it
— `(default)` above, or `(from $OTTO_HOME)` when that variable is set (and
non-empty; an empty `OTTO_HOME` counts as unset, the same as everywhere else
otto reads it). Rows are sorted oldest-cache-first, and the age column is the
sort key: it is the OLDER of the two cache files in that workspace, not the newer one —
`prune` decides per file, so a workspace with one fresh cache and one stale
one is still something a prune would act on, and reporting the newer file's
age would hide that. The caption totals the workspace count and cache bytes,
then counts how many workspaces have a cache older than the default 60-day
threshold — the same number `prune` (with no `--age`) would remove from. A
workspace `otto cache` cannot read (permission trouble, most often) is
omitted from the table rather than aborting the rest of the listing, and an
empty or missing home prints `no cached workspaces` and exits 0.

The block after the caption is about the workspace `otto` is running *in* —
the repos on `OTTO_SUT_DIRS` — and is the answer to "why does my host not
complete". Completion is best-effort by contract: a lab entry it cannot build
is skipped, never warned about, because a warning printed into a completing
shell corrupts the candidate list the shell is parsing. So the silence gets
explained here instead. `completion names` is the standing of this
workspace's cache entry — `fresh` means TAB is served from it; `stale`,
`expired` and `outdated` mean the next TAB rebuilds it; `tainted` means it was
written while startup reported errors and is never served, so every TAB runs
the full load until the error is fixed.

`shim` is the standing of the fast answer — a warm TAB is answered by the
console script from the cache alone when the entry's recorded files and
directories still stat the same (or were checked within the last minute);
`handing over — <reason>` names why the next TAB will run the full path instead
(which still answers correctly, just slower). The two `.ok` marker files beside
the cache are the minute's memory; `clear` and `prune` remove them with the
cache. See [the CLI page's completion section](../index.md#shell-completion)
for how the shim fits into completion overall.

`inventory` is the host inventory as completion resolved it — once per process,
the way commands do, so a broken `[inventory]` table shows as `BROKEN` and
empties every repo — followed by the lab files each declared source actually
read (a source on a custom backend is marked `not file-backed`). While the
inventory is broken, or cannot report freshness, nothing is written for the
workspace at all, and the standing line says so instead of promising a rebuild.
`hosts offered` lists the ids the entry holds, and `dropped` every entry the
enumeration left out, with where it was and why: a reference to an inventory
key that does not exist, a lab file that did not parse, a source that failed or
did not answer in time. The block reads the entry whatever its standing — a
stale entry still records the last enumeration, which is what the question is
about — and says `hosts offered: unknown` when no entry has been written yet.
Without a workspace (no `OTTO_SUT_DIRS`) the listing above is all there is.

## clear

```console
$ otto cache clear
removed ~/.otto/13739bf0-repo1/completion_cache.json
removed ~/.otto/13739bf0-repo1/remote_completion_cache.json
```

Bare, `clear` acts on exactly one workspace — the one the current
`OTTO_SUT_DIRS` resolves to — and unlinks both `completion_cache.json` and its
remote-path sidecar `remote_completion_cache.json` if present. It never
removes the workspace directory itself, age-blind or not: a user reaching for
this escape hatch wants completion state gone, not the directory that will
hold the next rebuild.

```console
$ otto cache clear --all
removed ~/.otto/9c0d1e2f-repo4/completion_cache.json
removed ~/.otto/9c0d1e2f-repo4/remote_completion_cache.json
removed ~/.otto/a1b2c3d4-repo1-repo2/completion_cache.json
removed ~/.otto/a1b2c3d4-repo1-repo2/remote_completion_cache.json
removed ~/.otto/e5f6a7b8-repo3/completion_cache.json
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
would remove ~/.otto/a1b2c3d4-repo1-repo2/completion_cache.json
would remove ~/.otto/a1b2c3d4-repo1-repo2/remote_completion_cache.json
2 file(s), would free 28B, 1 dir(s) would be removed, 2 workspace(s) kept: 2 young
```

`prune` is the age-aware GC: it removes cache files older than `--age` days
(default **60**, `DEFAULT_MAX_AGE_DAYS`) across every workspace under the
home, then `rmdir`s any workspace directory that removal left empty. Age is
decided per file, not per workspace, so a workspace with one stale cache file
and one fresh one still loses only the stale one. The kept count above splits
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
