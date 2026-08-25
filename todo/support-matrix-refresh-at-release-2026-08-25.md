# `make release` publishes the support matrix but never re-measures it

Raised 2026-08-25: "does `make release` refresh the conformance matrix? That
seems like a natural spot." It does not, and the instinct is right that
something should. But the obvious wiring — appending `conformance-bed` to the
release recipe — breaks the release, for reasons worth writing down before
anyone tries it.

## What the release chain does today

`make release` runs eleven targets: `clean-dist`, `web-install`,
`check-python`, `docs`, `nox`, `web`, `dashboard-all`, `validate-ts`,
`profile`, then `wheel-check` and `build`. None of them depends on
`conformance-bed` or `support-matrix`; the collator is hung off
`conformance-bed` alone (`Makefile:852`).

`make docs` *does* list `schemas/support_matrix.json` and
`scripts/render_support_matrix.py` among its inputs (`Makefile:1189`), and the
page is rendered at Sphinx `builder-inited`. So the accurate statement is:

> **every release RENDERS the matrix, and no release REFRESHES it.**

A release can ship a support-matrix page — a page that tells readers what otto
can do on which userland, and what proved it — whose measurements are
arbitrarily old, and nothing in the pipeline notices or says so.

## Why `&& $(MAKE) conformance-bed` in the recipe is the wrong shape

**1. It would block the push the release tells you to make.** `support-matrix`
writes `schemas/support_matrix.json`, a tracked file. `make gate-fresh` — run
by `.githooks/pre-push` on any push that updates `refs/heads/main` — "refuses
if tracked files are modified or staged" (its own help text). The release ends
by printing `git push --follow-tags`. A release that re-measured would leave
that file dirty and the very next push would be refused. The alternative
failure is worse: if anything stages it, it rides into the
`chore(release): bump version` commit unreviewed.

**2. It removes the one property the collator was designed around.**
`support-matrix`'s help says it "never commits — review the diff yourself",
and `collate_support_matrix` is the ONLY writer of a `measured-*` verdict. The
human diff review is deliberate: a `measured-broken` appearing or disappearing
is a claim about the product, not a build artifact. Folding the write into an
automated release deletes the review step.

**3. It couples releases to bed health for the wrong reason.** `conformance-bed`
is dev-VM-only and needs the lab VMs and the Zephyr guests. `make release`
already depends on the bed through `nox`'s `tests_all` legs, so this is not a
new dependency — but it would be a new *reason* to fail a release: a flaky
cell measurement, rather than a defect in the thing being shipped.

## What to do instead: gate on staleness, do not re-measure

The enabler is already in the data. Every cell carries `as_of` (e.g.
`"as_of": "2026-08-25"`) and `venue` (`"bed"`). So the release can cheaply
assert that the matrix is *current* without running a single bed test.

Options, roughly in increasing cost:

- **(a) Staleness assertion in the release.** Fail (or loudly warn) if any
  `measured-*` cell's `as_of` is older than N days, with the remedy in the
  error text: run `make conformance-bed`, review the diff, commit. Cheap,
  hostless, no bed needed, and it keeps the human in the loop. The threshold
  is a judgement call — a release cadence of weeks suggests something like 30
  or 60 days.
- **(b) Shape drift assertion.** Fail if the matrix's cell set no longer
  matches the axis space the current lab data and profiles produce — i.e. a
  host or userland was added and has never been measured. Catches the case
  (a) misses: a brand-new element whose cells are simply absent rather than
  stale. Needs `axis_space()`, so it is a little more work.
- **(c) Re-measure out of band.** A scheduled job (nightly or weekly) that
  runs `conformance-bed`, and opens a PR with the diff rather than committing
  it. Preserves review, keeps the bed off the release path, and makes (a)
  mostly self-healing.

**Recommendation: (a) now, as part of the release's existing static checks;
(c) if the staleness warning starts firing routinely.** (b) is worth doing
whenever someone next touches `axis_space()` — it is the one that catches an
untested new host, which is the failure most likely to embarrass a release.

Note that "untested" is a real, first-class state in the schema and is NOT the
same as unsupported. A staleness gate must not tempt anyone into flipping a
cell to `measured-ok` to silence it; only `collate_support_matrix` may write a
`measured-*` verdict, and only from a bed run.

## Sequencing

Follow-up to item 6 of the test-strategy workstream (spec
`docs/superpowers/specs/2026-08-22-test-strategy-and-unix-lab-rename-design.md`).
Item 5 built the matrix, its schema and its collator; item 6 is the fault
vocabulary. This lands after item 6 squashes.
