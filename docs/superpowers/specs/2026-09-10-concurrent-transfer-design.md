# Concurrent file transfers — design

**Date:** 2026-09-10
**Status:** approved in brainstorming; awaiting the implementation plan
**Builds on:** `docs/superpowers/specs/2026-09-10-recursive-transfer-design.md`
(shipped as `5fa40d24`)

## 1. Goal

`Host.put` and `Host.get` transfer the files of one batch concurrently by
default, bounded by a per-transfer-protocol cap, with a switch to run one
file at a time. The switch is on the API and on the CLI. The cap is a
field on the options class of each transfer protocol that can carry more
than one file at once, beside the SSH, Telnet and FTP options that already
exist.

This is a second layer of concurrency. Fan-out across hosts already exists
through the fleet helpers (`run_on_all_hosts`, `do_for_all_hosts`) and
plain `asyncio.gather`; this feature is about the files of one batch on
one host. The two layers compose: three hosts each receiving a tree run
three independent budgets at once.

## 2. What exists today

The per-file layer already exists, unevenly:

- `ScpFileTransfer` and `SftpFileTransfer` fan out every file of a batch
  with an unbounded `asyncio.gather`, one SSH session per file on the
  shared connection. A stock OpenSSH server allows `MaxSessions 10`, so a
  batch past that size fails with channel-open errors.
- `NcFileTransfer` fans out under `asyncio.Semaphore(limit)` sized from
  `NcOptions.max_concurrent_transfers` (`None` derives `(10 - 2) // 2 = 4`,
  a bound that fits a stock server). Its dispatcher `_gather_per_file`
  wraps each per-file coroutine in the semaphore and gathers with
  `return_exceptions=True`.
- `FtpFileTransfer`, `ShellFileTransfer`, `ConsoleFileTransfer` and
  `LocalFileTransfer` loop sequentially and apply `mark_skipped`: the
  first failure stops the loop and the files never reached are answered
  `Status.Skipped`.

The base class `BaseFileTransfer.put_files`/`get_files` validates and
delegates to abstract `_run_put`/`_run_get`; every backend sequences on its
own. Transfer progress is already multi-file: the shared Rich `Live`
draws one row per file across every concurrent transfer.

## 3. Contract

### 3.1 API

```python
async def put(self, src_files, dest_dir, *, mode=None, user=None,
              show_progress=True, recursive=False, concurrent=True) -> Result
async def get(self, src_files, dest_dir, *, user=None,
              show_progress=True, recursive=False, concurrent=True) -> Result
```

`concurrent` is appended after `recursive` on the `Host` protocol and on
every family (`UnixHost`, `LocalHost`, `DockerContainerHost`,
`EmbeddedHost`). It decides the in-flight count only:

- `concurrent=True`: the backend may have up to its concurrency limit
  files in flight at once.
- `concurrent=False`: exactly one file is in flight at a time, in the
  order given.

In both modes every source is attempted and answers its own entry. No
source is ever `Skipped` because a sibling failed. The nested `Result`
shape (`value` = `dict[Path, Result]` keyed by source; tree entries nested
under the tree root as the recursive spec defines) is unchanged.

Families whose transfer can carry only one file at a time (shell, console,
ftp, the local copy) accept `concurrent=True` and run one at a time. That
is a documented no-op, not an error. `DockerContainerHost` forwards
`concurrent` to the parent host's staging leg; the `docker cp` leg is one
call per batch and is unaffected.

### 3.2 CLI

`put` and `get` gain `--concurrent/--no-concurrent`, synthesized from the
bool by `param_synth` exactly as `--recursive/--no-recursive` is. No short
alias. The verbs remain single-host, as today.

### 3.3 Options

`ScpOptions` and `SftpOptions` gain

```python
max_concurrent_transfers: int | None = None
```

with the same meaning as `NcOptions.max_concurrent_transfers`: how many
files of one batch may be in flight at once; `None` derives a bound that
fits a default OpenSSH server. Shell, console, ftp and local have no
field; the docs list them as fixed at one.

One helper, moved out of `nc.py` into the transfer base module, derives
the default from a stock `MaxSessions` of 10 with a headroom of 2 (the
pooled control session and the exec the caller may already be inside),
divided by the channels one transfer holds. nc holds two per file (the
listener and its readiness poll), which is its existing
`(10 - 2) // 2 = 4`. scp and sftp hold one session per file, and their
default halves the same 8 usable channels to 4: a batch at full fan-out
then takes only half the connection's budget and the other half is still
there for whatever else the caller runs on that connection while the batch
is in flight -- its own exec calls, a second transfer on another backend,
an interactive session. (A `user=` transfer does NOT share this budget: it
authenticates separately over its own `ssh_connect`, with its own
`MaxSessions`.) A configured
value below one is refused where the backend is built, naming the option
(`ValueError`), as nc does.

`TransferContext` gains `sftp_options` so the sftp backend can read its
cap; today it carries only `nc_options` and `scp_options`.

## 4. Transfer layer

### 4.1 One dispatcher, overridable

`BaseFileTransfer` gains one concrete dispatcher and a limit every backend
answers:

    @property
    def concurrency_limit(self) -> int          # >= 1; base answers 1

    async def _dispatch_per_file(self, src_files, transfer_one, *, concurrent) -> dict[Path, Result]

`_run_put`/`_run_get` stay abstract and gain a keyword-only `concurrent`.
Each backend keeps its per-batch preamble (codec selection, warm-up,
connection open, refusals) and then hands ONE per-file coroutine to the
dispatcher, exactly the shape nc's `_gather_per_file` has today. The
dispatcher acquires the instance's semaphore (sized from
`concurrency_limit`) around EVERY file in BOTH modes; with
`concurrent=True` it gathers, with `concurrent=False` it awaits the files
in order. An exception from a per-file coroutine folds into that file's
`Result(Status.Error, msg=...)`; the batch continues. Cancellation
propagates: a cancelled gather stops in-flight files and fabricates no
Result.

The semaphore is one per transfer object, so it bounds every batch and
every concurrently running batch on that object, including the levels of a
tree (section 5) and overlapping calls from a caller's own gather. It is
created lazily on the first dispatch, not when the backend is built (a
subclass sets the options `concurrency_limit` reads after `__init__`
runs), and it is keyed to the loop that created it: `asyncio.Semaphore`
binds to the first loop that waits on it, so an object reused across
separate `asyncio.run` calls gets a fresh semaphore rather than a
`RuntimeError` per queued file. The old loop's tasks cannot still be
running, so rebuilding loses no permit.

The dispatcher is a default, not a mandate. A backend may skip the
dispatcher inside its own `_run_put`/`_run_get` to hand the whole batch to
its library (asyncssh's `scp()` takes a list of sources; the SFTP client's
`put`/`get` take `max_requests`). The override is bound by the contract in section 3,
which the conformance test checks, not by the base code. This is how the
native fast paths deferred in the recursive spec will land without
changing the API.

### 4.2 Per backend

| Backend | `concurrency_limit` | Change |
|---|---|---|
| scp | `scp_options.max_concurrent_transfers` or derived | unbounded gather → dispatcher |
| sftp | `sftp_options.max_concurrent_transfers` or derived | unbounded gather → dispatcher |
| nc | `nc_options.max_concurrent_transfers` or derived | its own dispatcher → the shared one; control-plane locks stay |
| ftp | 1 | loop + `mark_skipped` → `_put_one`/`_get_one` under `_ftp_lock` |
| shell | 1 | loop + `mark_skipped` → `_put_one`/`_get_one` |
| console (embedded) | 1 | loop + `mark_skipped` → `_put_one`/`_get_one` |
| local | 1 | loop + `mark_skipped` → `_put_one`/`_get_one` |
| tftp | 1 | stays `NotImplementedError` |

`mark_skipped` is deleted with the loops that used it. The
`aggregate_transfer` fold is unchanged: first non-ok status, all non-ok
messages joined, `value` the per-file mapping.

### 4.3 Threading the flag

`put_files`/`get_files` gain `concurrent: bool = True` after
`show_progress` (and `mode` for put). `UnixHost.put/get` pass it to
`_transfer_for(user).put_files/get_files`; `LocalHost` and `EmbeddedHost`
pass it to their single backend; `DockerContainerHost` passes it to
`self.parent.put/get` for the staging leg.

### 4.4 Docker's two legs

`DockerContainerHost` stages through the parent host, then runs one
`docker cp` per file. Today both legs carry the stop rule: a failed
staging batch downgrades every staged-ok file to `Skipped`
("docker cp not attempted (staging batch failed)"), and on `get` one
failed `docker cp` marks the earlier files "staged but not fetched" and
the later ones "not attempted". Under this contract the parent leg
attempts every file, so the container leg does too: every file whose
staging entry is ok is copied, sequentially (the `docker cp` leg is fixed
at one), a `docker cp` failure is that file's `Error`, and the
parent-leg failure of a file is that file's entry unchanged. The three
leg-level `Skipped` downgrades are deleted.

Staging is per CALL:
`/tmp/otto-docker-stage/<container_id>/<uuid>/` on the parent filesystem,
not one directory per container. Section 5 fans a tree's levels out and every level calls
`self.put`, so a directory shared between two in-flight calls would have
one call's `finally: rm -rf` carry off the other's staged files mid-copy.
The uuid segment is what keeps the two apart.

## 5. Trees

`put_tree` and `get_tree` (recursive_transfer.py) build the per-level
`host.put`/`host.get` coroutines after the one skeleton mkdir, then run
them with the same rule: together under `asyncio.gather` when
`concurrent=True`, one after another when `False`. The tree obeys the
protocol's cap through the backend's own semaphore, since every level's
put on the same host shares the one transfer object. Per-level Results
are folded into the tree entry exactly as today. The dry-run and
listing-failure paths are unchanged.

## 6. Errors and dry run

- A file's failure is that file's entry only. Exceptions fold into
  `Status.Error`; the batch continues; `aggregate_transfer` reports the
  first non-ok status.
- `max_concurrent_transfers < 1` raises `ValueError` naming the option
  when the backend is built.
- Dry run is unchanged: every file is `NotRun` before any dispatch, in
  either mode; a files-less tree stays `NotRun` "directories only".
- Cancellation propagates out of the gather; nothing is retried.

## 7. Compatibility

Not breaking. Both `Host` protocol lines widen by a trailing defaulted
keyword; `scripts/check_breaking_marks.py` reports them as widenings and
verifies the defaults from the live signature. `put_files`/`get_files` on
the backends widen the same way.

Two behaviour changes are documented, not marked:

1. scp and sftp batches larger than the cap now queue instead of failing
   past a stock server's `MaxSessions`.
2. Sequential backends no longer mark siblings `Skipped` after a failure;
   every file is attempted. Nothing in `src/` reads `Status.Skipped` back
   out of a transfer result (grep on 2026-09-10: the only transfer-side
   producers are `mark_skipped`, docker's three leg downgrades, and the
   directory-source refusal). The `Host.put`/`Host.get` protocol
   docstrings, which describe the "not attempted (earlier failure)" entry,
   are rewritten to the new rule.

One `Skipped` stays: a non-recursive batch containing a directory source
is refused before dispatch and its siblings are `Skipped`
("not attempted (directory source refused)"). That is a refusal of the
caller's input, not a sibling's failure, and the recursive-transfer spec
owns it.

## 8. Docs

One home: a "Concurrent transfers" section in `docs/api/host/transfer.rst`,
linked from `docs/guide/cli/host/put.md` and `get.md` and from
`docs/guide/configuration/host-options.md`, where `max_concurrent_transfers`
gains rows under `scp_options` and `sftp_options` beside nc's existing
"Concurrency and the remote channel budget" subsection (which becomes the
shared explanation of the derived default). The generated families page
notes which families are fixed at one, via the family capability notes.
Both behaviour changes from section 7 are named in the section and in the
squash subject.

## 9. Testing

Unit:

- Base dispatcher, with a fake backend recording the peak in-flight count:
  bounded by the cap when concurrent; exactly one when not; every file
  attempted after a failure; exceptions folded into that file's entry;
  order preserved when sequential; cancellation propagates.
- Each backend answers its `concurrency_limit` (1 for shell, console,
  ftp, local; from options for scp, sftp, nc); nc keeps its control-plane
  locks.
- The derivation helper pins the stock default and refuses `< 1`.
- `UnixHost`, `LocalHost`, `EmbeddedHost` and `DockerContainerHost` pass
  `concurrent` through (docker to the parent's staging put); docker copies
  every staged-ok file after a partial staging failure and answers a
  `docker cp` failure as that file's `Error` with its siblings still
  copied.
- CLI help shows `--concurrent/--no-concurrent` on `put` and `get`.
- Trees: levels launch together under one semaphore when concurrent, in
  order when not.
- Golden `public_api.txt` shows the two widened lines.
- The `mark_skipped` pins are removed with the rule.

Conformance: `test_put_get_batch_lands_every_file_in_both_modes` sends a
batch larger than the cap (cap + 3 files) in both modes on every cell and
checks every file lands byte-for-byte; a positive control proves the
in-flight count is bounded on the hermetic sftp cell (a probing
`_get_one` wrapper that fails if the count ever exceeds the cap). The
support matrix gains a `transfer-concurrent` surface whose cells start
`untested` until the next bed run.

Gates: the usual chain (`make coverage`, `coverage-unit`, `lint-python`,
`typecheck-python`, `lint-arch`, `docs`, `nox -s tests_hostless-3.14`,
import budget) before the squash.

## 10. Follow-ups (not built here)

- Native batch paths per backend: asyncssh `scp()` with a source list,
  sftp `max_requests`/`block_size` mapped from the cap.
- A `HostCapabilities` field for the concurrency limit so the support
  matrix can render it, and so the fixed-at-one families can be asserted
  hermetically.
- `follow_symlinks`, native `-r` paths and embedded recursion stay filed
  in the recursive-transfer spec.
