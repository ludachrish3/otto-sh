# `make conformance-bed` rewrites the tracked `schemas/support_matrix.json`

Observed 2026-10-16 on the dev VM: a `make conformance-bed` run rewrote the
tracked `schemas/support_matrix.json` — only its `as_of` dates changed, 81
lines of diff — and a `make gate-fresh` later in the same run refused with
"tracked files are modified or staged". The workaround was
`git checkout -- schemas/support_matrix.json` and re-running gate-fresh alone.

A gate lane must leave the tracked tree as it found it: a lane that dirties a
tracked file breaks every later lane that insists on a clean tree, and a
contributor who gates before committing stages the churn by accident.

- [ ] Find where the bed writes the matrix (`as_of` dates) and make the bed
      run read it only; the rewrite belongs to an explicit regenerate target
      (`make support-matrix` or the like), never to a gate lane.
- [ ] If the dates are meant to move with every run, keep them out of the
      tracked file (a sidecar under the build dir) so the tracked content is
      stable across runs.
- [ ] Pin it: a test or the lane census asserts that `make conformance-bed`
      leaves `git status --porcelain` empty for `schemas/`.
