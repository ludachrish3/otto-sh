# `nox -s tests_integration-*` fails five tests without a built frontend

Found on 2026-08-22 while verifying the telnetlib3 4.0.5 → 5.0.0 bump across
interpreters. Not a defect in any of those tests, and not interpreter-specific —
a missing prerequisite on the nox session.

## What happens

On a checkout with no built web frontend, `nox -s tests_integration-3.14`
reports **5 failed, 7329 passed**. All five fail on one cause:

```
RuntimeError: React dashboard build not found at
/home/vagrant/otto-sh/src/otto/_webassets/monitor/dist/index.html
 — run `make web` to build the web/ frontend before starting the monitor server.
```

| test | needs |
| --- | --- |
| `tests/integration/chaos/test_signal_monitor.py::test_sigterm_during_monitor_serve_exits_143` | the monitor server to start |
| `…::test_forced_sigterm_during_monitor_serve_still_exits_143` | same |
| `tests/integration/cov/test_coverage_pipeline.py::TestCoverageReport::test_multi_run_stitching` | the report SPA |
| `…::test_merged_coverage_across_hosts` | same |
| `tests/integration/cov/test_capture_report_cycle.py::test_manual_survives_unrelated_commit_and_stales_on_edit` | same |

**Attribution proven, not assumed:** after `make web`, those exact five run
green (`5 passed in 16.70s`) on the same interpreter, same tree.

## Why it is invisible most of the time

`make coverage` has `dashboard` as a prerequisite of `coverage-python`, which
builds the dist — so the house per-task gate self-heals it and nobody sees this.
`nox -s tests_integration-*` has no such prerequisite. The nox venv installs
otto from a build that carries no frontend (the known "bare `uv build` ships
frontend-less wheels" behaviour), so each interpreter's venv hits it
independently.

It is not 3.14-only. 3.11 happened to come back clean on the same tree — the
five simply did not land where the missing artifact mattered that run, which
makes this *intermittently* visible, the worst kind.

## Shape

Give the `tests_integration` session (and any sibling that starts the monitor
server or renders the coverage report) the same frontend prerequisite
`coverage-python` has, or an explicit fail-fast check at session start that says
"run `make web`" **once**, rather than five times as five unrelated test
failures.

The runtime error message is already good. The problem is that it arrives
attached to five tests that have nothing to do with whatever the person was
changing — the reader's first hypothesis is their own diff.

## Why bother

Anyone reaching for a non-default interpreter is usually chasing something
subtle; five red herrings at that moment are expensive. It cost real time today
on a run whose entire purpose was deciding whether a dependency bump was safe —
the five failures had to be run down and cleared before the actual answer
(clean) could be trusted.

## Related

- `noxfile.py` — `tests_integration`, and the `dashboard` session's docstring on
  why its venv is Python-only.
- `Makefile` — `coverage-python: dashboard`, the prerequisite that hides this.
- `todo/pytest-stale-web-dist-guard.md` — the **same family, different
  entrypoint**. That one (raised 2026-07-30) covers bare `pytest` certifying a
  stale or missing dist, and explicitly records that the `make` dependency chain
  is complete and must not be "fixed". This entry is the third entrypoint: the
  nox sessions, which are neither `make` nor bare `pytest` and which the Make
  chain therefore does not reach. Worth solving together.
