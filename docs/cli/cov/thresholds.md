(coverage-report-thresholds)=
# Report thresholds

`otto cov report` colors every coverage percentage cell it renders —
the project summary, each tier's column, the sortable file table, and
every per-file page — against gcovr-style cutoffs: a cell at or above
`high` renders **green**, at or above `medium` renders **yellow**, and
below `medium` renders **red**. Configure the cutoffs under
`[coverage.report]`:

```toml
[coverage.report]
high = 80
medium = 70
```

| Field | Meaning | Default |
|-------|---------|---------|
| `high` | Percentage at or above which a cell renders green | `80` |
| `medium` | Percentage at or above which a cell renders yellow; below it renders red | `70` |

Both values must fall within `0`-`100`, and `medium` must not exceed
`high` — an inverted or out-of-range `[coverage.report]` block is a
settings error, rejected at parse time rather than at report time.
These are the only two keys; a repo with no `[coverage.report]`
section gets the defaults shown above. This is distinct from the
per-tier {ref}`legend colors <coverage-colors>`, which color source
lines and table columns by which tier covered them, not by
percentage.
