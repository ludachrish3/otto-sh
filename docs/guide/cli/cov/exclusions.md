# Exclusion markers

lcov's `geninfo` honors the standard exclusion markers natively —
excluded lines never enter the parsed data, so they never enter a
denominator:

- `LCOV_EXCL_LINE` — exclude one line.
- `LCOV_EXCL_START` / `LCOV_EXCL_STOP` — exclude a block.
- `LCOV_EXCL_BR_LINE`, `LCOV_EXCL_BR_START` / `LCOV_EXCL_BR_STOP` —
  branch-only variants (line/block still counted, only its branches
  excluded).

The renderer additionally re-scans each rendered source file for
these markers so excluded lines and blocks are visually distinct
(grey, with a per-file excluded count) instead of reading as ordinary
uncovered code.  In the row-coloring precedence (see
{ref}`coverage-colors`), excluded **always wins**, even over a covered,
stale, or aging line.

Extend the recognized marker set with custom strings via
`[coverage.exclusions] markers`:

```toml
[coverage.exclusions]
markers = ["MYPROJ_NO_COV"]
```

Custom markers are **render-only today**: a line marked
`// MYPROJ_NO_COV` is scanned by the renderer alongside the built-in
`LCOV_EXCL_*` set, so it renders grey and excluded like any other
excluded line — but unlike the built-in markers (which `lcov`'s
`geninfo` strips from the parsed data before it ever reaches otto),
a custom marker is *not* passed to the `lcov` capture as an `rc`
override. The line still counts toward the coverage percentages;
only its visual presentation changes.
