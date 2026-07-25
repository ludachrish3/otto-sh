# Coverage validity (Plan A) — follow-ups from the final branch review

Triage source: final whole-branch review of `worktree-coverage-ui-rework`
(2026-07-25), all items verdicted follow-up (nothing blocks merge).

## 1. Binary-transcode hole: silently-valid credits over rewritten bytes

A charset conversion that makes git classify a file as binary (UTF-8→UTF-16,
NUL-introducing) produces `Binary files ... differ` with no hunks. All three
parse sites treat that as "no change", so manual credits stay valid over
completely rewritten bytes — the opposite of spec §8.1's intent. Documented as
a `{warning}` limitation in the guide meanwhile. Pre-existing (the old
per-file chain had the identical hole); not a regression of this branch.

Fix shape (all three sites, per the final review):

- `treediff.py`: detect `Binary files ... differ` lines → sentinel `FileDiff`
  (rather than dropping the file from the parsed dict).
- `anchor.py` `_build_fallback_index`: honor the sentinel (its
  `fd is None → verbatim` branch would otherwise swallow it).
- The lazy per-file chain: check `diff_no_index_u0` output for the binary
  marker.
- Resolver degrades sentinel files to stale WITH a warning, keeping the
  count>0 gate (never mark never-executed lines stale).
- Then drop/soften the guide's `{warning}` and pin with a UTF-16 timeline case.

## 2. Spec §10 timeline-case gaps (deferral annotated in spec §10)

- Encoding-flip pin (BOM addition → affected lines revoke; pins amended §8.1).
- Rebase; nested repo/submodule; `max_age` tightened post-capture; moved
  `LCOV_EXCL` markers.
- **Aging-recovered semantic needs a decision before pinning**: today
  `validity.py` clears only `"stale"` on fresh coverage and sets `"aging"`
  whenever state is None — a line freshly re-covered by a new run can still
  carry/acquire the aging flag regardless of fold order. Decide intended
  behavior, then pin.
- `test_overlap_one_valid_one_stale_no_double_count` never exercises the
  stale half of its name (old capture credits only the unchanged line);
  extend so the old capture credits the edited line pre-edit and assert
  per-run revocation traceability — or rename.
- Batched-fallback hardening: a subdirectory relpath through the index; a
  dispositions spot-check in `test_fold_gcd_base_is_batched` (spawn count
  alone would pass a fast-but-wrong path); optional dup-base-blob index case.

## 3. Small code polish (fine-as-is, do when touching the files)

- `AnchorResult.verifiable` has no production consumer (`validity.py` gates on
  `new_relpath is None`); either gate on it or drop the field.
- `_build_fallback_index`: pass `list(present)` to `cat_blobs` instead of
  recomputing an equal set.
- `blobs_exist` docstring: note the full-40-char-sha assumption.
- `treediff.py` `have_newline` → `have_new_side` (it means "saw a `+++` line").
- `supersede._key`: two label-less captures (`display_name=None`, `board=""`)
  on one tier would collapse into one context; unreachable via real capture
  flows today — guard or comment.
- Guide shallow-clone bullet says "the report naming the fix" — the deepen
  hint is a fold-time log warning, not in the rendered report.
- `test_fold_gcd_base_is_batched` → `gced_base` (avoid greatest-common-divisor
  misread).
