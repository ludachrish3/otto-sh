# A dry-run follow-up the contract work left open

Found while making `host`, `link` and `tunnel` honest under `--dry-run` (that
work is on main). Not folded in, because it is presentation rather than a
correctness question.

(§1 was `BaseHost.reboot` having no dry-run guard, with `hard=True`
power-cycling a real machine. RESOLVED — `reboot` and `UnixHost.shutdown` now
announce and return `Status.NotRun` above every action, and the guards live in
`tests/unit/host/test_dry_run.py`.)

The correctness follow-ups from the same workstream are separate, because they
are a defect family rather than presentation:
[dry-run-failure-message-relay-family-2026-08-16.md](dry-run-failure-message-relay-family-2026-08-16.md)
— six library verbs that build a failure message out of a declined result, plus
`Userland.probe`, the last `is_ok=True` dry-run arm in the tree.

## A seam-side "extra lines" hook would let the power verbs keep their richness

`reboot`, `shutdown` and `power` deliberately keep the `--dry-run` seam stop
(they are the verbs that touch power, so the library arm is a second guard, not
the only one). Two things are lost at the CLI by that choice and both are
reclaimable without ever running a body: the deep announcement (`reboot`'s
controller name and resolved wait bounds) and the local feasibility check (a
controller-less `--hard` exits 0 printing "would run" instead of raising).
Shape: a per-leaf hook in the `__otto_dry_run_refs__` pattern that returns extra
lines for `print_dry_run_block` and may raise — no body code runs. Not built,
and not worth building until a second caller wants it.

## `otto link repair --all -n` repeats its caveats once per link

The dry-run sweep prints the same "not checked:" paragraphs verbatim for every
link — roughly 15+ terminal lines each at 80 columns, so a lab with a handful of
links buries the per-link plan under identical prose. A single impair or repair
is fine and should not change; this is specific to the sweep.

Shape: hoist the shared caveats to one block for the whole sweep, keep the
per-link `would:` lines. Worth doing when someone is next in
`src/otto/cli/link.py`; not worth a dedicated change.
