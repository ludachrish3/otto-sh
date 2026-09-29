# Test-name cache: per-file pytest records, one session per run

Design for issue #457's open budget question. Status: proposal for the owner.
Scope: `otto test NAME` (validation + the run) and single-symbol tab completion
of test names, class names and `-m` markers. Compound completion (nodeids,
`-k` expressions, several symbols in one word) is out of scope; §3.9 says in
one line how this leaves room for it.

All paths are relative to the worktree `.claude/worktrees/plain-pytest-457`.
Every number in §4 comes from probes run on 2026-09-27 against a
50-file / 5-dir generated repo (`tests/_fixtures/generated_repo.py`,
`realistic=True`, the `test_repo` surface's shape) in
`/tmp/claude-1000/fable-cache-design/`, counted with the same strace filter the
budget harness uses (`-e trace=%file,getdents64`, `/proc` `/sys` `/dev`
excluded), against the installed pytest 9.1.1
(`.venv/lib/python3.10/site-packages/pytest/__init__.py`).

## 0. Recommendation in short

1. Keep pytest as the only thing that ever collects. otto's plugin, inside the
   one pytest session a run already starts, prunes the files to collect
   (`pytest_ignore_collect`), matches names to items
   (`pytest_collection_modifyitems`, the existing `matches_name` rule) and
   records what it collected. The separate name-resolving collection
   (`Repo.collect_tests` + `resolve_selection`) goes away.
2. Replace the whole-corpus `__collected_tests__` blob with a **per-file
   table**: for each test file, its stat triple and the `(classes, name)`
   pairs pytest collected from it, plus a directory-mtime table. A run and the
   completer validate it with one `stat` per file and directory (58 for the
   fixture; ~1 s → ~60 ms on a 1 ms NFS) and re-collect only the files whose
   stat moved, or the directories whose mtime moved.
3. The `ast` scan stays as the seed and the synchronous bridge for a file that
   just changed; the pytest record for a file replaces the static one as soon
   as a run or the background warm has collected that file.
4. Expected: warm `otto test TestTop0` drops by about 1,500 file ops (7,344 →
   ≈5,900, under the 6,539 ceiling and ≈8.5× `--version`, under the 9.46×
   target), with workspace ≈1,223 → ≈250. The remaining +60 over the old
   workspace baseline is the stat pass, which is the price of any cache that
   is right.

## 1. How it works today

### 1.1 A warm `otto test TestTop0`

| Step | Where | Cost (fixture, strace) |
| --- | --- | --- |
| bootstrap, `run_tests` | `src/otto/suite/run.py:729` | as before #457 |
| **collection 1**: `resolve_selection` → `Repo.collect_tests()` per repo: in-process `pytest.main([tests dirs, "--collect-only", -p no:cacheprovider, ...])`, `sys.dont_write_bytecode=True`, stdout/err to devnull, then `_evict_repo_modules` | `src/otto/suite/selection.py:106-150`, `src/otto/config/repo.py:559-756`, `:1007` | imports all 52 test files: **+1,020 workspace**, +480 elsewhere (plugin entry-point rescan ≈250, hypothesis re-registration ≈200) |
| name → nodeid: `matches_name` over every item, `_absolute_nodeid` | `selection.py:64`, `:93` | CPU only |
| **collection 2**: `_run_pytest_session([nodeids, ...])` — pytest collects the named file again (module evicted, so re-imported) and runs | `run.py:569-685` | ≈4,400 total, 107 workspace |

Measured on the branch (task-11 report): 7,344 file ops, workspace 1,223,
10.57× `version_repo`. Growth over the 5,945 baseline: +1,399, of which
+1,049 is workspace — the first collection importing every test file — and
the rest is the second `pytest.main()`'s fixed overhead. Probe S4 vs S3 in §4
reproduces the shape: the collect pass costs 1,502 file ops here, 1,021 of
them workspace.

`-m EXPR` alone does the same whole-tree collection per repo
(`repos_with_marker_matches`, `selection.py:165`) only to learn which repos
have a match, then runs a second session with `-m` again.

### 1.2 A warm TAB

- A bash TAB enters `otto._shim.main` → `_shim_complete.answer_or_reason`
  (`src/otto/_shim_complete.py:749`). It opens the one cache file, walks the
  stored command tree, and for a `NAMES` or `-m` site validates the `names`
  and `tests` key sets by comparing stored stat triples (`validate_keys`
  `:667`, `_stat_pass` `:620`) — one `stat` per test file, conftest, config
  and directory — unless the per-key marker file is under 60 s old, in which
  case zero stats. It then reads the static floor from the `tests` section and
  the pytest-collected names from `__collected_tests__` under the key
  `payload["tests_digest"]` (`collected_entry` `:699`). Budget:
  `completion_repo_warm` = 420 file ops (a names site; a tests site adds the
  stat pass outside the window).
- The `tests` section is the `ast` scan (`scan_test_corpus`,
  `src/otto/config/completion_cache.py:2371`), keyed by `_tests_key_paths`
  (`src/otto/config/cache_sections.py:103`): test files + conftests + pytest
  configs + settings + every directory walked.
- `__collected_tests__` (`completion_cache.py:2540-2660`) is ONE entry per
  `compute_fingerprint` (`:679` — settings, init modules, pytest configs, all
  test sources, lab files, inventory). Any edit anywhere invalidates the whole
  entry. It is filled by `--list-tests` (`src/otto/cli/test.py:385`) or by the
  TAB-time warm: `maybe_warm_collected_tests` (`:2745`) spawns `otto` with
  `_OTTO_DUMP_TEST_NAMES=1` (`src/otto/cli/main.py:974` →
  `dump_collected_test_names` `:2484`), which collects **every repo's whole
  tree** in a subprocess, 15 s cap (`:337`), while the TAB waits.

So after one test-file edit, the next `NAMES` TAB: shim hands over
(`stale`), `entry(cache_stale=True)` bootstraps and rebuilds the sections
(one corpus walk, an `ast` parse per file), then `_names_completer`
(`cli/test.py:191`) finds the collected entry cold and blocks on a whole-tree
collection subprocess.

## 2. What pytest 9.1.1 offers, verified

| Capability | Verified behaviour | Use in this design |
| --- | --- | --- |
| Collect a subset: file paths or nodeids as args | `file.py::test_param` runs every parametrization (5 passed for 3 params + 2 generated); `file.py::TestA` runs inherited methods too (probe B1/B1b) | fallback only; see the ignore hook |
| `pytest_ignore_collect(collection_path, config)` | firstresult; consulted for every file and directory pytest scans that is not itself an argument (`_pytest/main.py` `Dir.collect`) | **the pruning mechanism**: pass the test dirs, ignore every file not in the candidate set |
| Explicit file args bypass `pytest_ignore_collect`/`collect_ignore` | `pytest ignored/test_ign.py` collects a file `collect_ignore` hides (B5; `Session.isinitpath`) | why file args are NOT used to narrow: a conftest's ignore rules would be bypassed |
| `pytest_collection_modifyitems(session, config, items)` with `tryfirst=True` | sees all items before `-m`/`-k` deselection (B6: 10 seen, then `-m` dropped its own) | name matching + per-file recording |
| `pytest_deselected(items)` | fires for anything a plugin removes; terminal reports "N deselected" | report deselection honestly |
| `--continue-on-collection-errors` | the selected test runs, the broken file is reported, exit **1** (B2); without it exit 2 and nothing runs | cold path only |
| `config.getini("markers")` in `pytest_configure` | declared (`slow`, `hw`) **and** plugin-registered (`asyncio`, `timeout`, `hypothesis`, `repeat`, ...) (B4); `pytest --markers` prints the same list (B9) | marker source of truth; replaces `Repo.configured_markers` |
| `item.iter_markers()` | applied markers, inherited from class/module | per-file marker record (as today) |
| `--collect-only -q` | prints one nodeid per line (B1) | not needed: in-process hooks give items with markers |
| `config.cache.get/set` (`_pytest/cacheprovider.py`) | JSON under `<rootdir>/.pytest_cache/v/...`; absent under `-p no:cacheprovider` (B3); rootdir for a file arg with no ini is the common ancestor of the arg and the **invocation dir** (B7 — it landed outside the repo) | rejected as the store, §5 |
| Assertion-rewrite `.pyc` | honours `sys.pycache_prefix` (`_pytest/assertion/rewrite.py`); suppressed by `sys.dont_write_bytecode` | leave the run as is; the collect-only pass that set `dont_write_bytecode` disappears |
| `-k EXPR` | substring/keyword match: `-k test_x` also collects `test_xy` (B8) | not a name-validation tool; otto's rule is exact |
| Second `pytest.main()` in one process | re-scans every distribution's entry points (≈250 file ops), hypothesis re-registers (≈200) (§4 trace diff) | one session per repo per run |

**What pytest does not offer:** no collection cache of any kind (every session
imports every collected module), no per-file "names in this file" query
without importing the file, no hook that says "this file did not change".
`cacheprovider` remembers failures and durations, not items. The rewrite
`.pyc` is the only per-file cache pytest keeps, and it caches bytecode, not
collection.

## 3. The design

### 3.1 The store: a per-file table

`__collected_tests__` becomes schema 4, keyed per repo (`sut_dir`), not per
corpus fingerprint:

```json
"__collected_tests__": {
  "<sut_dir>": {
    "schema_version": 4,
    "generated_at": 1727400000,
    "env": {"pytest": "9.1.1", "otto": "0.16.1",
            "site_packages": [mtime_ns, size],
            "configs": {"<sut>/pyproject.toml": [mtime_ns, size], "<sut>/pytest.ini": null, ...},
            "settings": [mtime_ns, size]},
    "dirs":  {"<abs dir>": [mtime_ns, size], ...},
    "files": {"<abs test file or conftest>": {
                "stat": [mtime_ns, size],
                "source": "pytest" | "ast",
                "tests": [[["TestTop0"], "test_x"], [[], "test_plain"], ...],
                "markers": ["asyncio", "slow"],
                "error": null | "SyntaxError: ..."}},
    "registered_markers": ["slow", "hw", "asyncio", "timeout", ...],
    "names": ["TestTop0", "TestTop0::test_x", "test_x", ...],
    "markers": ["asyncio", "hw", "slow", ...]
  }
}
```

- `tests` holds `(classes, base name)` pairs — exactly what `selectable_names`
  and `matches_name` consume today (`repo.py:188`, `selection.py:64`).
  Parametrization ids collapse to the base name, as the scope requires.
- `names`/`markers` are the precomputed union the shim reads; the per-file
  data is for validation and refresh. Writers recompute both.
- A conftest is a file record with no tests: its stat triple is what matters.
- `error` records a file that failed to collect, keyed by the stat it failed
  at, so it is not re-collected on every run until edited, and the dry run
  and `--list-tests` can name it.
- `env` is the "everything else" key: the pytest config files (already in the
  `tests` key set, `repo.py:233`), `settings.toml`, the venv's
  `site-packages` directory stat (its mtime moves when a distribution is
  installed or removed — the cheapest honest proxy for "the plugin set
  changed"), and the pytest and otto versions.

The `tests` section (the `ast` floor) and its key set stay as they are.
`compute_fingerprint` stops keying anything test-related and can lose its
corpus walk (only `__collected_tests__` used it; the sections use
`section_digest`).

### 3.2 Validating and classifying (one stat per path)

`classify(repo, table)`, shared by the run, the completer and the warm
subprocess:

1. Stat every `files` and `dirs` key (stored triples, no walk — the shim's
   `_stat_pass` shape). 58 stats on the fixture (52 files + 6 dirs),
   `os.walk`-free.
2. A file whose triple matches → **fresh**. Differs → **changed**. Gone →
   **deleted** (record dropped).
3. A directory whose mtime moved → list it once (`corpus_snapshot.walk`
   pruned by `norecursedirs`, `python_files` match) → files without a record
   are **new**; a new subdirectory is walked and its files are new too.
4. `env` mismatch (a pytest config, `settings.toml`, `site_packages` or a
   version) → **everything is changed**.
5. A **changed conftest** marks every record under its directory changed
   (pytest's conftest scope is its directory and below; a conftest between a
   tests dir and `sut_dir` is in the key set today, `completion_cache.py:622`).

Not tracked by stat: the repo's `libs` (a base class in `pylib/` that gains a
test method changes a derived file's collection without touching its stat)
and anything a plugin generates. §3.5 says how the run stays correct anyway;
completion is bounded by the 24 h TTL, as today.

### 3.3 `otto test NAMES`: one pytest session per repo

`run_tests` (`run.py:729`) stops calling `resolve_selection`. Per repo:

1. `classify`. Candidate files = files whose fresh record matches any name
   (`matches_name` over the record's pairs) ∪ changed ∪ new. With names all
   matched by fresh records and nothing changed, the candidates are exactly
   the files that hold the names — the pure warm path.
2. Start the session as today (`_run_pytest_session`), targets = the repo's
   test dirs (as the `-m`-alone branch already does), with `OttoPlugin`
   carrying `candidates` and `names`:
   - `pytest_ignore_collect` (extend the existing one, `plugin.py:261`):
     ignore any test file not in `candidates`, and any directory under a test
     dir that contains no candidate. Because the test dirs are the arguments,
     conftests' own `collect_ignore`/`pytest_ignore_collect` still apply
     (§2, B5), rootdir does not move, and nothing is imported that the cache
     did not name.
   - `pytest_collection_modifyitems(tryfirst=True)`: record every collected
     item into per-file `(classes, name)` pairs and marker names before any
     other plugin deselects; then, when names were given, keep the items
     `matches_name` selects (in collection order, so `--no-random` order is
     pytest's) and hand the rest to `pytest_deselected`. Names left unmatched
     are reported on the plugin.
   - `pytest_collectreport`: a failed module report → `error` on that file's
     record (the existing `_Collector` logic, `repo.py:634`).
   - `pytest_configure`: `config.getini("markers")` → `registered_markers`.
3. After the session, write the records for the files that were collected
   (fresh stat triple taken at collection time), the dirs, `env`, and the
   recomputed `names`/`markers`. Atomic write, reserved namespace, same rules
   as `_record_collected_tests` (`completion_cache.py:2610`) minus the
   ephemeral-fingerprint gate: the table depends on no inventory.
4. **Unmatched names → stage B (cold):** run the same session once more with
   an empty ignore set (the whole tree) and `--continue-on-collection-errors`.
   Its records refresh every file. Names still unmatched raise
   `UnknownSelectionError` with did-you-mean over the freshly recorded
   universe (`unknown_names_message`, unchanged). This is the only place a
   whole-tree collection happens for a run, and it is a run, not a collect
   pass followed by a run.
5. Exit codes: rc 5 (nothing collected/selected) from a repo is "no match in
   this repo", folded as today; every repo at 5 → `NoTestsMatchedError`.

Why stage A runs the changed/new files too: the common loop is "add a test,
run it". `otto test test_new` finds no fresh record for `test_new` but
`test_5.py` changed, so it is a candidate; pytest collects it, the plugin
matches, the run happens, and the record is refreshed — no whole-tree
collection. It also means a run keeps the cache current for the files the
user is touching, so the TAB warm rarely has anything to do.

A stage-A candidate that is broken interrupts the session (exit 2) unless
`--continue-on-collection-errors` is passed there too; see open question 1.

### 3.4 Seeding and refresh

| Writer | What it writes | When |
| --- | --- | --- |
| Section rebuild (`entry()`, `cli/main.py:1044`) | `source: "ast"` records for files with no record or a stale one (the `ast` scan is per file already; it just keeps its results per path) | any `names` miss or stale bash TAB, as today; imports no pytest |
| `otto test NAMES` run | `source: "pytest"` records for the files it collected; `env`; `registered_markers` | every run |
| `--list-tests` | every file (it collects the whole tree) | every unfiltered listing, as today |
| TAB warm subprocess (`_run_collect_subprocess`, `completion_cache.py:2706`) | `source: "pytest"` records for **changed/new/ast-only files only**: the child runs the same collect-only session with the ignore set = everything else | a `NAMES`/`-m` TAB whose table has non-fresh or `ast` records; same lock, cooldown and 15 s cap |

The completer (`_names_completer`) offers: the static floor for every file
(fresh by construction after the rebuild) ∪ pytest names of **fresh
`pytest` records**. A changed file's stale pytest names are not offered (they
might be deleted); its static names are. The warm subprocess then upgrades it.

### 3.5 Correctness

| The static seed or a stale record gets it wrong | How a run stays right |
| --- | --- |
| Inherited tests (`TestA(BaseTests)` in another module), `pytest_generate_tests`, conftest-provided items, `python_classes`/`python_functions` overrides | Never in an `ast` record; present in the pytest record once the file is collected. A name in none of the records → stage B collects the tree and finds it or says "did you mean". |
| A name removed from a file whose stat did not move (base class edited in `libs`) | Stage A collects the cached file, pytest finds no such item, the name is unmatched → stage B → did-you-mean. |
| A name added the same way | Not in any record → stage B finds it. |
| A file edited to remove a name | Its stat moved → changed → collected → the record no longer has it → unmatched → stage B → did-you-mean. |
| A new file with the name | Directory mtime moved → new → collected in stage A. |
| Conftest edit that changes what collects under it | Subtree marked changed → collected in stage A. |
| Plugin installed/removed, pytest upgraded, pytest config edited | `env` mismatch → everything changed → stage A is a whole-tree run. |
| Same-size, same-mtime edit | Undetectable by stat, as for every section today (documented on `completion-cache.md` "Why stat-based"). |
| Two machines sharing `$OTTO_HOME` | Stat triples are per path, same as the sections; see `startup-performance.md` "Shared-home safety". |

Completion can offer a name that no longer exists (a `libs` edit) until the
TTL or a run touches the file. Selecting it gives the did-you-mean error, not
a silent empty run.

### 3.6 Markers (single symbols)

- Floor: the `ast` marker scan (unchanged) + `OTTO_MARKERS`.
- Truth: `registered_markers` from `config.getini("markers")` in the run's
  `pytest_configure` (declared in any pytest config file **and**
  plugin-registered — B4) plus applied markers from `item.iter_markers()` per
  file record.
- `Repo.configured_markers()` (`repo.py:790`, a hand parser of
  `pyproject.toml`) and `get_markers_panel` read the table's
  `registered_markers` when present and fall back to the parser only for a
  never-collected repo — or, if the owner prefers, `--list-markers` runs one
  `pytest --markers`-equivalent (a session with no targets) and the parser is
  deleted. The parser re-implements pytest config reading, which is the kind of
  duplication the owner's model rules out.

### 3.7 `--list-tests`, `-m`, the dry run

- `--list-tests [NAMES]`: unchanged in behaviour — a listing is where a user
  asks pytest what exists, so it collects the whole tree (one collect-only
  session per repo). Its recording becomes per file. `_refuse_unknown_names`
  stays.
- `-m EXPR` alone: no pre-collection. One session per repo over its test dirs
  with `-m`; rc 5 = no match in that repo; all 5 → `NoTestsMatchedError`.
  That removes the other whole-tree collection (`repos_with_marker_matches`).
- `-m` with names: names validate against the unfiltered records; pytest's
  `-m` deselects. A known name that `-m` excludes yields "no tests matched",
  not today's misleading did-you-mean (today's name resolution collects
  **with** `-m`, so an excluded name looks unknown).
- Dry run (`print_static_tests`, `cli/test.py:419`): reads the table instead
  of re-scanning — pytest records where fresh, `ast` records otherwise — and
  still expands no parametrization. Still imports nothing.

### 3.8 The shim and a TAB

`_shim_complete.collected_entry` (`:699`) loses `tests_digest`: after
`validate_keys` has passed the `tests` key set, it reads
`__collected_tests__[<sut_dir>]["names"]` per repo, checking `schema_version`
and TTL as now. The shim's stat pass already covers exactly the file and
directory set the table depends on; no new stat. The `tests_digest` field
leaves the shim payload; `SCHEMA_VERSION` and the shim's `SCHEMA` bump
together (pinned by `tests/unit/shim`). A stale TAB still hands over, and the
full path's warm now collects the changed files only.

### 3.9 Room for compound completion later

Each record already holds `(classes, name)` per file and the file path, which
is the data a `file.py::Class::test` or multi-symbol completer would need;
nothing here has to change shape for it.

## 4. Expected cost, with the probes

Probe scenarios (`/tmp/claude-1000/fable-cache-design/probe.py`; second of two
runs, venv bytecode warm, fixture files cold as in the harness):

| Scenario | total | workspace | notes |
| --- | ---: | ---: | --- |
| S0 `python -c pass` | 249 | 0 | |
| S1 `import pytest` | 1,558 | 0 | pytest + plugins' import |
| S2 collect-only, whole tree (today's pass 1) | 4,332 | 955 | 52 items |
| S3 run session on one nodeid (today's pass 2) | 4,399 | 107 | |
| **S4 S2 then S3 in one process (today's shape)** | **5,901** | **1,128** | |
| S5 one session, whole tree, plugin prunes nothing, matches `TestTop0` (cold / stage B) | 5,232 | 1,015 | 1 passed, 51 deselected |
| **S6 one session, one file, plugin (warm)** | **4,291** | **106** | |
| S7a / S7b / S7c collect-only on 1 / 2 / 11 files | 3,503 / 3,659 / 4,633 | 146 / 244 / 940 | **≈90–100 ops per extra file** |
| S8 stat pass over 52 files + 6 dirs (`os.walk` + stat) | 330 (−249 = 81) | 69 | stored triples, no walk: 58 |

Trace diff S4 − S3 (the collect pass): 1,020 workspace, 208 hypothesis, 244
`dist-info` reads (pytest's per-session entry-point scan), ≈30 misc.

Applied to the surface (measured 7,344 / workspace 1,223 / 10.57×):

| Path | file ops (est.) | workspace (est.) | vs today |
| --- | ---: | ---: | --- |
| warm `otto test TestTop0` (pure warm: cached file only + 58 stats + 1 cache read/write) | ≈5,900 | ≈240–270 | **−1,450 (−20 %)**; ≈8.5× `version_repo`; under the 6,539 ceiling; the 191 workspace ceiling needs ≈260 |
| warm run after editing one unrelated nested file | ≈6,000 | ≈360 | +1 file collected (≈100) |
| cold run (unknown name / conftest / config / plugin change) | ≈6,700 (S5 vs S4: one session, not two) | ≈1,100 | −650 vs today, once per such change |
| warm `NAMES` TAB inside the 60 s window | 420 | 0 | unchanged |
| warm `NAMES` TAB outside the window | ≈480 | 58 | unchanged (already a stat pass) |
| stale TAB after one test-file edit: rebuild + warm subprocess | rebuild unchanged; subprocess ≈ S7a + bootstrap instead of S2 + bootstrap | −(N−1)×≈95 | fixture: ≈−4,800 ops in the child; real repo of 400 files: ≈−38,000, i.e. seconds → ≈1 s at 1 ms RTT |

The single session removes essentially all of the +1,400: the whole collect
pass (1,020 workspace + ≈480 fixed) is what a second `pytest.main()` cost, and
the design has none. What comes back is the stat pass, ≈60 ops, ≈1.2 per test
file, which no correct cache can avoid; on the fixture it is a 5 % tax on the
workspace baseline of a run and ≈6 % of what it replaces.

The per-file saving on refresh is ≈95 file ops per file not re-collected
(S7), i.e. it scales with corpus size; the fixed cost of any collecting
process (≈1,500 for pytest's import + ≈2,000 for otto's bootstrap in the
subprocess) does not move.

## 5. Alternatives rejected

- **Keep the double collection, raise the ceiling.** Pays ≈100 file ops per
  test file on every run for information the cache can hold; the owner ruled
  it out.
- **Narrow pytest by passing the cached file paths / nodeids as arguments.**
  Explicit arguments bypass `pytest_ignore_collect` and `collect_ignore` (B5)
  and move `rootdir`/cache dir with the invocation dir (B7); the ignore hook
  narrows the same way without either effect and is one plugin method.
- **Store per-file records in `config.cache`.** Lands `.pytest_cache` in the
  user's rootdir (or the cwd's common ancestor, B7), moves a directory mtime
  the key set watches (#456), is per rootdir not per workspace, and the
  stdlib-only shim would have to find it. otto's cache file is already the
  one place.
- **`-k` for name selection.** Substring semantics (`test_x` selects
  `test_xy`, B8); otto's rule is exact and already written.
- **`pytest --collect-only -q` in a subprocess and parse nodeids.** Loses
  markers and collect errors; the in-process hooks already give items.
- **Content-hash the files.** One open and read per file per check — the
  cost the cache exists to avoid; stat triples are what every other section
  uses.
- **Per-file `ast` records as the truth, pytest only on a miss.** Wrong for
  inherited, generated and conftest-provided tests, and against the owner's
  model; `ast` seeds only.
- **A detached collection daemon / watcher.** Listed non-goal (#447); the
  per-file table makes the synchronous warm cheap enough not to need one.
- **Keying the table by `compute_fingerprint` with per-file sub-entries.**
  One digest over everything is exactly the all-or-nothing invalidation being
  removed.

## 6. Tasks (subagent-sized)

1. **The table and `classify`** (`otto/config/completion_cache.py`, new module
   `otto/config/test_records.py` if it reads better). Schema 4 records,
   read/write/merge, `classify` (stat pass, dirs, new/changed/deleted,
   conftest subtree, `env`). Tests: `tmp_path` repos with pytester-free
   fixtures — edit a file, add a file, delete a file, touch a conftest, swap
   a pytest config, fake `site_packages` mtime; assert the classification and
   that the stat count equals files + dirs (monkeypatch `os.stat`, as
   `corpus_snapshot` tests do). The `ast` rebuild writes `source: "ast"`
   records per file.
2. **The plugin: prune, match, record** (`otto/suite/plugin.py`). Extend
   `pytest_ignore_collect` with the candidate set; add the `tryfirst`
   `pytest_collection_modifyitems` matcher using `matches_name`, the
   collect-report error capture, `registered_markers` from `pytest_configure`.
   Tests: pytester sessions with a class in one file and a decoy in another —
   the decoy's module never imports (`sys.modules` / an import-side-effect
   file); inherited and parametrized items match by base name; deselected
   count reported; `-m` still applies after matching; a `collect_ignore`d
   file stays ignored with the dirs as targets (the B5 pin); a broken
   candidate's `error` lands in the record.
3. **`run_tests` on one session** (`otto/suite/run.py`, `otto/suite/selection.py`).
   Stage A / stage B, rc-5 folding, `-m`-alone without pre-collection, records
   written after each session, `resolve_selection`/`repos_with_marker_matches`
   deleted (or reduced to the pure `matches_name`/`unknown_names_message`
   helpers). Tests: the existing `tests/e2e/test_selection_runs.py` and
   `tests/unit/suite` selection tests re-pointed; new: a warm run after an
   edit collects only the edited file (count imports via a module-level
   side-effect file per test module); an unknown name goes cold once and
   errors with did-you-mean; a name `-m` excludes says "no tests matched";
   `--no-random` keeps collection order (the §3 order pin). Import budget:
   `test_repo` measured, ceiling regenerated only in the plan's final task.
4. **Completer, warm subprocess, `--list-tests`, dry run, markers**
   (`otto/cli/test.py`, `completion_cache.py:2484-2800`, `repo.py:790`). The
   child collects only non-fresh/ast files (ignore set passed through the
   dump env var or a JSON file); `_names_completer` unions floor + fresh
   pytest records; `record_collected_tests_from_items` → per file;
   `print_static_tests` reads the table; `configured_markers` prefers
   `registered_markers`. Tests: the existing completer/warm tests
   (`tests/unit/cli/test_lab_tests_completers.py`, cache warm tests) plus: a
   stale record's pytest names are not offered while its `ast` names are; the
   child's collection touches only the stale file.
5. **Shim + schema bump + docs** (`otto/_shim_complete.py`,
   `completion_cache.py` `SCHEMA_VERSION`, `docs/architecture/subsystems/completion-cache.md`,
   `completion.md`, `startup-performance.md`, the spec §4.6/§5.5 note).
   `collected_entry` reads per repo without `tests_digest`; the shim payload
   drops it. Tests: `tests/unit/shim/test_differential.py` and the schema pin;
   `otto cache info` output. Docs: the "Who reads, who refreshes" table gains
   the `otto test NAMES` row (reads and writes `__collected_tests__` only,
   never a section), the reserved-namespace paragraph is rewritten for the
   per-file table, "Deliberate non-goals" loses nothing.
6. **Gate and budget** (final task, squash-aware). `make coverage`,
   `nox -s tests_hostless-3.14`, `make typecheck`, `make import-snapshot`
   for `test_repo` with the new numbers and the workspace ceiling
   re-baselined; the `after.md` measurement record gets a dated addendum.

## 7. Risks and open questions for the owner

1. **Exit code when a candidate or an unrelated file is broken.** Stage B
   uses `--continue-on-collection-errors`: the requested tests run, the
   broken file is reported, exit is 1 (B2). Spec §5.5 says a broken unrelated
   file "doesn't stop the tests that were asked for" — they do run — but the
   pinned test may assert exit 0. Options: accept pytest's verdict (my
   recommendation: it is pytest's rule, and the file's `error` record means
   the warm path never touches it again until edited, so it is reported once
   per breakage); or a `pytest_make_collect_report` hookwrapper that turns a
   failed module report for a non-matching file into a logged skip. Stage A
   with a broken changed candidate: same choice.
2. **The workspace ceiling of `test_repo`.** The stat pass is ≈60 workspace
   ops on the fixture, over the 191 ceiling (baseline 174) by design. It is
   the cost of validation and scales at ≈1.2 per test file. Accept a new
   baseline (≈250), or accept an unvalidated warm path (pytest would still
   catch a removed name, but a name added to an existing file would not run
   until something else refreshed that file — I do not recommend it).
3. **`libs` edits are not tracked.** A base class in `pylib/` that gains or
   loses a test method is invisible to the stat pass. Runs stay correct via
   stage B; completion is stale until the TTL or a run touches the derived
   file. Tracking would mean walking the lib trees (one stat per lib file) on
   every run; probably not worth it, but it is a call.
4. **Runs now write the cache.** Today only completion, root help,
   `--list-tests` and `otto tunnel` touch the cache file; `otto test NAMES`
   joins the reserved-namespace writers (it writes only what it collected,
   the same rule as `--list-tests`). The doc's "any other command touches
   nothing" invariant narrows to "any command other than `otto test`".
5. **Stage A includes changed/new files the user did not ask about.** It
   imports them (≈100 ops each) so the cache stays current and "add a test,
   run it" needs no whole-tree pass. If the owner prefers a run to import
   only the named files, drop changed/new from the candidates; the cost is a
   stage-B pass on the first run of a newly written test and a warm
   subprocess that does the refreshing instead.
6. **Multi-repo runs.** One session per repo as today; stage B is per repo.
   A name unmatched in repo A but matched in repo B still triggers A's
   stage B — the same whole-tree cost today's `resolve_selection` pays for A
   on every run, so not a regression, but worth a line in the docs.

## 8. Owner decisions (2026-09-27, approved)

1. Stage B's exit code follows pytest: a broken unrelated file under
   `--continue-on-collection-errors` runs the requested tests and exits 1. No
   hookwrapper that turns the failure into a skip. The spec §5.5 wording and any
   pinned test are updated to match.
2. The `test_repo` workspace ceiling is re-baselined to cover the stat pass
   (≈250). The file-op ceiling does not rise. The re-baseline happens once, in
   the final gate task, with the measured number.
3. `otto test NAMES` writes the cache (reserved namespace only, only what it
   collected), like `--list-tests`.
4. Stage A collects changed/new files the user did not name, so "add a test, run
   it" needs no whole-tree pass.
5. `libs` edits stay untracked; runs stay correct through stage B, completion may
   be stale until a refresh. Documented.
6. Scope: all tasks (1–5 below, plus the gate folded into the plan's final gate
   task). Tab completion offers single symbols only; compound pytest strings are a
   separate future issue.

## 9. Addendum: no AST seed (2026-09-27)

Owner decisions this addendum implements: the `test_repo` surface gates the
**warm** state (seeded by a prior `otto test TestTop0`); otto's own `ast` scan
of test files is **dropped** — a pytest collection is the only thing that ever
learns a test name; the per-file table is seeded by **one mechanism** every
reader goes through; completion still offers single symbols; pytest 10 is out
of scope. It builds on 13b.1/13b.2 as landed and on 13b.3's fix rounds: a
record is trusted for what it holds, never for what it lacks, and each record
carries the dependency files (module namespace + MRO sources) whose change
invalidates it. Line references are to the worktree at `2c95704e` plus the
13b.3 fix-2 working tree.

### 9.1 One seeding entry point

```python
def ensure_table(repo: Repo, *, seed: Callable[[], RepoTable] | None = None) -> tuple[RepoTable, Classification]:
    """Return the repo's table and its classification, seeding it when cold.

    Cold = no table, ``env`` mismatch (pytest config, settings, site-packages
    stat, python/pytest/otto version), or the table older than the TTL. A cold
    table is replaced by *seed*'s result — by default one whole-tree
    ``--collect-only`` session with OttoPlugin recording everything — and
    written. A warm table is returned as classified: fresh, changed, new,
    deleted files; the caller decides what to do with the non-fresh ones.
    """
```

`ensure_table` lives in `otto/config/collected_tests.py` next to `classify`;
its default `seed` is the run module's session runner with `collect_only=True`
and `candidates=None` (13b.3's `_run_pytest_session` + `OttoPlugin`, no JUnit,
no coverage steps). It imports pytest only on the cold branch. **Every reader
of test names calls it; nothing else ever writes a whole table.** From then on
every reader sees a hit, or a small per-file update done by whoever collects
next.

The owner's model holds with two bends, both about *which session* is the
seed, not about whether there is one:

| Reader | Cold table | Warm table, non-fresh files | Notes |
| --- | --- | --- | --- |
| **bash TAB** (shim) | hands over `stale` (no table / env / TTL) → full path → completer | **answers from the table**: last-known names of every record that still exists, and **spawns the refresh child detached** when it saw a moved stat (9.2) | the shim runs no pytest, so the seed cannot happen there |
| **completer** (full path, all shells) | calls `ensure_table` **inside the disposable child** (`_run_collect_subprocess`, 15 s cap, lock, cooldown) and re-reads; **blocks once** (recommended — see below) | offers last-known names of existing records, **does not wait**, and spawns the same child **detached** to refresh the non-fresh files (9.2) | stdout is the shell's channel, so the child is mandatory |
| **`otto test NAMES`** | **bend 1:** its own whole-tree stage-B *run* session is the seed (`ensure_table(repo, seed=<this run's whole-tree session>)`): one session, tests run, full table written. No separate collect-only pass. | stage A collects fresh holders ∪ changed ∪ new ∪ dependents (13b.3), refreshing those records | |
| **`--list-tests`** (unfiltered) | its whole-tree collect-only session **is** `ensure_table`'s default seed | it re-collects the whole tree anyway (a listing asks pytest what exists) and rewrites everything | |
| **`--list-tests NAMES`, `-n` dry run, `--list-markers`** | **bend 2:** on a cold table the whole-tree collect-only seed already answers the question (the plugin matched the names in it), so the reader prints from that session instead of seeding and then collecting again | warm stage-A collect-only over the candidates (dry run, narrowed listing); `--list-markers` reads `registered_markers` from the table and collects nothing | |
| **root `--help` / section rebuild** | **never seeds, never reads the table.** It stays the fast path it is: `names` and `shim` only, no corpus I/O | — | recommended; a help screen has no test names on it |

**TAB before any table exists — block once, up to the existing cap.** The
completer already waits for a whole-tree collection on a cold collected set
today (`_names_completer`, `cli/test.py:229-231`); the static floor it lost
was shown *alongside* the wait, never instead of it. Fixture measurements:
the child (otto bootstrap + whole-tree collect of 52 files) 0.93 s wall; a
bare `pytest --collect-only` of the tree 0.76 s, of one file 0.73 s — pytest's
fixed start dominates, ≈1 ms per plain file after it. On timeout/failure the
TAB offers nothing and the 60 s cooldown stamps; any run, listing or later TAB
seeds. "Return nothing and seed in the background" gives the same first-TAB
result one keystroke later; rejected for the seed. The detached child is
reserved for the per-file refresh (9.2), where nothing is missing that a
whole tree could fill.

### 9.2 A TAB right after an edit — last-known names now, the file refreshed in the background (owner decision)

Without the `ast` floor a changed file's record is the only source of its
names, and the TAB must stay instant. So:

- **The answer.** The shim (and the completer on the full path) treats a file
  whose stat moved as *present with stale content*: its names are offered as
  last recorded; a deleted file's names are dropped (its stat fails); nothing
  hands over and nothing waits. Correctness at run time is untouched: a
  changed file is a stage-A candidate (13b.3), so a name just added runs, and
  a name just removed is unmatched → stage B → did-you-mean, never a silent
  run. Cost: the shim's ≈420 ops plus the stat pass.
- **The refresh.** When the stat pass saw any moved file or directory stat
  (or a dependency's), the answering process **spawns the collect child
  detached** — `subprocess.Popen([<venv>/otto], env={..., _OTTO_DUMP_TEST_NAMES: 1},
  start_new_session=True, stdin/stdout/stderr → devnull, close_fds=True)` —
  and exits without waiting. The child is today's one
  (`_run_collect_subprocess`, `completion_cache.py:2706`): it bootstraps,
  takes the collect lock (`_acquire_collect_lock`, stale-lock recovery
  included), calls `ensure_table` per repo, collects **only** the non-fresh
  candidates through the collect-only session, writes the table atomically
  and exits; it prints nothing. If the lock is held it exits at once. By the
  next TAB the record is fresh. The shim stays stdlib-only: it imports
  `subprocess` only on the branch that spawns, after its answer is written,
  and it skips the spawn when `.completion_collect.lock` exists and is younger
  than `COLLECT_LOCK_STALE_SECONDS` (one `stat`), so a burst of TABs starts
  one child. The full-path completer does the same after printing.
- **What this changes for `#447`.** The completion-cache page lists
  "rebuilding in a detached process" as a non-goal because it needed a
  process lifecycle. The lifecycle now exists for the refresh alone: the
  lock, its stale recovery, the cooldown stamp on failure and the 15 s cap
  are the existing child's; the only new piece is `start_new_session=True`
  and not waiting. The docs task rewrites that non-goal: the **seed** is
  never detached (a cold TAB blocks once, 9.1), the **refresh** always is.
- **Rejected:** a synchronous per-file collect at TAB time (exact, but
  ≈0.9 s on the fixture — bootstrap + pytest start — on every TAB after every
  save); and "refresh at the next run only" (correct, but a name written
  minutes ago would stay unoffered until a run touched its file).

Consequence for the shim: a tests-site stat pass over the table's paths hands
over only for **cold** (no table for a repo, `env` mismatch, TTL expired); a
moved file or directory stat is answered from the records and refreshed
behind the TAB. Today's behaviour (any moved test path → hand over → section
rebuild → whole-tree child, all before the answer) goes away, which is the
largest saving on the path users hit most.

### 9.3 Every consumer of the AST scan, and what replaces it

*Amended by §13: the shim never stats a table's files, directories or
dependencies. It checks only `env` and the TTL, answers with last-known
names, and leaves the stat pass to the collect child, started once the
600 s check window has lapsed; the 60 s `tests` marker window is gone.*

| Consumer | Today | After |
| --- | --- | --- |
| `scan_test_corpus`, `StaticTest`, `CorpusScan`, `_static_tests_in`, `_marker_name`, `collect_test_names`, `collect_marker_names` (`completion_cache.py:2318-2530`) | the `ast` pass, the floors | **deleted** |
| the `tests` section (`cache_sections.py:103` `_tests_key_paths`, `:163` `_collect_tests`, `:208` `Section("tests")`, `MERGED_VIEW_SECTIONS`) | static floors keyed by the corpus walk | **deleted**. The table validates itself: it already stores `[mtime_ns, size]` per file, dir, dep and env path. `SECTIONS` = `names`, `shim` (`derived_from=["names"]`). |
| the shim (`_shim_complete.py`): `validate_keys` `:667` (`keys["tests"]` stat pass), `collected_entry` `:699` (`tests_digest`), `Payloads.tests`, `_source_values` kinds `tests`/`markers` `:493-502` | floor ∪ collected blob; any moved path hands over | on a tests/markers site: `_stat_pass` over the table's `env` paths (cold check → hand over `stale`), `generated_at` vs TTL (cold), then stat `files`/`dirs`/`deps` to drop deleted files, and answer with the union of the remaining records' names (or `markers` ∪ `registered_markers`). The `completion_cache.tests.ok` marker window applies to that pass. `tests_digest` and `keys["tests"]` leave the shim payload (`completion_tree.py:283-297`). |
| `compute_fingerprint` (`completion_cache.py:679`) and its corpus walk (`iter_test_sources` `:622`, `_match_py_files` `:570`, `_is_norecurse_dir`, `_NORECURSE_NAMES`) | keys the collected blob and `tests_digest` | **deleted** with the blob (`_collected_cache_entry`, `_fresh_collected_entry`, `read_collected_tests`, `read_collected_markers`, `_record_collected_tests`, `record_collected_tests_from_items`, `COLLECTED_SCHEMA_VERSION`, the `_DUMP_*` frames and `_parse_*`). `hash_file` stays for `section_digest`; `_tunnel_scope_digest` never walked. |
| the rebuild (`cli/main.py:1084-1099`: `scan_test_corpus`, `tests=`, `markers=`, `static_scan=`; `write_cache`/`write_sections(static_scan=)` `completion_cache.py:1278`, `:1391`, `:1470-1474`; `refresh_static_tables`, `static_table` `collected_tests.py:834-890`) | seeds `source: "ast"` records | **deleted**. A rebuild does no corpus I/O and never touches the table. |
| `FileRecord.source`, `SOURCE_AST`, `SOURCE_PYTEST` (`collected_tests.py:104-107`, `:136`) | tells a static record from a pytest one | **deleted**; schema 4 → 5 (no `source` field). |
| `_candidate_files` (`run.py:910-948`) | `record.source == SOURCE_AST or any(matches_name…)` | the `SOURCE_AST` clause goes; fresh holders ∪ changed ∪ new ∪ dependents stays |
| dry run `print_static_tests` (`cli/test.py:419`), `DRY_RUN_TESTS_HEADLINE`, the `StaticTest` path of `tests_tree` (`:328`) | static parse, imports nothing | **a collect-only session** (9.4) |
| `_names_completer` / `_markers_completer` (`cli/test.py:191`, `:239`) | `tests` section floor ∪ collected blob, warm on cold | `ensure_table` per repo (child when cold, block once); offer every existing record's names, last-known for changed files, then spawn the detached refresh child; `registered_markers` ∪ `OTTO_MARKERS` for `-m`. No floor. |
| `Repo.configured_markers` (`repo.py:819`, hand parser of `pyproject.toml`), `get_markers_panel` (`:838`), `list_markers_callback` (`cli/test.py:302`) | otto parses pytest's config | **deleted**. `--list-markers` = `ensure_table` per repo, then `registered_markers` (`config.getini("markers")`: declared **and** plugin-registered, §2 B4) plus the applied markers from the records, plus the `OTTO_MARKERS` panel. |
| `configured_python_files`, `_load_pytest_config`, `_ini_section`, `_split_patterns`, `DEFAULT_PYTHON_FILES` (`repo.py:243-350`), `_pattern_matcher`/`_list_dir` (`collected_tests.py:495-535`) | otto decides which files in a changed directory are test files | **deleted** (recommended, 13b.4). A directory whose mtime moved is itself a candidate: `pytest_ignore_collect` lets pytest into it (its files, and any subdirectory with no record); pytest applies `python_files`/`norecursedirs`/conftest ignores; every file it collected there gets a record. `dirs` is filled from `pytest_collect_directory` (each `Dir` under the test dirs). `pytest_config_paths` (`repo.py:233`) stays: a list of stat keys, not a parser. |
| `Repo.collect_tests` (`repo.py:559`), `CollectedTest` (`:159`) | the collect-only pass for `--list-tests` and the dump | every collect-only need goes through the run module's session with `collect_only=True`; both are **deleted**. `_evict_repo_modules` (`:1007`) stays while stage A and stage B can run in one process. |

Kept, unchanged: `selectable_names`/`classes_from_nodeid` (`repo.py:188-230`),
`matches_name`/`unknown_names_message` (`selection.py`), `classify` and
`updated_table`, `OttoPlugin`'s prune/match/record, the `names` section and
the shim's tree walk.

### 9.4 The dry run and the listings: pytest collects, nothing runs

**Dry run — recommendation: a collect-only session through the run's path.**
`otto -n test NAMES` calls `ensure_table` (a cold table → the whole-tree
collect-only seed, which already matched the names), else runs a warm stage-A
session with `--collect-only` over the candidates (stage B on an unmatched
name), no JUnit, no coverage steps, and prints the matched items as
`--list-tests` prints them. That gives what §4.3 of the parent spec could not:
parametrizations expanded, `-m` evaluated by pytest, inherited tests under
every class that has them, a did-you-mean for a real typo. It imports the
candidate files (≈100 file ops each on the warm path), which the static dry
run did not. Printing the cached records and importing nothing was rejected:
with no static seed a fresh home would show an empty tree and a stale record
yesterday's names — a dry run that can be wrong is worse than one that costs
a collection. The headline becomes "dry run: pytest collected these tests;
nothing ran". The dry-run principle (`docs/cli/dry-run.md`: no command body,
no device) holds — collection imports modules; it runs no test and opens no
host.

**`--list-tests [NAMES]`**: unfiltered = the seed session over the whole tree,
every time (a listing is where the user asks pytest what exists); with names,
warm stage-A collect-only. **`--list-markers`**: `ensure_table`, then the
table's `registered_markers` and applied markers; no collection on a warm
table.

### 9.5 Staleness and correctness with pytest-only records

*Amended by §13: a bash TAB after an edit does not see the moved stat. It
answers with last-known names, and the change reaches it one check late (up
to the 600 s window plus one TAB), or at once after any run or listing that
updated the table. Deleted files' names stay offered until then.*

- **A TAB after an edit** (9.2): the shim sees the file's stat moved, answers
  with its last-known names and spawns the detached child, which re-collects
  that file (and its dependents) and rewrites the record; the next TAB shows
  the new name. A run in between collects the file itself (it is a
  candidate).
- **A new file.** Its directory's mtime moved → the directory is a candidate
  → the detached child (or the next run/listing) lets pytest collect it →
  records for what it found, the directory re-stamped. The one TAB in between
  does not offer the new file's names — an omission, never a phantom.
- **A deleted file.** Its stat fails → the shim and the completer drop its
  names at once; the next writer drops the record.
- **Dependency invalidation** (13b.3 fix 1/2): unchanged — a record whose
  namespace/MRO source moved is changed, hence a candidate.
- **Cold** (no table, `env` mismatch, TTL): the shim hands over; the
  completer's child, a run's stage B, or a listing re-seeds the whole tree.
- **Runs** are exactly 13b.3's stage A/B without the `ast` clause; the run
  path never consulted the static layer, so nothing weakens there.
- **Remaining hole** (documented): tests generated from non-Python data.
- **What is gone:** the instant static floor of a fresh home or a broken repo.
  A repo whose collection fails or exceeds the cap gets no name completion
  until a run or listing seeds it (each shows the collection error).

### 9.6 Budget effects

| Surface / path | Before | After (expected) | Why |
| --- | --- | --- | --- |
| `help_repo` (cold rebuild) | 4,094 / ws 226 | ≈3,980 / ws ≈110 | the rebuild's corpus walk and `ast` parse go: 2 ops per test file + 2 per dir (`test_cold_rebuild_walks_the_corpus_once` documents that rate); the pin becomes "a cold rebuild's workspace does not scale with the corpus" |
| `help_repo_warm`, `completion_repo_warm`, `completion_repo_handover` | 2,889 / 420 / 2,441 | unchanged | names sites never touched the corpus |
| `test_repo`, **warm state** (seed `otto test TestTop0`; the gated state) | 13b.3 fix-1 measured 5,737 / ws 228 / 9.01× | ≈5,740 / ws ≈230 / ≈9.0× | steady state had only pytest records already; `seed_argv` → `["otto","test","TestTop0"]`; workspace ceiling re-baselined ≈250 (§8.2); the 6,223 file-op ceiling holds |
| `test_repo` state 1 (seed `otto --help`) | 6,679 / ws 1,140 | not gated; a fresh home's first run is the stage-B seed (≈ S5 shape, ≈6,700) | the first run was always a whole-tree collection under this design; it is now also the seed |
| first `NAMES` TAB in a fresh home | rebuild (2 ops/file) + whole-tree child, ≈1 s | whole-tree child only, ≈0.9 s on the fixture | same collection, minus the scan |
| `NAMES` TAB after one edit | hand-over: bootstrap + section rebuild + whole-tree child, before the answer | **the shim answers**: ≈420 + the stat pass (≈60) + `import subprocess` and one `Popen` (≈30, only on this branch); the child's ≈3,500 + bootstrap run detached, off the TAB | 9.2 |
| dry run | ≈0 beyond bootstrap | ≈ a warm run minus execution (≈4,300 on the fixture) | it collects now |
| `--list-markers` | a pyproject parse | a table read (cold: the seed) | pytest is the source |

### 9.7 Deletions and test changes, specifically

Product (functions in the 9.3 table): `completion_cache.py` loses the scan
block (`:2318-2530`), the collected-blob block (`:2540-2800` except the lock,
cooldown and child runner, which move beside `ensure_table`), `compute_fingerprint`
and the corpus walkers, the `tests`/`markers`/`static_scan` parameters of
`write_cache`/`write_sections`; `cache_sections.py` loses `_tests_key_paths`,
`_collect_tests`, the `tests` `Section`; `completion_tree.py` loses
`tests_digest` and `keys["tests"]`; `collected_tests.py` loses `source`,
`static_table`, `refresh_static_tables`, `_pattern_matcher`, `_list_dir` and
gains `ensure_table`; `repo.py` loses `collect_tests`, `CollectedTest`,
`configured_markers`, `get_markers_panel`'s parser, the pytest-config readers
(`configured_python_files` and below); `cli/test.py` loses
`print_static_tests`, the floors in both completers, the `StaticTest` branch
of `tests_tree`; `cli/main.py` loses the scan calls; `run.py` loses the
`SOURCE_AST` clause; `_shim_complete.py` loses `Payloads.tests` and the
digest lookup, stops handing over on a moved test path, and gains the detached spawn.

Tests deleted: `tests/unit/config/test_marker_scan.py` (all three; the
`OTTO_MARKERS` union moves to a `--list-markers` test);
`test_completion_labs_tests.py::test_collect_test_names_static_scan`,
`::test_the_scan_descends_into_nested_test_classes` (nested classes are pinned
by the plugin's recording test);
`test_collected_tests_table.py::test_a_static_table_records_each_file_from_the_scan`,
`::test_the_rebuild_writes_static_records_and_keeps_a_fresh_pytest_one`,
`::test_a_rebuild_without_a_scan_leaves_the_records_alone`;
`test_entry_cache_rebuild.py::test_a_rebuild_seeds_every_test_file_record_statically`
(→ "a rebuild opens no test file and writes no table");
`test_run_sessions.py::test_a_statically_seeded_table_never_hides_an_inherited_test`
(the P1 dependency tests stay); `test_listing.py::TestConfiguredMarkers` (both);
`test_completion_cache_unit.py::test_fingerprint_and_static_scan_read_the_same_patterns`.
Tests rewritten: `test_collected_tests_table.py::test_a_file_edited_while_the_scan_reads_it_is_stamped_stale`
(a pytest collection instead of the scan); `test_shim_section.py::test_shim_digest_is_derived_from_names_and_tests`
and `test_cache_sections.py::test_shim_digest_moves_iff_a_child_digest_moves`
(names only); `test_visited_dirs.py::test_a_new_nested_test_file_moves_the_tests_digest_only`,
`test_completion_cache_unit.py::test_editing_the_pytest_config_itself_moves_the_digest`,
`::test_every_pytest_config_file_is_in_the_digest` (→ `classify` says new /
env cold; mostly already in `test_collected_tests_table.py`);
`test_lab_tests_completers.py` (`*_unions_collected_over_floor`,
`*_falls_back_to_the_live_scan`, `*_warms_on_cold_collected` → cold seeds once
and blocks; changed file → last-known names, no wait); `test_listing.py`
list-markers tests; `tests/e2e/cli/test_test_listing_e2e.py` (dry run prints
collected items; `--list-markers` shows a plugin-registered marker such as
`asyncio`); `tests/unit/shim/test_differential.py` (tests site: fresh table
answers; moved file answers last-known; cold hands over);
`tests/unit/import_budget/test_import_budget.py::test_cold_rebuild_walks_the_corpus_once`
(→ O(1)); the `test_repo` surface's `seed_argv`. Dry-run captures under
`docs/_static/generated` regenerate.

### 9.8 Revised tasks (replacing 13b.4 and 13b.5)

- **13b.3 (in flight) — no new scope.** 13b.4 removes the `SOURCE_AST`
  clause. 13b.4 also adds `collect_only=True` to the session runner (no JUnit
  move, no coverage steps, records still written) if 13b.3 has no such switch.
- **13b.4 — `ensure_table`, pytest-only records, the child, the completers**
  (`collected_tests.py`, `completion_cache.py`, `run.py`, `cli/test.py`).
  Schema 5 without `source`; delete `static_table`/`refresh_static_tables`/
  `static_scan=`; `ensure_table(repo, seed=)` with the collect-only whole-tree
  default and the run passing its stage-B session; directory candidates
  replace `_pattern_matcher`/`configured_python_files` (`dirs` from
  `pytest_collect_directory`); the child runs `ensure_table` and exits (no
  stdout frames; the parent re-reads); completers: cold → child, block once;
  warm → all existing records' names, last-known for changed files, no wait,
  detached refresh spawned when anything was non-fresh.
  Tests (pytester repos): a cold TAB seeds the whole tree once and answers;
  child timeout → empty answer + cooldown; after a warm run, save a file → the
  TAB answers instantly with last-known names and spawns the child detached
  (spy on `subprocess.Popen`: `start_new_session=True`, devnull stdio, not
  waited for); the child collects exactly that file (import side-effect
  marker per module) and the next TAB shows the new name; a held lock → no
  spawn; a `run_tests` before the child finishes still collects the file
  itself; add a
  file → its directory is collected, a `collect_ignore`d sibling is not; a
  `python_files = check_*.py` config is honoured with no otto parser; env
  mismatch → whole-tree re-seed; a run on a cold table writes the full table
  from its own stage-B session and starts no collect-only session (count
  `pytest.main` calls).
- **13b.5 — listings, markers, dry run on the one session** (`cli/test.py`,
  `repo.py`, `run.py`). `--list-tests`, `--list-markers`, `-n` via
  `ensure_table` + `collect_only=True`; delete `Repo.collect_tests`,
  `CollectedTest`, `configured_markers`, `print_static_tests`; `tests_tree`
  over collected items. Tests: a cold dry run runs exactly one pytest session
  and prints from it; a warm dry run imports only the candidate files; it
  shows expanded parametrizations and an inherited test under its subclass,
  evaluates `-m`, did-you-means a typo, runs nothing (a test body that writes
  a file does not); `--list-markers` on a warm table collects nothing and
  shows a declared, a plugin-registered (`asyncio`) and an applied-only marker;
  e2e listing tests updated; dry-run capture regenerated.
- **13b.6 — drop the `tests` section; the shim answers from the table**
  (`cache_sections.py`, `completion_tree.py`, `_shim_complete.py`,
  `completion_cache.py`, `cli/main.py`). `SECTIONS` = names + shim(names);
  shim: env/TTL cold check → hand over; stat files/dirs/deps → drop deleted,
  answer last-known, spawn the detached child on any moved stat (lock-file
  stat first); `tests_digest` gone; `SCHEMA_VERSION`/shim `SCHEMA`
  bump; delete `compute_fingerprint` and the corpus walkers; the rebuild
  imports nothing from the corpus. Tests: differential on a tests site with a
  fresh table, a moved file (answers and spawns, `completion_repo_warm`
  unchanged), a deleted file (its names gone), a held lock (no spawn), a
  missing table / env mismatch / TTL (hands over stale); `otto cache info`
  wording; rebuild opens no test file; `test_cold_rebuild_walks_the_corpus_once`
  → O(1) pin; a tests-site TAB's file ops do not scale with the corpus beyond
  one stat per tracked path.
- **13b.7 — docs, harness seed, gate.** `test_repo` `seed_argv` →
  `otto test TestTop0`; `docs/architecture/subsystems/completion-cache.md`
  (sections: names + shim; the table validates itself; one seeding entry
  point; "who reads, who refreshes" rows for `otto test`, the child, listings;
  the #447 non-goal rewritten: seed blocks once, refresh is detached), `completion.md`, `execution.md`, `docs/cli/test/index.md` +
  `selection.md` (dry run collects; candidates come from pytest only; first
  TAB seeds; a TAB after a save shows last-known names until the next run),
  `startup-performance.md`, parent spec §3/§4.3/§4.6/§5.4 amendment note;
  ceilings re-baselined once in the final gate (`make import-snapshot`),
  `after.md` addendum.

### 9.9 Risks

1. **A repo whose collection is slow or broken has no completion** until a
   run or listing seeds it; the 15 s cap and the 60 s cooldown are the only
   bounds. The owner accepted this.
2. **A detached child is a process nobody waits for** (9.2). It can die
   with the terminal, race a run for the lock (the loser exits), or write a
   table a concurrent run overwrites (atomic replace, last writer wins, both
   write pytest records — no wrong record, at worst one extra collection).
   Its failures are silent by design; `otto cache info` should report the
   lock and the cooldown stamp.
3. **The dry run imports test modules.** Module-level side effects run on
   `-n`; they already run on every `--list-tests` and run; the dry-run page
   must say so.
4. **Directory candidates collect every file in a changed directory once**
   (≈100 file ops per sibling); the alternative keeps otto's `python_files`
   parser, which is the coupling being removed.
5. **Stage A→B in one process still needs `_evict_repo_modules`**; deleting
   `Repo.collect_tests` must not delete the eviction.

### 9.10 Data point: is the AST scan incompatible with upstream pytest's next major?

No, not by the advertised breaking changes. Upstream `main`'s three `breaking`
entries (`-c` path usage errors; keyword-only `indirect`/`ids`/`scope` on
`parametrize`; exit codes on plugin import failure) touch none of the rules
the scan mirrors — otto passes `--override-ini`, never `-c`; the scan reads no
`parametrize` arguments; and a plugin import failure in the collect child
already surfaces as a non-zero exit that stamps the cooldown. The scan's real
gap is with **today's** pytest 9.1.1, not the next one: it hard-codes the
default `python_classes = Test` / `python_functions = test` prefixes
(`_pytest/python.py` `_matches_prefix_or_glob_option` reads them from config
and accepts globs); it cannot see `__test__ = False`/`True` (`isnosetest`),
`inspect.isabstract` classes (skipped), classes with an `__init__` (not
collected), fixtures whose names start with `test` (`getfixturemarker`
excludes them), `unittest.TestCase` subclasses collected whatever their name
(`_pytest/unittest.py` `pytest_pycollect_makeitem`), `collect_imported_tests =
false` (a 9.x ini that drops imported test functions), inherited and
generated items, or `pytest_collect_file` providers. Every one of those is a
convention pytest owns and may extend at any release; removing the scan
removes the surface such a change would land on.

## 10. Owner decisions on the addendum (2026-09-28, approved)

1. Remove otto's AST test scan entirely; pytest is the only source of test names, classes and markers (§9 as
   written): one seeding entry point (`ensure_table`); a cold TAB blocks once on the child; a TAB after an edit
   answers with last-known names and refreshes in a detached child; the dry run is a pytest collect-only pass
   (imports test modules, runs nothing); the deletions in §9.7.
2. Keep per-file dependency tracking as built in 13b.3 (module files, base classes, star-imports, imported modules,
   editable installs / PYTHONPATH libs; stdlib and site-packages excluded). Per-file refresh over whole-tree
   re-collection. Documented holes (non-Python data, bare imported values, sourceless star-imports) stay documented.
3. `test_repo` gates the warm state (seeded by a prior `otto test TestTop0`); the workspace ceiling is re-baselined
   (≈250) in the final gate.
4. Tab completion offers single symbols; compound selections are #489.

## 11. Simplification review (2026-09-28)

Read against the worktree at `1fcb7da4` plus the 13b.4 fix-1 working tree.
Sizes are `awk`-counted from the current files: `run.py` 1,478 lines
(`_Sessions` 257, `_SessionStdout` 57, `_collect_table` 61), `plugin.py`
1,274 (the `__pycache__` settle 67, the dependency walk 73),
`collected_tests.py` 1,100 (`_merged_dirs` 34, the child deadline 45).
Two probes under `/tmp/claude-1000/fable-simplify/` back the load-bearing
claims: `p1` (a `pytest.UsageError` raised from `pytest_collection_modifyitems`)
and `p2` (a `builtins.__import__` observer).

### 11.1 Question 1 — can stage B and the abandonable first pass go away? Yes.

**The hypothesis holds.** After 13b.3, a fresh record is already trusted for
what it lacks everywhere except the code path that then re-collects the
whole tree "in case". Walk the cases with candidates = fresh holders ∪
changed ∪ new ∪ candidate dirs (13b.3's `_candidate_files`, `run.py:910`):

| Case | Where the name is | Is it a candidate? | Verdict without stage B |
| --- | --- | --- | --- |
| add a test, run it | the file just saved (changed) | yes | matched in the one session; unchanged |
| new file | its directory moved → candidate dir | yes | unchanged |
| changed conftest (S6 conftest base, `collect_ignore` edits) | every file under it is changed, its dirs are candidates | yes | unchanged |
| env mismatch / no table / TTL | anywhere | the whole tree is the session (the seed) | unchanged: one session, tests run in it |
| dependency first seen this run (S9) | holder stored with a `None` dep stat → changed | yes | unchanged |
| base class / lib / star-import / `import cfg` gains a test (P1, S1–S4, S7, A–F, G2) | holders are changed through `deps` | yes | unchanged |
| a file whose record is an `error` | it failed to collect; the record is fresh with no tests | no | today stage B re-collects it and it fails again → did-you-mean. Without stage B: unmatched → the same error, and it can name the broken file from the record ("`tests/x.py` did not collect: SyntaxError …"). Better, not worse. |
| the documented holes: non-Python data, `from cfg import FLAG`, a stat-invisible edit | a fresh record that lacks the name | no | today: found by stage B only when no *other* file holds the name (the docs already call this inconsistent). Without stage B: unknown until the file is re-collected (an edit, `--list-tests`, the TTL). The error message says so. This is the one behaviour loss; 11.3(a) closes the bare-value part of it. |

So a name unmatched after the candidate session exists nowhere in that
repo, and the run can refuse **before any test runs**. Probe `p1` verifies
the mechanism: a `pytest.UsageError` raised from the plugin's
`pytest_collection_modifyitems` (after it recorded what was collected) exits
4, prints `ERROR: <did-you-mean>` on stderr, runs nothing, still fires
`pytest_sessionfinish` (so records and markers are complete), and writes an
empty JUnit file — which is why the temp-path JUnit move stays (11.3(d)).
What leaks that the held output hid: a conftest's own prints during
collection and pytest's "no tests ran" line. Acceptable; the "set aside
without a trace" paragraph in `docs/cli/test/selection.md` goes.

What this deletes: `_Sessions._by_name`'s two-phase `wider`/`rerun`,
`_RepoRun.rerun`/`settled`, `_whole_tree` and its `seeded` closure,
`OttoPlugin.abandoned`, `must_match` as an abandon trigger (it becomes the
UsageError trigger), `_SessionStdout` and `stream.discard()`, the
`unsearched` logic of `_refuse_unknown`; tests
`test_an_unknown_name_collects_the_whole_tree_once_and_suggests`,
`test_one_unknown_name_among_known_ones_runs_nothing` (rewritten: exits 4
before tests), `test_a_name_a_helper_lost_goes_to_the_whole_tree_and_suggests`
(rewritten: unknown at once), `test_a_session_set_aside_leaves_no_output_and_no_junit`
(deleted), `test_names_matching_nothing_are_reported` (rewritten for the
UsageError). Roughly −200 lines in `run.py`, −30 in `plugin.py`.

### 11.2 Question 2 — multi-repo: decide once, before any session

Today a name may live in a later repo, so a session that misses it cannot
error, and the driver abandons and widens. Replace this with one decision
made from the tables alone (stats, no pytest):

```
classify every searched repo                              # one stat per tracked path
holders[name]  = {repo | a FRESH record in repo matches name}
uncertain      = {repo | cold, or any changed/new file or candidate dir}
unplaced       = [name | holders[name] is empty]
if unplaced and uncertain:
    refresh every uncertain repo: one collect-only session over its
    non-fresh files and candidate dirs (a cold repo: the whole tree — this
    IS the seed); recompute holders
if unplaced still: raise UnknownSelectionError(did-you-mean over every table)   # nothing ran
for repo in configured order with holders or non-fresh files:
    one run session over its holder files ∪ its changed/new files ∪ candidate dirs,
    names=all, must_match = names whose holders are exactly {repo}
```

`must_match` is now only a race backstop (an edit that kept mtime and size
between `classify` and the import): it raises the UsageError in-session, so
even then no test runs in that repo. A name whose holders span two repos is
matched in the first; if a stat-invisible edit made both miss it, the error
comes after the first repo's tests ran — the same window every stat cache
has, and it is loud, not silent.

**Cost.** The pure warm path (every name held by a fresh record, nothing
non-fresh) is unchanged: one session per holding repo, no extra pytest —
this is the gated `test_repo` state. The "add a test, run it" loop pays one
collect-only session over the changed file before the run session (in
process: ≈480 fixed + ≈100 per file, §4; ≈0.1 s locally), instead of
today's single stage-A session — because the new name is held by no fresh
record until the file is read. A cold first run pays the whole-tree
collect-only seed and then the run session (the tree imported twice,
≈+1,500 file ops once), instead of §9.1's one session.

**Optional shortcut (≈10 lines), keeps §9.1's "the run's own session is the
seed":** when exactly one repo is uncertain and every unplaced name can only
be there, skip its refresh and run its candidate session directly with
`must_match = unplaced`; the UsageError still fires before its tests. This
restores today's single session for the single-repo workspace (the common
case) at the price of a second rule. I recommend landing the uniform rule
first and taking the shortcut only if the cold-run or add-a-test numbers
matter to the owner; the gated surface does not move either way.

### 11.3 More general checks

**(a) One import observer instead of the function/module branches of the
namespace walk — keep the class-MRO walk.** Probe `p2` installs a
`builtins.__import__` wrapper for the collection phase and, for every
import statement executed by a file under the test dirs (`globals["__file__"]`
— no frame inspection, no `sys.modules` read, so the
`no-branching-on-import-state` rule is untouched), records the returned
module's `vars(module).get("__file__")` and, for each `fromlist` name that
is itself a module in `vars(module)`, that file too. Measured on a scratch
repo: `from cfg import FLAG` → `cfg.py`; `from helpers import *` →
`helpers.py`; `from pkg import sub` and `import pkg.sub` → both files; a
module already cached because another test file imported it first is still
observed (test_b → `helpers.py`). Not observed: `importlib.import_module`
(bypasses `__import__`), and the base of a base reached through a cached
lib — `test_a` imports `deep.Mid`, whose base lives in `helpers.py`, and the
observer attributes `helpers.py` to `deep.py`, not to the test file. The
class-MRO walk covers exactly that (S1 deep/diamond MRO, S2, A, B, C, D, E),
so it stays. What goes: `_value_sources`'s function and module branches and
`_namespace_sources`'s reason to exist beyond classes (≈45 lines); what
comes: the observer, installed in the existing `pytest_load_initial_conftests`
wrapper (before the first conftest imports anything) and removed in
`pytest_collection_finish` under `try/finally` (≈45 lines). Net lines ≈ 0;
the rule set shrinks from five kinds (item function, item class MRO,
namespace classes, namespace functions, namespace modules) to two (what a
file imports; the MRO of every class in it). **It closes the bare-imported-value
hole** (F's `from cfg import FLAG`, L) and the sourceless star-import hole
(the `.pyc` is the module's `__file__`). New documented hole:
`importlib.import_module(...)` at module level, and a bare value two imports
away (`cfg` does `from cfg2 import X`; only `cfg.py` is tracked). New risk:
a global `builtins.__import__` swap for the length of collection — restored
in `finally`, nested sessions install and remove their own, a conftest that
wraps `__import__` itself composes (ours wraps whatever is installed at
session start). Covers: F, L, S3, S4, G, G2, H (the observer touches no
value: only `vars(module)`), plus P1/S1/S2/S6/S7/S9/A–E through the kept MRO
walk. Own task (11.4); not needed for 13b.5–13b.7.

**(b) No `__pycache__` beside the tests: delete the settle.** The settle,
its race (13b.4 fix-1 Critical 1) and `_entries` exist only because a first
import writes `__pycache__` next to the file and moves the directory's
mtime after its stat. Set `sys.pycache_prefix` for every otto session (run
and collect-only) to `$OTTO_HOME/<workspace>/pycache` unless the user
already exports `PYTHONPYCACHEPREFIX`, and pass `-o cache_dir=<xdir>/.pytest_cache`
so pytest-randomly's cache write (the other in-repo write the plugin works
around in `_start_directory`) leaves the rootdir alone. pytest's assertion
rewriter honours `sys.pycache_prefix` (§2), so nothing otto runs ever
creates an entry in a test directory, and a directory's stat means "a file
appeared, went away or was renamed" without exception. Deletes
`pytest_sessionfinish`'s settle, `_may_have_gained_bytecode`,
`_PYTEST_LEFTOVERS`, `_entries`, the `collect_only` `dont_write_bytecode`
toggle in `_run_pytest_session` (a collect-only pass may now write bytecode
— to the prefix — which makes the next import cheaper), the "last moment
before listing" re-stat in `_start_directory` (`classify`'s stat suffices),
and the settle tests (`test_a_first_run_writing_bytecode_leaves_no_directory_moved`,
the fix-1 race regression). ≈−90 lines, and the fix-1 Critical becomes a
deletion. Covers: the settle race (a file created between `classify` and
the listing is inside the pre-listing stat → collected; one created after
the listing moves the stat → next run), `test_a_file_saved_in_a_listed_directory_during_the_session_is_not_vouched_for`
still holds. New risk: bytecode for test files accumulates under
`OTTO_HOME` (`otto cache clear` removes the directory; one tree per
workspace); a user's `cache_dir` ini is overridden for otto's sessions
(pytest's `--lf`/`--ff` are not otto features). Another tool writing
`__pycache__` into the repo (plain `pytest`) costs one full collection of
that directory on the next otto run, then it is re-stamped — no
correctness effect. Lands in the in-flight 13b.4 fix round: replace the
settle rather than fix it.

**(c) One collection path.** `_collect_table` (`run.py:1188`) and
`_Sessions._session_outcome` + `_record` both run `_run_pytest_session`
and call `updated_table` + `write_tables`; `ensure_table(seed=)` and
`_whole_tree`'s closure exist to let the run's session be the seed. With
11.1/11.2 the driver is a loop, and one function serves every caller:

```python
def collect(repo, table, classification, *, candidates, candidate_dirs, names=(), must_match=(), run=False) -> tuple[_SessionOutcome, RepoTable | None]
```

`ensure_table` keeps its name and cold rule but loses `seed`/`Seed`/
`TableState.seeded`: a cold reader calls `collect(..., candidates=None,
run=False)`; the run calls `collect(..., candidates=None, run=True)` and
writes the result with `whole_tree=True` — the same merge. `refresh_table`,
the child, 13b.5's listings and dry run all call `collect(run=False)`.
Deletes `_collect_table`, `_whole_tree`, `_session_outcome(write=)`, the
`Seed` type and `seeded` bookkeeping (≈−110 lines in `run.py`,
≈−25 in `collected_tests.py`); `updated_table`/`_merged_dirs` are unchanged
(they already are the one merge).

**(d) What 11.1 removes from the output machinery.** `_SessionStdout`
(held/discarded output, 57 lines) goes entirely: no session is set aside any
more, and a collect-only session's silence is `-q` plus the terminal
reporter's `report_collect` override the plugin already has. The temp-path
JUnit move stays (≈10 lines): a UsageError session still writes an empty
JUnit (probe `p1`) and must not replace an earlier results file
(`test_a_typo_leaves_an_existing_results_file_alone`). The pre-clean at the
commit point (`_before_tests`, ≈25 lines) stays: it is what keeps a typo, a
`-m` that excludes everything and a session that collected nothing from
touching a host, and 11.2's pre-session refusal makes the typo case
unreachable but not the other two. The one-seed-per-run logging stays (it
is 6 lines and reproduces multi-repo runs).

**Others looked at and left alone.** `_merged_dirs` (34 lines) already is
the one rule for directories once (b) removes the settle. The child's
`SIGALRM` deadline (45 lines) is what bounds a hung init module in a
detached process; a `subprocess.run(timeout=)` in the parent cannot bound a
detached child. `_evict_repo_modules` stays for as long as two sessions can
run in one process (11.2's refresh + run).

### 11.4 Edge-case coverage of the proposals

| Probe | What it checks | 11.1/11.2 | (a) observer | (b) prefix | (c) one path |
| --- | --- | --- | --- | --- | --- |
| P1 | base edited in another test module | deps → changed → candidate | MRO walk kept | — | same merge |
| P2 | static seed hides an inherited test | gone with the `ast` seed | — | — | — |
| S1 | deep + diamond MRO | candidate via deps | **MRO walk kept for this** | — | — |
| S2 | derived class with zero items gains its first test | deps from namespace classes | kept (classes) | — | — |
| S3 / S4 | star-import of a helper with / without tests | deps | observer records the module | — | — |
| S6 | base in a conftest | conftest changed → subtree | observer: conftest imports are attributed to the conftest; MRO covers the class | — | — |
| S7 | editable / PYTHONPATH lib | `_tracked_dependency` unchanged | same scope rule on the observed file | — | — |
| S9 | dependency first seen this run | `None` stat → changed next run; unchanged | same stamping | — | — |
| A / B | `type()` factory, class made in a function | MRO walk (kept) | kept | — | — |
| C / D | namespace-package base, symlinked lib | MRO walk; paths as pytest saw them | observed file is `__file__` as imported | — | — |
| E | `__test__` toggled on a base | MRO walk | kept | — | — |
| F | `import cfg` gates a definition | module in namespace | **observer, and `from cfg import FLAG` too** | — | — |
| G / G2 | sourceless `.pyc` dependency | `_python_file` accepts `.pyc` | the `.pyc` is the observed `__file__` | — | — |
| H | hostile global (`__class__` raises, lazy proxy) | `type()`-only walk kept | observer never touches a value (`vars()` only) | — | — |
| L | `--list-tests` refreshes the bare-value hole | still true (whole tree) | the hole itself closes | — | — |
| settle race | a file created between `classify` and pytest's listing | — | — | **no settle, no race** | — |
| unknown name / typo | error before any test | **UsageError in-session or pre-session refusal** | — | — | — |
| broken unrelated file | asked-for tests run, exit 1 | unchanged (`--continue-on-collection-errors`) | — | — | — |
| `-m` excludes the name | "no tests matched", no pre-clean | unchanged (`_before_tests`) | — | — | — |
| multi-repo split names | each runs where it lives; unknown runs nothing | **pre-session decision** | — | — | — |

Ranked by lines removed against risk:

| # | Proposal | Removes | Adds | Risk | Where |
| --- | --- | ---: | ---: | --- | --- |
| 1 | 11.1 + 11.2 + (d): decide before running, no stage B / abandon / held output | ≈−290 (`run.py`), ≈−30 (`plugin.py`), 4 tests | ≈+60 (decision function, UsageError) | low: the behaviour loss is the holes' inconsistent second chance; multi-repo keeps "nothing runs before every name is placed" | new task 13b.4b |
| 2 | (c) one collection path | ≈−135 | ≈+30 | low: mechanical | 13b.4b |
| 3 | (b) pycache prefix + `cache_dir`, delete the settle | ≈−90, 2 tests | ≈+8 | low; bytecode moves under `OTTO_HOME` | the in-flight 13b.4 fix |
| 4 | (a) import observer | ≈−45 | ≈+45 | medium: a global `__import__` swap during collection | own task after 13b.6, optional |

Net for 1–3: `run.py` ≈1,478 → ≈1,050, `plugin.py` ≈1,274 → ≈1,150,
`collected_tests.py` ≈1,100 → ≈1,075, before 13b.5/13b.6's own deletions.

### 11.5 Recommended task plan

- **13b.4 fix round (in flight):** land (b) instead of fixing the settle:
  `sys.pycache_prefix` under `OTTO_HOME` (honouring `PYTHONPYCACHEPREFIX`)
  and `-o cache_dir=` for every session; delete the settle and its tests;
  keep the pre-read module/conftest stats. Test strategy: a first run writes
  no entry under any test directory (`os.scandir` before/after, and the
  stored dir stats equal the pre-session ones); the fix-1 race probe passes
  with the settle gone; `otto cache clear` removes the bytecode tree; a
  session's rewritten `.pyc` lands under the prefix (`_pytest` rewrite name
  pattern).
- **13b.4b (new, before 13b.5): decide before running; one collection
  path.** 11.1, 11.2 (uniform rule; the shortcut only if the owner asks) and
  (c). Tests: rewrite the four named in 11.1; the pre-session decision unit
  tests on synthetic tables (held, unplaced with/without uncertain repos,
  split across repos, one repo cold); the add-a-test loop runs exactly two
  `pytest.main` calls (collect-only then run) and the pure warm loop one; a
  typo exits before any test with did-you-mean naming a broken file's
  error; multi-repo: an unknown name runs nothing in any repo; the budget
  `test_repo` warm number unchanged (it is the pure warm path).
- **13b.5, 13b.6** as briefed, on top of the unified `collect(run=False)`;
  13b.5's listings and dry run become one-liners over it.
- **13b.6b (optional): the import observer** (a). Tests: the `p2` matrix as
  pytester cases (bare value, star, `from pkg import sub`, cached module,
  `importlib.import_module` documented as not seen, nested sessions
  restore `__import__`, a conftest that wraps `__import__` still works);
  the run-level F test extended to `from cfg import FLAG`; docs' hole list
  shrinks to non-Python data, dynamic imports and second-hand bare values.
- **13b.7 docs:** the "set aside" paragraph and the stage-B sentence go;
  "What the cache can't follow" says an unknown name from a hole is an
  error with `--list-tests` as the remedy; the bytecode location joins
  `startup-performance.md`'s `PYTHONPYCACHEPREFIX` section.

## 12. Owner decisions on the simplification review (2026-09-28, approved)

1. Stage B and the abandonable first pass go (§11.1): an unmatched name after the pre-session decision and the
   run's own pruned session is a did-you-mean error before any test runs; a whole-tree session happens only on a
   cold table.
2. Multi-repo decides once, before any pytest session (§11.2), WITH the single-uncertain-repo shortcut: when exactly
   one repo is uncertain, its refresh folds into its run session (one session), so the single-repo "add a test, run
   it" loop stays one `pytest.main` call.
3. (b): every otto-run pytest session (runs, collect-only seeds/refreshes, the child) writes bytecode under
   `sys.pycache_prefix` in `OTTO_HOME` (honouring an existing `PYTHONPYCACHEPREFIX`) and uses `-o cache_dir=` under
   `OTTO_HOME`; the `__pycache__` settle and its tests are deleted.
4. (c): one `collect()` path replaces `_collect_table` / `_session_outcome` / `ensure_table(seed=)` plumbing.
5. The import observer (a) is NOT done; the bare-imported-value hole stays documented.

## 13. Owner decisions on the TAB's cost (2026-09-28, approved)

1. A warm test-name TAB is O(1) in corpus size: a few hundred file ops at most, since each stat is a round trip on NFS.
   The shim never stats a file, directory or dependency the table tracks. It reads the table, runs the env check
   (O(1) per repo: the pytest configs, the settings file, site-packages, and the python and `sys.prefix` compared by
   value), reads the check marker, and answers from the last-known names. This supersedes the per-TAB stat pass in
   §9.2/§9.3 and the 60 s tests-marker window.
2. The stat pass moves into the detached collect child. A TAB spawns the child when the check marker is older than the
   **check window, 600 s**, and no lock or cooldown applies. The child classifies: if nothing moved, it neither
   re-collects nor rewrites the table; otherwise it re-collects what moved and drops deleted files' records.
3. An added, edited or deleted test reaches TAB one check late: up to the window plus one TAB. This is accepted in the
   service of speed ("mostly accurate in most cases"). A run always classifies synchronously before it runs, so a
   stale TAB never hides a test from a run.
4. Every session that writes the table touches the check marker: an `otto test` run, an unfiltered `--list-tests`, and
   the child. So a TAB just after a run spawns nothing.
5. The 24 h TTL is unchanged. `generated_at` moves only on a whole-tree collection (cold, TTL lapsed, a pytest config
   or the env changed, or the cache cleared), whatever triggered it. A run that re-collects only changed files never
   resets it: the daily whole-tree collection is the safety net for the holes in §9.5.
