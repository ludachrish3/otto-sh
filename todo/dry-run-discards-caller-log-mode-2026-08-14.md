# `--dry-run` echoes commands their caller asked to keep quiet

Found 2026-08-14 while closing the dry-run half of the userland paste-safety
property (`09e4aafc`). Adjacent to that fix, deliberately not folded into it.

## The bug

`BaseHost._dry_run_result` (`src/otto/host/host.py:541`) takes only `cmd`:

```python
def _dry_run_result(self, cmd: str) -> CommandResult:
    self._log_command(f"[DRY RUN] {cmd}")
```

It has **no `log` parameter**, so a caller's `log=LogMode.QUIET` / `LogMode.NEVER`
is discarded and the command is echoed at the default `NORMAL`. A real run
honours the mode; a dry run does not, which is backwards — the dry run is the
mode a user reaches for when they want to see *less* happen, not more.

## Why it matters, with real callers

The callers that pass a quiet mode do it because the command line itself is the
problem, not just noisy:

- `src/otto/host/embedded_host.py:626,630` — `log=LogMode.NEVER`, and the reason
  is stated at `:600`: the **large encoded payload** must never reach the sink.
  Under `--dry-run` the whole encoded body is printed.
- `HostFileOps.write_file` sends with `log=LogMode.QUIET` so large bodies stay
  out of the console. The dry-run line carries the base64 of the file's
  contents — so a dry run of writing a credentials file prints the credentials,
  encoded but trivially reversible.
- `src/otto/host/unix_host.py:786` — `cat /proc/modules`, merely noisy.

So this is a content-exposure bug in the payload cases, not only console spam.

## What was already fixed, and what this is not

`09e4aafc` stopped `Userland`'s probes from reaching `_dry_run_result` at all
(a dry run now reports them as unasked rather than fabricating measurements).
The probes were the loudest instance — eight lines per resolution — so the
symptom is much reduced, but the mechanism is untouched and every other quiet
caller still leaks.

Do **not** fix this by making the probes special again; the fix is that
`_dry_run_result` should accept and honour the caller's mode, the way the real
path does.

## Suggested shape

Thread `log` through `_dry_run_result` from each call site and pass it to
`_log_command`, matching what the non-dry path does with the same argument.
Check `_dry_run_transfer` (just below it) for the same defect before assuming
it is one function's problem.

Guard it with a test that a `LogMode.NEVER` command produces no sink output
under dry-run — and mutate the mode to prove the test can fail, because a test
asserting "nothing was logged" passes trivially against a broken sink.
