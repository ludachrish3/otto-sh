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

## What a run imports

Each repo's pytest session both finds the names and runs what they select.
otto keeps a cache of what pytest last collected from each test file, the
**test-names cache**, and the session imports only the test files that
cache says hold one of the names, plus any test file you changed or added
since the cache last saw it, so a test you just wrote runs straight away. A
directory you added a file to is collected whole, so pytest, with your
`python_files` and `collect_ignore`, decides which of its files are tests.

- **Only pytest fills the cache.** Every name in it is one a pytest
  collection found: a run records what its own session collected, a
  listing or a dry run what its collection found, and tab completion runs
  its collections in a background process ([below](#tab-completing-names)).
  otto never reads a test file itself, so inherited, parametrized and
  generated tests are all in it.
- **A file gets its tests from other files too:** a base class in another
  test module, a library, an editable install or any directory on your
  `PYTHONPATH`; an imported test function; whatever a `from ... import *`
  brings in; a module it imports whole (`import cfg`, then `if cfg.FLAG:`
  around a test). The cache remembers those files for each test file, from
  what pytest imported, even for a class that has no test yet, and a change
  to one of them sends the test files that use it back through pytest. So a
  test a base class or a star-imported module gains, or one a flag turns on,
  runs everywhere it lands.
  Installed packages and the standard library aren't watched file by file:
  installing, upgrading or removing a package already collects the whole
  tree.
- **The whole test tree is collected** on the first run in a repo; after
  you add or edit a pytest config file at the repo's root (`pyproject.toml`,
  `pytest.ini` and the like) or edit its `.otto/settings.toml`; after a
  package is installed, upgraded or removed; when you switch to another
  Python or virtualenv; and on the first use a day or more after the last
  whole-tree collection.
- **A name the cache has never seen** (a typo, say, or a test in a file you
  just saved) is looked for, before any test runs, in what the cache can't
  vouch for: the files you changed or added, and the whole tree of a repo it
  holds nothing for yet. When one repo has such files, its own session reads
  them and runs first, and it must find the name before it runs a test; when
  several do, each is collected first, running nothing. Either way, a repo
  whose collection can't finish (a conftest that fails to load, say) ends
  the run there, before any test, with pytest's exit code and the reason
  logged. A name found nowhere is the error above, which also names any
  file that failed to collect, and no test runs in any repo. The session
  that stopped prints pytest's session header and its "no tests ran" line
  (and whatever a conftest printed while it loaded); its JUnit file is
  neither written nor put in place of an earlier one.

### What the cache can't follow

The cache notices a change by a file's modification time and size. A few
changes add or remove a test without touching any Python file the cache
watches:

- a test whose existence depends on non-Python data: a module that loops
  over a JSON file, or a custom collector for YAML files;
- a test that depends on a plain value (a flag, a number) imported from
  another module, whether the test file imports it (`from cfg import FLAG`,
  then `if FLAG:` around a test) or a module it imports does (`cfg` doing
  `from cfg2 import FLAG`);
- an edit that leaves a file with the same size and modification time,
  which is rare for a file a person edits.

After such a change a run doesn't send the test file back through pytest,
so it doesn't see the change. A test that appeared, under a name no other
file holds, is refused as unknown, with the error above. When another file
already holds the same name, the run runs that file's test and misses the
new one. Tab completion shows the names the file had before.

If one of these applies to you, do one of the following:

- **Edit the test file**, or just update its modification time
  (`touch tests/test_flags.py`). The next run collects it again, and so does
  tab completion's next check.
- **Clear the cache** with [`otto cache clear`](../cache/index.md#clear).
  The next run collects every file again. It also deletes the compiled test
  files, so that run is slower.
- **List everything**: `otto test --list-tests` with no names and no
  `--markers` collects every file again and prints the result.

Writing `import cfg` in place of `from cfg import FLAG` avoids the
plain-value case when `cfg` defines `FLAG` itself. The cache also catches up
by itself the next time it collects the whole tree: on the first run or TAB
a day or more after the last time.

## A file that fails to collect

A file that fails to import is logged as an error naming it:

```text
ERROR    Test collection failed for tests/router/test_broken.py in repo 'acme':
         ModuleNotFoundError: No module named 'nonexistent_mod'
```

It doesn't stop the tests you asked for: they still run. As in pytest, a run
that hit a collection error exits 1 even when every test passed. The file is
reported by the run that first reaches it after it breaks; later runs that
don't need it leave it alone until you edit it.

## Several repos

Each repo runs its own pytest session, and gets its own JUnit file and
artifact directory ([Where a run's files go](index.md#where-a-runs-files-go)).
A repo in which no test is selected takes no part in the result. Which repo
holds a name is decided from every repo's cache before any session starts:
a repo whose cache says it holds none of the names, and that has no file the
cache can't vouch for, gets no session at all. A repo whose cache is cold is
collected whole, since only that can show a name is not there too. Repeating
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
  must be collected whole ([above](#what-a-run-imports)), the TAB waits
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
  run checks every test file first and collects the ones that changed.

How the cache and the background process work is on
{doc}`../../architecture/subsystems/completion-cache` ("The test-names cache").
For the exact, fully expanded list of what a selection runs,
`otto test --list-tests` prints every collected test.
