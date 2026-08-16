# The failure-message relay: six library verbs that read a decline's `.value`

> **One of four dry-run leftovers.** Siblings:
> [dry-run-followups-2026-08-15.md](dry-run-followups-2026-08-15.md)
> (presentation), [dry-run-ledgered-minors-2026-08-16.md](dry-run-ledgered-minors-2026-08-16.md)
> (the five minors the final review kept), and
> [plan-and-review-practice-2026-08-16.md](plan-and-review-practice-2026-08-16.md)
> (process). This file is the only one of the four that is a correctness
> hazard — do it first if only one gets done.

Found by the whole-branch review of the dry-run contract work. **No task on that
branch claimed this family and the roll-up missed it**, which is the only reason
it is written down rather than fixed: it is a package-sized job and the branch
is already long. Every site below is library-only (no CLI path reaches them
under `-n` today) and every one of them is currently **loud** — they raise
`CommandNotRunError` rather than fabricating — so nothing is shipping a lie
right now. The hazard is the shape, not the current behaviour.

It is the same class the branch fixed twelve of in `otto.docker.compose` (the
compose-package sweep; spec
`docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`), and the
thirteenth — `DockerContainerHost._auto_up` — was closed on the same branch as
this entry was written.

## The shape

A verb runs a command, gets a `CommandResult` back, and its last line builds a
human message out of `result.value` (or `result.only.value`) — a line whose
whole job is to RELAY what the device said. Under a dry run the result is a
`NotRunResult`, `.value` raises, and the traceback comes out of a message
builder. Loud, but pointed at the wrong place: the reader is told a message
could not be formatted, not that nothing was run.

The mechanical fix is one check ABOVE the failure-message line — a decline test
(`result.status is Status.NotRun`, or the `refuse_declined_*` helper the return
type calls for) that returns or raises before anything reads `.value`. It is the
same edit six times, which is why it should be done as one pass rather than
opportunistically.

## Verified sites

- `src/otto/host/file_ops.py:192` (`mkdir`), `:200` (`rm`), `:209` (`cp`),
  `:217` (`mv`) — all four end `return Result(result.status, msg=result.value)`.
  The status they return is honest (`NotRun`); building `msg` is what raises.
  Note `write_file` in the same file (`:362-366`) already has its own dry-run
  arm above this line, so it is NOT part of the family — it is the model.

- `src/otto/host/embedded_host.py:570` — `EmbeddedHost.rm`, the same
  `return Result(result.status, msg=result.value)` as the base `rm`.

- `src/otto/host/unix_host.py:984` — `unload`'s
  `f"rmmod {resolved} failed: {result.only.value.strip()}"`.

- `src/otto/host/unix_host.py:950` — `load`'s
  `f"staging {file} failed: {put_result.msg}"`. This one reads `.msg`, so it
  does NOT raise — it composes. `put` declined, so the message says a staging
  **failed** about a thing that was never attempted, and the `[DRY RUN] PUT:`
  banner `put` honestly produced is embedded inside the word "failed". Wrong
  story, quietly, which makes it the worst of the six.

- `src/otto/host/file_ops.py:287` — `read_file`'s failure arm,
  `raise FileNotFoundError(f"read_file({path!r}) failed: {result.value}")`.

### `read_file:287` is why this entry exists

Its outcome today is CORRECT: `read_file` returns `str`, a `str` cannot carry
"I did not look", so declining by raising is the only honest behaviour available
(`refuse_declined_fact`'s argument, in a third shape). But it is correct **by
accident**. Nothing at that line intends a decline — `.value` raises on the way
into an f-string, and the exception that escapes happens to be the right one.

That makes the obvious tidy-up actively dangerous. Anyone cleaning this family
up mechanically will convert `result.value` → `result.msg` (the fix that is
right for the four `Result(...)` sites, because `.msg` does not raise), and at
`:287` that turns a correct decline into a **fabricated `FileNotFoundError` for
a file nobody checked** — a caller's `except FileNotFoundError: create_it()`
then writes a file over a device that was never read. The sweep must treat this
site as a deliberate raise and give it an explicit `CommandNotRunError` decline
above the failure arm, not a `.msg` swap.

## Sibling: the last `is_ok=True` dry-run arm in the tree

`src/otto/host/userland.py:1626-1627` — the `probe` verb answers
`Result(Status.Skipped, value=_dry_run_report())` under a dry run, so
`(await host.probe()).is_ok` reads **True** for a probe that measured nothing.
Nothing is fabricated: the payload is honest announcement text and the real
guard is in `Userland._send`. But it is the last `is_ok=True` dry-run arm left,
and it is the same shape the owner's `write_file` ruling flipped to `NotRun`.
Candidate for the same flip. Ledger only — check the `otto host <id> probe` CLI
exit-code contract first, since `Skipped` and `NotRun` land differently there.
