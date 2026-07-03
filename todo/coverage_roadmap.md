# Coverage Module Roadmap

Items deferred from the initial coverage integration.  These are not
planned for the current release but are tracked here for future work.

## Updates since this file was first created

The highest priority features are:

- support for gcc and clang
- coverage "tiers" were based on info files. I don't know if this is the longterm approach we want to take. There can be gcda files produced from the real lab on remote hosts, unit tests generated on the build host, and then multiple configurable tiers for different kinds of manual testing. I'm open to other solutions, but to make this coverage feature useful for ALL types of users, tracking 100% code coverage is very important. The common tiers are on-host (sometimes embedded systems or stripped down unix hosts) real e2e testing, unit testing done on the build host (a more fully featured machine with modern tooling), and manual testing done either by changing code and recompiling, or using GDB, to capture proof of functionality and coverage in scenarios
- the main wrinkle is tracking coverage from manual testing means. it's safe to assume that all code within a sut repo has a ticket of some kind assigned to it. this is why git blame annotation is considered as a potential solution to tracking manual coverage and mark that in coverage reports
- per-ticket coverage is still desired because program management needs to know what state testing is and direct resources to beef up testing.
- another ambitious goal is to have the HTML coverage report have linkage from boolean clauses and branches that are taken. The previous view was to combine gcno and dwarf info to have enough info to do this. this is still a set of assumptions that you can rely on to make this a reality.
- using the same frontend tech stack as the new monitoring rework is very welcome for its efficiency, testability, and aesthetics.

## Usability Improvements

### Coverage Threshold Enforcement (`--cov-fail-under`)

Add a `--cov-fail-under PERCENT` CLI option that fails the test run
(non-zero exit code) if overall coverage drops below the threshold.
Should integrate with otto's existing exit code conventions.

**Configuration**: `coverage.options.fail_under` in `.otto/settings.toml`.

### Console Coverage Summary

Print a Rich-formatted coverage summary table to the console after
the HTML report is generated, showing per-file and overall percentages.
Currently coverage results are only in the HTML report and a log line.

## Feature Deferrals

### Git Blame Annotation

Currently implemented but can be expensive on large repos (one
`git blame --porcelain` subprocess per source file).  Consider:

- Making it opt-in via `coverage.options.annotate_blame = false`
  (currently defaults to `true`)
- Batching blame calls or using `libgit2` for in-process blame
- Caching blame results across runs (since blame data changes
  infrequently)

### Per-Ticket Coverage Breakdown

The original proposal mentions "a per-ticket breakdown of which lines
are uncovered" as highly desired.  This requires:

- Integration with a ticketing system (Jira, Linear, etc.)
- Mapping uncovered lines to tickets via git blame → commit → ticket
- A new HTML view or API endpoint for per-ticket reports

This is a significant feature that depends on ticketing system
integration and should be designed separately.

### Manual Coverage Integration

Manual test coverage (`MANUAL` tier) is produced entirely outside otto:
testers run the product with `GCOV_PREFIX` set, collect `.gcda` files,
and produce a `.info` file with `lcov --capture`.

Future improvements:
- An `otto coverage capture` CLI command to help testers produce
  `.info` files from manual sessions
- Automatic accumulation of manual `.info` files across sessions
- Tester identification (who ran which manual test)

### PathMapping Auto-Discovery Improvements

The current auto-discovery parses `SF:` lines from `.info` files or
`strings` output from `.gcno` files.  Improvements:

- Interactive mode: show discovered prefix and ask user to confirm
- Support for multiple build roots (e.g., main source + vendored deps)
- Cache discovered mappings in `.otto/coverage_mappings.toml`
