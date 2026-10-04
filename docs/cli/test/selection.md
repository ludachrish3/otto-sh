# Choosing tests

`otto test` runs the tests its names and `-m` select, across every
configured repo. This page is how that selection works.

## Names

Each name is looked up in every repo's tests, as pytest collects them:

| Name | Selects |
| --- | --- |
| `test_reboot` | every test named `test_reboot`, in any class, module or repo |
| `TestDevice` | every test in every class named `TestDevice` |
| `TestDevice::test_reboot` | `test_reboot` in every class named `TestDevice` |

```bash
otto test test_login                    # every test named test_login
otto test TestB::test_login test_plain  # one class's test_login, and a plain function
otto test TestDevice -m slow            # TestDevice's tests that are marked slow
otto test -m "not integration"          # every test the marker expression selects
```

- **Parametrized tests match by their base name.** `test_interface_up`
  selects every `test_interface_up[...]` variant.
- **Nested classes** match at any depth. For a `test_x` in
  `TestOuter::TestInner`, the names `test_x`, `TestInner`,
  `TestInner::test_x`, `TestOuter` and `TestOuter::TestInner::test_x` all
  select it; `TestOuter::test_x` doesn't, since it names a `test_x` defined
  directly in `TestOuter`.
- **A test runs once** however many names select it.
- **An unknown name is an error** with did-you-mean suggestions, never a
  silent empty run:

  ```text
  Invalid value for NAMES: no collected test matches: 'test_rebot' (did you mean: test_reboot?)
  ```

File paths and full pytest test IDs are not names.

## Markers

`-m EXPRESSION` (`--markers`) is a pytest marker expression. With names, it
narrows the named tests to those the expression selects. On its own, it
selects every matching test in every repo that has one. Where your own
markers are declared, and the markers otto adds, are in
[Markers](index.md#markers).

## Order

Tests run in a random order by default
([Test order and reproducibility](index.md#test-order-and-reproducibility)).
With `--no-random` they run in pytest's collection order: repo by repo, file
by file, and in source order within each file. The order you write the names
in doesn't change it.

## Nested test directories

Collection starts at each directory in the repo's `tests` setting and
recurses as pytest does: `test_*.py` files at any depth are collected, each
directory's `conftest.py` loads for the tests below it, and `norecursedirs`
is honored. A test in `tests/router/test_basic.py` is selected by name like
any other.

## What a run collects

Each repo's pytest session both finds the names and runs what they select,
and it collects the repo's whole test tree, as `pytest -k` would: pytest,
with your `python_files`, `collect_ignore` and conftests, decides which
tests exist. So a run with names executes exactly the tests pytest
collects for them, including tests that a JSON file, an environment
variable or a flag in another module turns on, and a same-named test in a
file or repo you never ran before.

otto also keeps a cache of what pytest last collected, the **test-names
cache**, for [tab completion](#tab-completing-names). A run never lets it
narrow what pytest collects. It uses it for two things only:

- **Refusing an unknown name before any test runs.** A name the cache
  doesn't place in any repo (a typo, or a test you just wrote) is looked
  for first by one repo's session: one whose test files changed since the
  cache saw them, or else the first. That session runs before any other
  repo's, and it stops before running a test if it doesn't find the name;
  the next repo then looks, and so on. A name found nowhere is the error
  above, which also names any file that failed to collect, and no test runs
  in any repo. The session that stopped prints pytest's session header and
  its "no tests ran" line (and whatever a conftest printed while it
  loaded); its JUnit file is neither written nor put in place of an earlier
  one. A repo whose collection can't finish while it looks (a conftest that
  fails to load, say) ends the run there, before any test, with pytest's
  exit code and the reason logged.
- **Keeping itself current.** Every run writes back what its sessions
  collected, so tab completion offers what the run saw.

A name the cache places somewhere that turns out to be gone (an edit it
couldn't see) is reported as unknown once every repo's session is done:
loud, never a silent run of fewer tests.

## A file that fails to collect

A file that fails to import is logged as an error naming it:

```text
ERROR    Test collection failed for tests/router/test_broken.py in repo 'acme':
         ModuleNotFoundError: No module named 'nonexistent_mod'
```

It doesn't stop the tests you asked for: they still run. As in pytest, a run
that hit a collection error exits 1 even when every test passed, and every
run reports the file until you fix it: a file that doesn't import might hold
one of the names.

## Several repos

Each repo runs its own pytest session, and gets its own JUnit file and
artifact directory ([Where a run's files go](index.md#where-a-runs-files-go)).
A repo in which no test is selected takes no part in the result. With names,
every repo's session runs, since any repo may hold a name, in the configured
order, after the one that looks for a name the cache places nowhere
([above](#what-a-run-collects)). Repeating
(`--iterations`/`-i`, `--duration`/`-d`, `--threshold`), `--cov*`,
`--monitor*` and every registered option apply to every repo's session alike.

## Tab-completing names

Names tab-complete, one per word, matched by **base name**: a bare
`test_login` stands for every `test_login[...]` parametrization, and
`TestClass::test_login` narrows it to one class:

```{raw} html
:file: ../../_static/generated/termynal/complete-test-names.html
```

The candidates come from the test-names cache, so they are the names
pytest collected, inherited and generated tests included. `-m` completes
marker names from the same cache.

- **The first TAB waits once.** With no cache yet for a repo, or one that
  must be collected whole ([below](#what-the-cache-cant-follow)), the TAB waits
  while pytest collects the whole test tree in a background process, capped
  at 15 seconds, and then answers. If that collection fails or times out, the
  TAB offers no names for that repo, and no TAB tries again for a minute;
  `otto test --list-tests` shows why the collection failed.
- **After that, a TAB answers at once**, from the names pytest last
  recorded. It doesn't look at your test files at all, in any shell, so the
  files a TAB reads don't grow with the number of tests you have.
- **A change shows up one check late.** Every ten minutes at most, a TAB
  starts a background check once it has answered: it looks for test files
  that were added, edited or deleted, and collects the ones that changed.
  The TABs after it offer the new names. So a test you add shows up in
  completion up to ten minutes (and one TAB) after you save it, or at once
  after an `otto test` run, a `--list-tests` or a dry run that finished,
  since each of those brings the cache up to date. (`--list-markers` only
  collects a repo that has no usable cache yet, and an interrupted run may
  leave some repos unread.) When a collection finds that a test file now
  takes its tests from another file (a new base-class module, say), the
  next check collects that test file once more; its names are right either
  way.
- **A run is never held back by completion.** Whatever completion shows, a
  run collects every test file ([above](#what-a-run-collects)).

### What the cache can't follow

The cache notices a change by a file's modification time and size, and
remembers which other files each test file takes its tests from: a base
class in another module, a library, a module it imports whole. The whole
test tree is collected again on the first TAB in a repo; after you add or
edit a pytest config file at the repo's root (`pyproject.toml`,
`pytest.ini` and the like) or edit its `.otto/settings.toml`; after a
package is installed, upgraded or removed; when you switch to another
Python or virtualenv; and a day or more after the last whole-tree
collection. Some changes add or remove a test without touching any file the
cache watches:

- a test whose existence depends on non-Python data (a module that loops
  over a JSON file, a custom collector for YAML files) or on an environment
  variable;
- a test that depends on a plain value (a flag, a number) imported from
  another module (`from cfg import FLAG`, then `if FLAG:` around a test);
- an edit that leaves a file with the same size and modification time.

After such a change, TAB keeps offering the names the file had before until
it is collected again: by the next `otto test` run with names, a
`--list-tests`, or a dry run; by editing or `touch`ing the file; or by
[`otto cache clear`](../cache/index.md#clear). A run is never affected: it
collects every file.

How the cache and the background process work is on
{doc}`../../architecture/subsystems/completion-cache` ("The test-names cache").
For the exact, fully expanded list of what a selection runs,
`otto test --list-tests` prints every collected test.
