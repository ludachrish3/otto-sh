# `bootstrap._discovery_errors` accumulates for the life of the process

Found while root-causing the xdist exit-1 flake fixed in `cc6bc5dc` (issue
#180 branch). That commit fixed the *test* symptom with an autouse
`bootstrap._reset()` in the root conftest. The underlying asymmetry in the
production module is deliberately left here rather than folded into a
test-isolation fix, because changing it is a behaviour change on a
documented public entry point.

## The asymmetry

`src/otto/bootstrap.py` keeps four module globals. Three are caches that
`discover()` / `bootstrap()` populate and read (`_discovered`, `_result`,
`_completion_names`). The fourth is not a cache — it is an accumulator:

```python
_discovery_errors: list[BootstrapError] = []          # bootstrap.py:68

# inside discover(), only when _discovered is None:
_discovery_errors.append(BootstrapError(sut_dir, ...))  # bootstrap.py:94

# inside bootstrap():
errors: list[BootstrapError] = list(_discovery_errors)  # bootstrap.py:105
```

`discover()` appends but never clears, and `bootstrap()` folds the whole list
into *every* result it ever builds. The only thing that empties it is
`_reset()` (`bootstrap.py:137`), whose docstring calls it a test hook.

So a single failed repo discovery is permanent for the process: re-running
discovery cannot clear the error, and there is no supported public way to
drop it. Errors are also duplicated rather than replaced if `_discovered` is
cleared without `_discovery_errors` being cleared alongside it — the two
globals have no invariant tying them together, they just happen to be reset
together in the one function that touches both.

## Why it matters beyond the tests

For a one-shot CLI process this is invisible: discovery runs once and the
process exits. It bites anything long-lived:

* **Library embedders.** `otto.bootstrap.bootstrap()` is public and
  documented as the entry point (`docs/library/index.md:16,19,33,203`). A
  host process that fixes a repo's `settings.toml` and calls `bootstrap()`
  again still gets the stale error, and — because `fail_loud_on_bootstrap_errors()`
  (`src/otto/cli/invoke.py:405`) exits 1 whenever `bootstrap().errors` is
  non-empty — anything routed through that gate stays wedged.
* **Any future daemon/server mode**, for the same reason.

It is also what made the test flake so hard to read: the error outlived the
`monkeypatch.setenv` that caused it, so the failure surfaced arbitrarily far
from its origin, as a bare `SystemExit(1)` with empty stderr.

## Options

1. **Scope the errors to the discovery they came from** (preferred). Return
   them as part of `discover()`'s result — e.g. make `_discovered` hold
   `(env, repos, errors)` — so recomputing discovery necessarily recomputes
   its errors and the two cannot drift apart. `bootstrap()` reads them off
   that tuple instead of a parallel global. Removes the invariant-by-
   convention entirely.
2. **Clear `_discovery_errors` at the top of the `if _discovered is None:`
   branch.** One line, keeps the shape, but leaves two globals that must be
   kept in sync by hand — the thing that broke here.
3. **Add a public `reset()`/`invalidate()`** so embedders can recover.
   Complements either of the above rather than replacing them; today the only
   escape hatch is the underscore-prefixed test hook.

Option 1 plus a documented public invalidation is probably the right pair.

## Guard to keep

`tests/unit/test_env_hermeticity.py::test_bootstrap_state_cannot_leak_between_tests`
pins the test-level symptom. A fix here should add a direct unit test: record
a discovery error, make the underlying repo valid, re-run discovery, and
assert the error is gone from `bootstrap().errors` — which fails today.
