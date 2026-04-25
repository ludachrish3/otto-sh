# Improve `.gcno` mismatch error handling in `otto cov`

## The error

When running `otto cov report <log_dir> --report <out>`, lcov/geninfo can fail with:

```
geninfo: ERROR: "/path/to/file.gcno": reports 'X' functions, but this version of gcov
         reports 'Y' - mismatch with notes file
geninfo: ERROR: GCOV failed
```

`otto cov` currently bubbles this up as a raw nonzero exit from the lcov
subprocess with no interpretation, so the user is left staring at a lcov
stack trace with no idea what they did wrong.

## Likely cause

A `.gcno` file and a `.gcda` file are only compatible if they came from the
**same compilation**. The mismatch means the SUT binary was recompiled (or
partially recompiled) after the `.gcda` files were produced — i.e. the
`.gcno` files on disk now describe a different version of the code than
what actually ran.

Common ways to land here:

1. **Concurrent test runs racing on the same source tree.** Two `otto test
   --cov` runs compiling `tests/repo1/product/` (or any shared SUT) at the
   same time will clobber each other's `.gcno` files, so whichever `.gcda`
   set you then try to report against may be stale. This is exactly how
   the e2e coverage tests were failing under xdist until
   `@pytest.mark.xdist_group("coverage_e2e")` + `--dist loadgroup` pinned
   them to one worker.
2. **Manual rebuild between `otto test --cov` and `otto cov`.** Editing a
   source file and running `make` (or letting an IDE rebuild) after the
   test run but before the coverage report.
3. **Mixing `.gcda` files from a prior run with freshly-rebuilt
   `.gcno` files.** e.g. stale `.gcda` files left on a host from an
   earlier build.
4. **Toolchain/gcov version mismatch** — a different `gcov` binary than
   the one that produced the `.gcno` files. Less common in our setup but
   worth mentioning.

## What I'd like `otto cov` to do instead

1. **Detect the mismatch error** by scanning lcov's stderr for the
   `mismatch with notes file` / `GCOV failed` signatures (and/or a
   nonzero exit with that keyword in stderr).
2. **Surface a plain-language explanation** on stderr along the lines of:

   > `.gcno` / `.gcda` mismatch detected for `<file>`. This usually means
   > the source was recompiled after the `.gcda` files were produced, so
   > the coverage notes no longer match the data. Common causes:
   >   - Concurrent `otto test --cov` runs on the same source tree.
   >   - A rebuild happened between `otto test --cov` and `otto cov`.
   >   - Stale `.gcda` files from an earlier build are present.
   > Re-run `otto test --cov` to regenerate matching notes and data.

3. **Exit nonzero** (preserve the failure), but with a clear error code /
   exception type so the CLI prints the friendly message above rather than
   the raw lcov spew.
4. Optional: list which `.gcno` path(s) were implicated so the user can
   inspect mtimes. Bonus points for stat-ing the `.gcno` vs `.gcda` and
   reporting the time delta — if `.gcno` is newer than `.gcda`, that's
   the smoking gun.

## Where the change probably lives

- [src/otto/coverage/correlator/merger.py](../src/otto/coverage/correlator/merger.py)
  — `LcovMerger` runs `lcov --capture` / `geninfo`. This is where stderr
  should be inspected and the friendly error raised.
- A new exception type (e.g. `GcnoMismatchError`) raised from the merger
  and caught in [src/otto/cli/cov.py](../src/otto/cli/cov.py) to print the
  friendly message and exit with a distinct code.

## Not in scope

- Auto-recovery / auto-rebuild. Too much magic; the user should know why
  their coverage is invalid and make the choice.
- Protecting against concurrent compiles — that's a test-infrastructure
  concern (already handled via `xdist_group` for our e2e tests).
