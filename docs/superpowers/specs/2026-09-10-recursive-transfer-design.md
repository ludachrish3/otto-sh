# Recursive file transfer (`put -r` / `get -r`) — design

**Date:** 2026-09-10
**Status:** approved in brainstorm; awaiting spec review

## Summary

`Host.put` and `Host.get` learn to transfer a directory tree, opt-in through a
new `recursive` keyword (`-r`/`--recursive` on the CLI), with `cp -r`/`scp -r`
semantics: the source directory lands UNDER `dest_dir` wearing its own name.
Recursion is implemented ONCE, above every transfer backend, by reducing a
tree to the ordinary non-recursive per-file transfers the backends already
do. Every POSIX-family host (unix, docker, local) and every unix backend
(scp, sftp, ftp, nc, shell) gets it with no backend change; the embedded
family refuses it loudly.

## Compatibility verdict: NOT a breaking change

`scripts/check_breaking_marks.py` refuses only a DELETED or RENAMED golden
line in `tests/unit/api_snapshot/public_api.txt`; additions never fail it.
This design only ADDS: a trailing keyword on two `Host` protocol methods, a
CLI flag, and a new module. The two golden lines

```
otto.host.host:Host.get(src_files, dest_dir, user, show_progress)
otto.host.host:Host.put(src_files, dest_dir, mode, user, show_progress)
```

each grow by one name (`recursive`) and lose none. The existing `Result`
contract for FILE sources is untouched (see §3). A directory source was never
a supported input — nc refuses it by name, ftp PUT is documented broken, the
rest have no directory handling — so making it work behind a flag is a
feature, and making the flag-less case fail UNIFORMLY (§2) tightens an
undefined input rather than changing a defined one. Commit type: `feat`, no
`!`.

## Decisions taken in brainstorm

1. **Opt-in, `recursive=False` by default** (cp/scp), not automatic (rsync,
   `docker cp`). A bare directory source keeps failing; a scripted `put`
   cannot ship a tree by accident.
2. **Nested Result.** Top-level keys stay "the source exactly as passed"; a
   directory entry's own `value` is the per-file dict for its tree, keyed by
   path RELATIVE to the directory root.
3. **cp/scp destination layout only.** `put(Path("build"), Path("/tmp"),
   recursive=True)` lands `/tmp/build/...`, always. No rsync trailing-slash
   rule — `Path` cannot even express one. Contents-only is spelled by the
   caller (`list(build.iterdir())`).
4. **Generic walk above the backends** (approach 1). Native fast paths
   (`scp -r`, asyncssh `recurse=True`, `docker cp`, `shutil.copytree`) are a
   FOLLOW-UP optimisation (§8), addable without an API change.
5. **Embedded family refuses `recursive=True`** with `NotImplementedError`
   naming the host, the same shape as its `user` refusal.
6. Small semantics, decided rather than asked: `mode` applies to FILES only;
   `user` is unchanged (recursion reduces to ordinary per-file transfers);
   directory symlinks are NOT descended and file symlinks copy their
   target's bytes (`os.walk` defaults); empty directories ARE created;
   `recursive=True` on a plain file source is accepted and harmless.

## 1. API and CLI surface

`Host.put` and `Host.get` (protocol in `src/otto/host/host.py`, the
`BaseHost` stubs, and every family override) gain a TRAILING keyword:

```python
recursive: Annotated[bool, Opt("-r", help="Recurse into directory sources.")] = False
```

placed after `show_progress` so no existing positional or keyword moves.
The protocol docstrings gain one paragraph each pointing at the single home
for the semantics (the transfer page in the Host API docs, §7); they do not
restate them.

CLI: `otto <host> put -r build /opt` and `otto <host> get -r /var/log ./logs`
through the existing `Opt` annotation on the `@cli_exposed` overrides.
`show_progress` stays `Exclude`d as today.

## 2. The flag-less directory source: one uniform early refusal

`BaseFileTransfer.put_files` already validates sources up front (bad `mode`,
over-limit basenames — cheapest and most specific first). It gains one more
check in that preamble, for PUT: a local source that `is_dir()` is refused
BEFORE any transfer with a per-file error entry

```
<path>: is a directory (pass recursive=True, or -r on the CLI, to transfer a tree)
```

so nc's by-name refusal and ftp's silent breakage collapse into one message
on every backend. GET cannot cheaply stat a remote source in the preamble
without a command, so a remote directory source keeps each backend's own
failure; the recursive helper (§4) is the path that KNOWS it has a directory.

## 3. Result shape

Unchanged for file sources. For a directory source `D` passed to
`put([D, f], dest, recursive=True)`:

```
Result(status=<first non-ok among top-level entries>,
       value={
         D: Result(status=<first non-ok among the tree>,
                   value={Path("a.txt"):     Result(ok, value=dest/D/a.txt),
                          Path("sub/b.bin"): Result(error, msg=..., value=dest/D/sub/b.bin),
                          ...},
                   msg=<mkdir/listing message when the tree never started>),
         f: Result(ok, value=dest/f),
       })
```

Keys inside a directory entry are `Path`s relative to `D`. Skipped entries
(`"not attempted (earlier failure)"`) follow the backend's existing sequential
rule within a level; the aggregate rule (Skipped counts as ok) applies at both
levels. Empty directories produce no per-file entry; their creation failing
is a tree-level failure (§5).

## 4. The recursion helper

New module `src/otto/host/recursive_transfer.py`, two coroutines the three
POSIX families call from their own `put`/`get` when `recursive` is set,
AFTER their family preamble (user validation, `_resolve_dest`, dry-run) has
run — the helper never re-implements a family rule.

```python
async def put_tree(host, src_files, dest_dir, *, mode, user, show_progress) -> Result
async def get_tree(host, src_files, dest_dir, *, user, show_progress) -> Result
```

**PUT data flow.**

1. Partition `src_files` into files and directories by local `is_dir()`,
   which follows symlinks: a symlink to a directory passed at the TOP level
   is a directory source and is walked, as `cp -r link` does. Only directory
   symlinks ENCOUNTERED DURING the walk are left undescended.
2. Plain files go through the ordinary non-recursive `host.put(files, dest_dir,
   ...)` as one batch — their entries are lifted unchanged into the top level.
3. Each directory `D` is walked with `os.walk(D, followlinks=False)`. The walk
   yields, per level, the relative directory and its regular files (file
   symlinks included — `open()` follows them).
4. Every destination directory for every `D`, including empty ones, is
   created in ONE exec: `mkdir -p <q(d1)> <q(d2)> ...`, via the family's
   `PosixFileOps.mkdir` quoting. One command for the whole call, not one per
   level.
5. One non-recursive `host.put(level_files, dest_dir / D.name / rel, mode=mode,
   user=user, show_progress=show_progress, recursive=False)` per level that
   has files. `mode` batching, `user` authentication/chown, and per-file
   progress bars are the backends' existing ones.
6. Assemble the nested Result (§3), re-keying each level's entries from the
   absolute source path to `path.relative_to(D)`.

**GET data flow.**

1. The remote tree listing comes from a family hook,
   `PosixFileOps._walk_remote(dir) -> list[tuple[str, Path]]` (`"d"`/`"f"`,
   absolute path), issued as ONE exec of a POSIX `sh` function:

   ```sh
   w() { for p in "$1"/* "$1"/.[!.]* "$1"/..?*; do
           [ -e "$p" ] || [ -L "$p" ] || continue
           if [ -d "$p" ] && [ ! -L "$p" ]; then printf 'd %s\n' "$p"; w "$p"
           elif [ -f "$p" ]; then printf 'f %s\n' "$p"; fi
         done; }; [ -d <q(dir)> ] && w <q(dir)>
   ```

   No `find`: `find` is NOT a measured applet in `otto.host.userland`, and
   this repo's doctrine is that a userland fact that has not been measured is
   not assumed. BusyBox ash and GNU sh share this function, exactly as `glob`
   already lets the host's shell do the work. Docker runs it inside the
   container through its exec path.
2. A name containing a newline cannot ride a line-oriented listing. The
   function keeps a counter (`c=$((c+1))` per printed entry — the `for` loop
   runs in the function's own shell, so the counter is global) and prints a
   final `n <count>` line. The hook refuses the tree by name when the number
   of `d`/`f` lines it parsed differs from `<count>`: a newline inside a name
   splits one entry into two lines, so the mismatch is exact and needs no
   NUL-separated output through the exec path. The contract: a tree with a
   newline-bearing name fails as a tree-level error naming the directory,
   before any byte moves.
3. Local directories under `dest_dir / D.name` are created with
   `Path.mkdir(parents=True, exist_ok=True)`, including empty ones.
4. One non-recursive `host.get(level_files, local_level_dir, user=user,
   show_progress=show_progress, recursive=False)` per level with files.
   Docker's own `get` already stages through the parent host; the helper
   simply calls it per level, so the staging leg is unchanged.
5. Assemble the nested Result exactly as PUT does.

`LocalHost` overrides `_walk_remote` with a direct `os.walk`, no shell.

## 5. Error handling

- **Missing/unreadable source directory**: that top-level entry fails with a
  message naming it; other sources still proceed.
- **Missing plain source**: plain sources ride one batched non-recursive
  put, so a missing plain file follows the backend's own sequential rule (a
  sibling may be Skipped), exactly as a non-recursive call would.
- **Failed `mkdir -p`**: the directory entry fails BEFORE any byte moves,
  carrying the host's message; no per-file entries are fabricated for a
  skeleton that never existed.
- **Per-file failure inside a tree**: that file's own error entry, still
  carrying its `dest_path` where computed. Later files at the same level
  follow the backend's existing sequential/Skipped behaviour; later levels
  are still attempted.
- **Failed remote listing (GET)**: the directory entry fails naming the
  command's message. A newline-bearing name refuses the tree by name (§4).
- **Directory entry status**: first non-ok among its files (Skipped counts as
  ok) — the same rule the top level uses.
- **`mode` on directories**: never applied; directories keep `mkdir`'s
  default bits (a `644` on a directory would block traversal).
- **Embedded family**: `recursive=True` raises `NotImplementedError` naming
  the host and the family, before any validation of the sources.

## 6. Dry-run

- **PUT**: the local walk runs (reading the local tree issues no command) and
  every destination path is previewed as a `NotRun` entry in the nested
  shape, through the family's own per-level `put` preview. The `mkdir -p`
  is NOT issued under a dry run (a declined command would read as a failed
  skeleton and fail the tree for the wrong reason); the previewed
  destination paths already show every directory that would be created.
- **GET**: the remote listing WOULD be a command, so the directory entry is a
  single `NotRun` whose `value` is the local destination directory, with a
  `msg` saying the tree was not enumerated. Nothing under dry-run fabricates
  a listing (`refuse_declined_fact` doctrine).

## 7. Documentation

One home for the semantics: the transfer page under the Host API docs gains
a "Recursive transfers" section (layout rule, nested Result, symlink rule,
`mode`/`user` interaction, embedded refusal). The CLI reference entries for
`put`/`get` gain the `-r` row and LINK there. Protocol docstrings link, never
restate. The support-matrix page is unchanged: recursion is not a backend
surface, it is a host-level reduction to existing surfaces.

## 8. Follow-ups (filed, not built)

1. **`follow_symlinks`** flag on `put`/`get` (rsync `-L`, scp's default)
   to descend directory symlinks and copy link targets — the walk currently
   fixes `followlinks=False`.
2. **Native fast paths** (hybrid): `scp -r`, asyncssh `recurse=True`,
   `docker cp` of a whole tree, `shutil.copytree`, behind the same API,
   with the nested Result reconstructed from a post-transfer listing. Only
   if the per-level round trip proves slow on the product-and-tools use case.
3. **Embedded recursion** via Zephyr `fs ls` — needs its own measurement
   given console transfer speed.

## 9. Testing

Every test is mutated to red before it counts.

- **Unit, hermetic (`LocalHost`, real `tmp_path` tree)**: nested levels;
  empty directories created; a file symlink copied by content; a directory
  symlink not descended; mixed file + directory sources; the nested Result
  shape and re-keying; `mode` applied to files and not directories; each
  §5 branch with the hostile condition INJECTED (a failing `mkdir`, a failing
  listing, a per-file failure mid-level, a newline name); dry-run PUT
  previews and dry-run GET's single `NotRun`.
- **Listing function**: the real `sh` function runs on the runner's shell
  through `LocalHost` (calling the `PosixFileOps` implementation unbound,
  bypassing `LocalHost`'s filesystem override) on a tree containing
  dotfiles, an empty directory, a directory symlink, a dangling symlink, and
  a space-bearing name; the `LocalHost` override must agree with it. The
  loopback ssh cells (sftp, scp) exercise it end to end through the
  conformance round-trip below.
- **Flag-less refusal (§2)**: a directory source without `recursive` fails
  with the suggested-flag message on every unix backend the hermetic venue
  draws; the embedded refusal is pinned on the hermetic Zephyr double.
- **CLI**: `-r` reaches the method and appears in `--help`, through the
  CLI-subprocess e2e venue.
- **Contract, two venues**: the integration `TestTransferContract` and the
  conformance `test_transfer_contract` each gain a recursive round-trip
  (put a small tree, get it back, byte-identical compare, empty directory
  present; the embedded refusal is the contract on embedded cells). The
  conformance one runs hermetically in the default gate on `local`, the
  loopback ssh `sftp`/`scp` cells and the BusyBox artifact cells, and on
  the bed across the full `(host, term, transfer)` crossing, including
  BusyBox `shell`/`nc` and GNU `ftp`.
- **Golden**: `public_api.txt` updated for the two grown lines; the
  check-breaking gate is expected to pass unmarked (additions).
