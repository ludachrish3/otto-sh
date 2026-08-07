# `no such table: context` + xdist INTERNALERROR — what's settled, what isn't (2026-08-07)

Split out of the issue #196 fix (`--collect-only` must not arm the browser
build gate) so that squash stays one thing. Recorded because the symptom is
recurring and the first diagnosis was wrong.

## What actually happened this time (settled — NOT a cold-start race)

While gating the #196 fix in a fresh worktree, `make coverage-hostless` blew up
twice with ~242 and ~300 errors of

    coverage.exceptions.DataError: Couldn't use data file
      '.../reports/coverage/.coverage.otto.<pid>.<rand>': no such table: context

plus `INTERNALERROR ... xdist/dsession.py:218 KeyError: <WorkerController gw3>`.

First hypothesis was a cold-start coverage-schema pre-init race in a
never-before-built worktree. **That was wrong**, and the run order disproves
it: run 1 (fix, cold) errored, run 2 (pristine stash, warm) was clean, run 3
(fix, warm) errored. The correlation is with the *change*, not with cold start.

Root cause: the new `tests/unit/test_browser_guard.py` built a real
`pytest.Config` via `_pytest.config.get_config(argv); config.parse(argv)`.
Parsing argv **with this repo as rootdir makes pytest apply this project's
`addopts`**, which are

    -p no:tach --doctest-modules --cov=otto --cov-context=test
    --cov-report term-missing --cov-report html -n auto --dist loadgroup

So each such call started a *second* coverage session (with dynamic contexts)
inside the xdist worker already running that test, corrupting the worker's own
data file and taking the controller down with it. Verified directly:

    get_config([]); config.parse([])   # in the repo root
    -> config.option.cov_source == ['otto'], cov_context == 'test',
       numprocesses == 'auto', inipath == <repo>/pyproject.toml

Fixed in that test by parsing against a throwaway inert `[pytest]` ini via
`-c`, with an assertion that `cov_source`/`numprocesses` came back empty so the
isolation cannot silently lapse. **No product or harness defect was involved.**

## The durable hazard (worth a guard, not yet built)

*Any* in-process construction of a pytest `Config`/`Session` from this repo's
root silently inherits `--cov`/`-n auto` and can hijack its worker. This is a
sharp edge with an invisible failure mode: it does not fail where it is
written, it corrupts an unrelated worker and reports as hundreds of
unattributable errors in other files.

Known in-process inner-session sites to audit against this:

- `otto.suite.run.run_suite` → `pytest.main([suite_file, ...])`, driven by
  several `tests/unit/suite/` tests. *Probably* safe because the inner run's
  rootdir resolves to the `tmp_path` suite file rather than this repo — that
  is an assumption, not a verified fact, and it is exactly the kind of
  assumption that holds until someone runs a suite fixture from the repo tree.
- `tests/unit/suite/test_options_plugin.py` → `pytester.runpytest_inprocess`.
- `tests/unit/suite/conftest.py` documents the in-process pattern but isolates
  only the `SUITES` registry, not pytest config/coverage state.

Candidate guard: assert in a session-scoped autouse fixture (or a small unit
pin) that no test leaves a second `coverage.Coverage` instance started, or
that any in-process `Config` built under test carries no `cov_source`. Cheap
version: an ast-grep rule banning bare `get_config(`/`Config.fromdictargs(`
in `tests/` without an accompanying `-c`.

## Genuinely still open

- Whether a *real* cold-start / pre-init race also exists, independent of the
  above trigger. The symptom has been seen before under a different driver —
  see `todo/`-adjacent notes and the `make release` occurrence recorded as
  "nox `no such table: context`, fix = pre-init cov schema single-threaded".
  This session produced no evidence either way, because the one plausible
  cold-start data point (run 1) is fully explained by the addopts hijack.
- `tests/unit/test_coverage_schema_preinit.py::test_worker_data_file_preinited_under_real_coverage`
  failed during run 1 (`.coverage.otto.<pid>.<rand>` did not exist). That is
  consistent with being *downstream* of the corruption rather than an
  independent defect, but it was not re-checked in isolation. Re-run that test
  alone in a fresh worktree before concluding anything about it.

## How to investigate cleanly

The fresh-worktree first-coverage-run case is the one to reproduce, with the
#196 branch's test file **removed** so the known trigger is absent:

    git worktree add /tmp/cov-probe main && cd /tmp/cov-probe && uv sync
    make coverage-hostless          # first ever coverage run here

Clean => no cold-start race; close this section. Errors => a real one exists,
and the run above is a minimal reproducer.
