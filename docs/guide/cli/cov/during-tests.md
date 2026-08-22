# Coverage during a test run

```bash
otto test --cov TestMyDevice
```

This runs the test suite normally, fetches `.gcda` files from every
matched host, and — on a best-effort basis — produces a `capture.json`
per board, anchored to `base_commit`, against the lab's default `e2e`-kind tier
using the same capture-production machinery as `otto cov get`.  This
tail never fails an otherwise-successful test run: a non-git SUT,
misconfigured tiers, or a stamp mismatch during merge are logged and
swallowed, leaving the raw `.gcda` artifacts on disk for manual
recovery via `otto cov get`.  The files land in a `cov/` directory in
the suite's output directory, organized by board:

```text
<log_dir>/
  cov/
    <board_id_1>/
      capture.json
      *.gcda
    <board_id_2>/
      capture.json
      *.gcda
```

```{note}
Both `otto cov get` and this `otto test --cov` tail wrap one async library
function — `collect_coverage()` — paired with `run_coverage_report()` for the
HTML report. To drive collection and reporting from your own Python (CI glue or
a custom pipeline), see the *Collecting coverage from Python* section of
{doc}`../../../library/index`.
```

## Options

| Option | Description |
| ------ | ----------- |
| `--cov` | Fetch `.gcda` files from remote hosts after the run into `<output>/cov/` |
| `--cov-dir PATH` | Write coverage artifacts to an explicit directory (implies `--cov`) |
| `--overwrite-cov-dir` | Allow `--cov-dir` to clear an existing non-empty directory |
| `--cov-clean / --no-cov-clean` | Delete stale `.gcda` on remotes before the run (on by default; `.gcda` counters are additive) |
| `--cov-report, -r` | Also render the HTML report inline after the run (implies `--cov`) |
| `--cov-report-dir PATH` | Explicit destination for the inline HTML report (implies `--cov-report`) |

See {doc}`../test/index` for the rest of `otto test`'s options, and
{doc}`index` for the collection workflow these flags plug into.

## Choosing a Destination

Use `--cov-dir` to write coverage artifacts to an explicit location —
for example, a persistent CI directory:

```bash
otto test --cov-dir /var/artifacts/myrun TestMyDevice
```

`--cov-dir` implies `--cov`, so the `--cov` flag is optional when you
supply a path.  The destination directory is created if it does not
already exist.  If it exists and is non-empty, the run aborts to avoid
mixing stale coverage into the new results; pass `--overwrite-cov-dir`
to clear it first:

```bash
otto test --cov-dir /var/artifacts/myrun --overwrite-cov-dir TestMyDevice
```

Omitting both `--cov` and `--cov-dir` disables coverage collection.

## Inline Reports

`--cov-report` renders the HTML report immediately after the run,
without a separate `otto cov report` invocation.  It goes through the
same collection model: the configured tiers (colors, precedence,
custom exclusion markers), the unit-tier harvest, and the committed
manual store all apply, exactly as they would in a standalone report.
Like the capture tail, inline report generation is best-effort — a
report-side problem is logged and never fails an otherwise-successful
test run.

## Pre-Run Cleanup

By default, `--cov` deletes stale `.gcda` files on remote hosts
**before** the test run.  This is important because `.gcda` counters are
**additive** — without cleanup, coverage data from previous runs
contaminates the current results.

To skip pre-run cleanup and accumulate coverage across runs:

```bash
otto test --cov --no-cov-clean TestMyDevice
```
