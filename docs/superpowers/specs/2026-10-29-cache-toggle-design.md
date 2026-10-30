# Turning otto's cache off (#764)

**Status:** design, awaiting Chris's approval.
**Issue:** #764. **Base:** origin/main `9aed69752`; will be rebased onto the security follow-ups when they land.

## Why

Chris ran `otto -h`, found it slower than expected, and wanted to know how much of that is the cache: to compare otto with and without it. CPU load may explain the slowness; today there is no way to tell, because the cache is always on. The issue also names setups where keeping and validating the cache could cost more than it saves (slow NFS homes, short-lived containers, fast-changing workspaces).

So the feature has two jobs:

1. **A/B comparison.** Run the same command with the cache off and on, switching with the two verbs (see "Comparing").
2. **A lasting off switch** for a user whose setup is better off without it.

Success: a user can turn the cache off and back on with one command, see which is in effect and why, and otto behaves correctly — only slower or faster — either way.

## Goals

- `[cache] enabled` in the user's `settings.toml` (default true).
- `otto cache enable` / `otto cache disable` edit that setting in place, keeping the rest of the file (other tables, comments, layout) byte-for-byte.
- `otto cache info` says whether the cache is on and where that came from.
- One resolver decides "is the cache on"; every cache reader and writer asks it, and nothing else reads the setting or restates the default.

## Non-goals

- New benchmarking tooling. The comparison is documented, using otto's existing measure (see "Comparing"); no "uncached" surface is added to the import-budget harness (Chris).
- A per-command environment override (`OTTO_CACHE` or similar). Dropped (Chris): the verbs are the only way to switch.
- Per-cache switches (names only, tests only, …). One switch.
- Changing what the cache stores or how it validates.

## Anti-goals (a failure even if it "works")

- **Wrong answers with the cache off,** or stale answers served after it was turned off. Off must mean otto computes everything fresh.
- **A slower TAB with the cache on.** The check must add no file operations on the warm `-h` / TAB paths beyond what they already do (the TAB O(1) budget; import-budget ceilings never re-baselined upward).
- **A second place that decides the setting** — a restated default, a second TOML read, a hand-built key name.
- **Clobbering the user's settings file** — the verbs must never rewrite it from a parsed model.

## What "the cache" covers (my call)

Off means: the **completion cache** — `<workspace>/completion_cache.json` (its `names`, `shim`, `__collected_tests__`, `__dynamic_tunnels__` and `__docker_observed__` sections), its marker files `completion_cache.names.ok` / `completion_cache.tests.ok`, the collect-child state beside it (`.completion_collect.lock` / `.failed` — with no cache, no collect child is spawned), and the remote sidecar `remote_completion_cache.json`. These are everything `otto cache info` reports as cache data.

Not covered, deliberately:

- **`<workspace>/pycache/` and `pytest-cache/`.** They are not a speed-up only: they keep `__pycache__` out of the test directories, whose mtimes the test tables key on. Turning them off would change behaviour, not just speed.
- **The inventory snapshot** (`~/.otto/inventory-cache/`). It has its own off switch (`cache_ttl = "0"` per inventory) and is functional (it serves a stale snapshot when the backend is down). The docs point to that switch.
- **In-process memos** (`functools.cache`, `discover()`'s holder). They live for one process and cost nothing to keep.

Consequences of off, all correct but slower, to state in the docs:

- `otto -h` and every TAB take the full path: import every project's init code and walk the command tree each time.
- Test-name and docker completion offer only what can be computed live (test names by collection; docker names: none — a TAB never asks a host).
- `otto test` collects the whole test tree on every run instead of only changed files (Chris: off covers `otto test` too).

## How it is decided

### The resolver

`cache_enabled() -> CacheSetting` next to the user-settings loader in `otto.config.user_settings`. It returns whether the cache is on **and** its source (default, the settings file with its path, or the environment variable), because `otto cache info` and the verbs report the source. Precedence:

1. `[cache] enabled` in `user_settings_path()`'s file.
2. Default: on.

There is no environment override (Chris).

A `settings.toml` that does not parse or does not validate counts as **off** (my call): with the cache off every answer is computed fresh, so the failure is only slower, never wrong, and no command — TAB, `otto cache clear`, `info` — crashes on it. `otto cache info` shows the error as the source.

The value is resolved once per process (memoized, with a clear hook for tests). It must not add a settings read where one already happens: the warm `-h` path already loads the user settings (through the inventory fingerprint), so the builder makes both share one load per process (e.g. memoize `load_user_settings` itself, with the same clear hook) rather than reading the file twice. Warm TAB does not touch the user settings today and must not start to, except through the shim rule below.

`UserSettingsModel` gains `cache: CacheSettingsSpec | None` with `enabled: bool = True` (the model forbids extra keys, so the table needs the field). The key names (`cache`, `enabled`) are defined once and used by the model, the resolver and the verbs.

### The one off switch in code

`completion_cache._cache_path()` returns `None` when `cache_enabled()` says off. Every reader and writer of the completion cache, the remote sidecar (`remote_completion_cache._path` derives from it), tunnel ids, docker observed state, the test tables and the collect-child files already branches on `None` as "no cache"; the docstrings that say "nothing produces None today" are updated. The builder confirms every site by grep and lists any that bypass `_cache_path()`. Known so far: the shim's `locate_cache` (covered below); `suite/run.py`'s `workspace_home()` for the pycache/pytest-cache session directories (out of scope); and **the test tables on the `otto test` path**, which reach the file through `collected_tests._cache_file(workspace_home())` (`collected_tests.py:326`, from `suite/run.py` at its three call sites), not `_cache_path()`. Off covers `otto test` (Chris), so that path moves onto the switch: the `otto test` sites get the cache file from the same decision `_cache_path()` makes (one function deciding "which completion cache file, or none"; the builder picks whether `_cache_file` delegates to it or the call sites call it directly), and with no file every run collects the whole tree and stores nothing. No second check of `cache_enabled()` at those sites.

### The bash TAB shim

The shim may import only its pinned stdlib set (`test_shim.py`), so it cannot read TOML. One rule makes it obey anyway: the cache's validity depends on the user's `settings.toml`, so any change to it — by the verbs or by hand — makes the shim's stored answers stale and hands over to the full path. The full path, finding the cache off **by the settings file**, removes this workspace's completion files: the cache, its two marker files, the collect-child lock and cooldown files, and the remote sidecar — and nothing else (my call). This is a new narrow removal, not `clear_workspace()`, which also deletes `pycache/` and `pytest-cache/`; `otto cache clear` keeps using `clear_workspace()`, and both share the one list of completion file names. After the first removal there is nothing left to remove, so later off runs delete nothing. From then on the shim finds no cache file and always hands over. The builder chooses the mechanism (e.g. the user settings file as one of the stored key paths the shim already stats on its full stat pass), within the TAB budget: no extra per-TAB file operation on the marker-fresh path.

`otto cache disable` also removes the completion files of **every** workspace under the home immediately (the setting is user-wide), so the switch takes effect on the very next TAB anywhere. After a hand edit of the file instead, a workspace's shim may keep answering from its cache until its 60-second freshness window lapses (my call: acceptable — those answers were validated against the current files, so they are correct, only not yet "uncached"); the docs say `otto cache disable` applies at once.

## The verbs

`otto cache disable` and `otto cache enable`, on `cache_app` beside `info`, `clear` and `prune`.

- They edit `user_settings_path()` (so `OTTO_HOME` is honoured) with a round-tripping TOML library — `tomlkit` added as a runtime dependency (my call: rely on the library rather than hand-edit TOML text) and imported only inside the verbs, so no fast path pays for it.
- `disable` sets `[cache] enabled = false`, creating the file and the table if needed, then removes every workspace's completion files.
- `enable` sets `enabled = true` where a `[cache]` table exists; when there is no file or no table it changes nothing (on is the default) — my call, so enabling never creates a file.
- Both are idempotent and print the resulting state, its source and the file path.
- A settings file that does not parse, or does not validate as user settings, is refused with the error and left untouched (my call: the verbs never write a file otto itself cannot read).

`otto cache info` gains one line, first: on/off and its source (`default`, or `settings.toml: <path>`; for an unreadable file, the error). `clear` and `prune` work whatever the setting — removing stale data is still useful when off.

## Comparing

Docs only (Chris). The docs' new section ("Turning the cache off") shows the A/B with the verbs: `otto cache disable`, measure `otto -h`; `otto cache enable`, run `otto -h` once to rebuild the cache (disable removed it, so the first run after enable is cold), then measure it again. Measured in file operations (`strace -c -f -e trace=%file`), not wall-clock time, matching how otto's own budgets measure startup cost. It also states the expected size of the gap (warm `-h` ≈ 2.8k file ops vs uncached full path ≈ 3.3k+ on the budget's reference project).

## Tests

- Default on with no settings file, with a file without `[cache]`, and with `[cache]` lacking `enabled`.
- File over default; an unparsable or invalid file resolves off with the error as its source.
- Off → no cache file is read or written: a file-op assertion (the import-budget strace harness or an equivalent syscall spy), never wall clock.
- The shim honours a settings change (hands over, then the full path removes the leftovers), with no extra file operation on the marker-fresh TAB path.
- `otto test` with the cache off collects the whole tree and writes no cache file; on, it keeps tracking changed files.
- The verbs round-trip a settings file with other tables, comments and blank lines untouched apart from the `[cache]` key; `OTTO_HOME` honoured; idempotence; an unparsable file refused unchanged.
- `otto cache info` shows each source.
- A grep-style guard that only the resolver reads `[cache]`.
- Import-budget ceilings unchanged for the warm surfaces.

## Docs

- `docs/configuration/settings.md`: the `[cache]` table.
- `docs/cli/cache/index.md`: `enable` / `disable`, the `info` line, the "Turning the cache off" section (what off covers, what it does not, the consequences, the A/B).
- The architecture pages on the completion cache and startup performance: one sentence each linking the switch.

## Left to the builder

The memo mechanism and its clear hook; how the shim's validity comes to depend on the settings file; module placement of `CacheSettingsSpec`; the exact `info` wording; the file-op test harness.

## Decided by Chris

1. **`otto test` with the cache off:** off covers it too — every `otto test` collects the whole tree.
2. **Per-command environment override:** dropped.
3. **Measuring the gap:** docs only; no import-budget surface.
