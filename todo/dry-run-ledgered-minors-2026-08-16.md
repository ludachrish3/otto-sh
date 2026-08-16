# Dry-run contract — the five minors the final review triaged to "ledger"

The `--dry-run` contract workstream shipped as `03407261`. Its whole-branch
review triaged sixteen deferred minors: two were fixed before merge (docker
`_auto_up` re-raising a decline as a bare `RuntimeError`; `verify_connection`
logging `[DRY RUN]` on the live path), nine were dropped, and these five were
ruled "keep, but not on that branch". They lived only in the SDD ledger, which
is git-ignored scratch — this file is where they survive.

None of them is a correctness bug today. Each is a place where the contract is
one ordinary edit away from being weaker than it reads.

The correctness follow-ups from the same workstream are elsewhere:
[dry-run-failure-message-relay-family-2026-08-16.md](dry-run-failure-message-relay-family-2026-08-16.md)
and [dry-run-followups-2026-08-15.md](dry-run-followups-2026-08-15.md).

## 1. `CommandResult._identity()` hand-enumerates its own fields

[src/otto/result.py:157](../src/otto/result.py#L157) — `_identity()` (which
backs both `__eq__` and `__hash__`) and `__repr__` each list the field names
literally. Add a field to `CommandResult` and you get two silent regressions at
once: two results that differ only in the new field compare **equal**, and the
repr omits it. Declines are the worst case, because they are the values most
likely to be compared in a test and least likely to be inspected by eye.

Drift-proof fix: derive the tuple from `dataclasses.fields(self)`, minus
`value` (it can raise — that is the whole point of `NotRunResult`) plus
`host_name`. Verify the derived order matches today's before swapping, and pin
it with a test that adds a field to a throwaway subclass.

## 2. Three docker tests are named for a contract they no longer assert

[tests/unit/host/test_docker_host.py:530](../tests/unit/host/test_docker_host.py#L530),
`:566`, `:581` — `test_run_dry_run_skips_session`,
`test_put_dry_run_skips_transfer`, `test_get_dry_run_skips_transfer`. All three
now assert `Status.NotRun`, which is the opposite of the `Skipped`-as-ok
behaviour their names describe. A name that describes the old contract is how
someone "restores" it during a later cleanup.

Rename to say what they check (`..._declines_...`). Cheap; the only reason it
was deferred is that renames are noise on a 56-file branch.

## 3. An ok dict aggregate holding one decline renders a stray row

[src/otto/cli/invoke.py:977](../src/otto/cli/invoke.py#L977) —
`render_leaf_value`'s ok-branch dict path prints the decline's error message
and then a `src -> None` row for the same entry. The user sees the refusal
twice, once as prose and once as a fabricated-looking `None`. The `None` is the
part that matters: it is exactly the shape the contract exists to keep off the
screen.

## 4. `_param_words` under-reports negated boolean flags

[src/otto/cli/invoke.py:765](../src/otto/cli/invoke.py#L765) — a flag typed as
`--no-x` against a default of `True` is dropped from the would-run echo. The
announcement is otto's promise about what it *would* have done, so an option
the user typed going missing from it is a small honesty hole in the one line
the whole feature exists to print.

## 5. Nothing pairs the in-body arm with the seam stop for a fourth power verb

`reboot`, `shutdown` and `power` each deliberately carry **both** an
`is_dry_run()` arm in the body and the CLI seam stop — two guards, because the
seam reads typed root options while the arm reads the active context, so a
context-plumbing regression breaks one and not the other. That pairing is a
convention held in three heads and one design doc. A fourth power verb added
with only one half would look completely normal in review.

The implementer flagged this as a concern rather than over-building a
registry-driven check for a set of size three, which was the right call at the
time. Revisit when a fourth verb appears — or sooner, if a cheap pin exists
(e.g. a test that asserts every seam-stopping power verb also declines when
invoked directly through the library with a dry-run context).
