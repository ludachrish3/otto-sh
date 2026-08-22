# Selection runs

## Running without a suite name

`otto test` doesn't require a suite subcommand. Passing `--tests` and/or
`-m`/`--markers` alone selects tests by exact name and/or marker expression
across every suite and repo that has a match, including plain pytest
`test_*` functions (not just `OttoSuite` classes). Bare `otto test` with no
suite name and neither flag just prints help.

```bash
otto test --tests test_login                    # every test named test_login, any suite
otto test --tests TestB::test_login,test_plain   # disambiguate + mix suite/plain names
otto test -m "not integration"                   # marker expression, no suite name
otto test --tests test_login -m slow             # narrow a name selection by marker too
```

- `--tests NAME[,NAME...]` matches on exact function name: a bare name (e.g.
  `test_login`) matches that name in every suite/repo, across all
  parametrizations; `TestClass::test_name` restricts to one suite. Unknown
  names raise an error with did-you-mean suggestions rather than silently
  running nothing.
- `-m EXPRESSION` alone (no `--tests`, no suite name) runs the marker
  selection the same way — one pytest session per repo that has a match.

### Tab-completing `--tests`

`--tests` tab-completes test names, matched by **base name** — a bare
`test_login` selects every `test_login[...]` parametrization, and
`TestClass::test_login` disambiguates:

```{raw} html
:file: ../../../_static/generated/termynal/complete-test-names.html
```

Candidates come from a static source scan plus, once warmed, real pytest
collection — so dynamically generated tests are included too, and the first
slower TAB is a one-time cost. See
{doc}`../../../architecture/subsystems/execution` for the two-layer mechanism
behind it. For the exact, fully-expanded per-parametrization list, `otto test
--list-tests` still prints every collected id.
- Multi-repo selection runs write one JUnit file per repo
  (`junit_<repo>.xml`) instead of the single-suite `junit.xml`. An explicit
  `--results PATH` fans out the same way: `PATH`'s stem gets `_<repo>`
  appended for each participating repo (e.g. `--results custom.xml` becomes
  `custom_repoA.xml`, `custom_repoB.xml`, ...), so multiple repos' sessions
  never overwrite each other's results.
- Stability (`--iterations`/`-i`, `--duration`/`-d`, `--threshold`),
  `--cov*`, `--monitor*`, and `--results` all apply to selection runs the
  same as to a named suite.

### Suite-specific options and selection runs

Suite-specific options (declared on a suite's `Options` class) only exist as
CLI flags on that suite's own subcommand — `otto test TestDevice --flag`.
Selection runs (`--tests`/`-m` with no suite name) span multiple suites at
once, so there's no single flag set to parse; each suite's `Options` class is
instead **default-constructed** once per suite. If a suite's `Options` has a
required field (no default), its tests fail during the selection run with a
hint to re-run that suite directly:

```text
suite 'TestDevice' has required options — run `otto test TestDevice ...` to pass them (...)
```

Suites whose options are all optional (have defaults) run fine under
selection — they just get their defaults instead of CLI-provided values.

## Tab-completing `--tests`

`--tests` tab-completes test names, matched by **base name** — a bare
`test_login` selects every `test_login[...]` parametrization, and
`TestClass::test_login` disambiguates:

```{raw} html
:file: ../../../_static/generated/termynal/complete-test-names.html
```

Candidates come from a static source scan plus, once warmed, real pytest
collection — so dynamically generated tests are included too, and the first
slower TAB is a one-time cost. See
{doc}`../../../architecture/subsystems/execution` for the two-layer mechanism
behind it. For the exact, fully-expanded per-parametrization list, `otto test
--list-tests` still prints every collected id.
- Multi-repo selection runs write one JUnit file per repo
  (`junit_<repo>.xml`) instead of the single-suite `junit.xml`. An explicit
  `--results PATH` fans out the same way: `PATH`'s stem gets `_<repo>`
  appended for each participating repo (e.g. `--results custom.xml` becomes
  `custom_repoA.xml`, `custom_repoB.xml`, ...), so multiple repos' sessions
  never overwrite each other's results.
- Stability (`--iterations`/`-i`, `--duration`/`-d`, `--threshold`),
  `--cov*`, `--monitor*`, and `--results` all apply to selection runs the
  same as to a named suite.

## Suite-specific options and selection runs

Suite-specific options (declared on a suite's `Options` class) only exist as
CLI flags on that suite's own subcommand — `otto test TestDevice --flag`.
Selection runs (`--tests`/`-m` with no suite name) span multiple suites at
once, so there's no single flag set to parse; each suite's `Options` class is
instead **default-constructed** once per suite. If a suite's `Options` has a
required field (no default), its tests fail during the selection run with a
hint to re-run that suite directly:

```text
suite 'TestDevice' has required options — run `otto test TestDevice ...` to pass them (...)
```

Suites whose options are all optional (have defaults) run fine under
selection — they just get their defaults instead of CLI-provided values.

