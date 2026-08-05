# Follow-ups from the churn-review "cheap items" wave (2026-08-04)

Surfaced by the per-item opus reviews. Each is out of scope for the cheap item
that found it — recorded here rather than folded in, so the scope of each
squash stays one thing.

## From `fix(completion): hash every test source the --tests scan can read`

- **`Repo.iter_test_files` is a third, narrower reader of the same tests dirs.**
  `config/repo.py:716` still does a non-recursive `glob("test_*.py")`, so a
  `Test*` OttoSuite defined in `tests/unit/test_foo.py` or in `foo_test.py` is
  never registered in `SUITES`. Pre-existing, and NOT a glob fix: that reader
  *imports* what it returns, so widening it changes which user modules otto
  execs at bootstrap. Needs a decision, and a test that a nested suite becomes
  runnable, before anything moves.

- **A repo that overrides pytest's `python_files` still goes stale.** Neither
  `collect_test_names` nor `compute_fingerprint` knows about a `python_files`
  setting, and no pytest config file (`pytest.ini`, `pyproject.toml`,
  `tox.ini`, `setup.cfg`) is hashed. Verified live: with
  `python_files = check_*.py test_*.py`, pytest collects
  `check_alt.py::test_alt_pattern` and editing that file never moves the
  digest — the same bug this commit fixed, one config line away. Closing it
  means reading the repo's pytest config on the completion fast path.

- **The walk descends into dot-directories.** `rglob` yields `.tox/`,
  `.venv/`, `.git/` contents where pytest's `norecursedirs` would not.
  Harmless today (otto's own tests/ has none) but it is the pathological cost
  case: a venv tree measured 83 ms warm, on a path that runs twice per TAB.
  Excluding them means a manual walk instead of `rglob`.

- **`_test_sources` can yield a directory.** A directory literally named
  `test_x.py` is matched, and `_hash_file` folds its mtime in, so unrelated
  writes inside it move the digest. No crash on either side (the scan's
  `read_text` raises `IsADirectoryError` ⊂ `OSError`, already caught). Cosmetic
  — costs a `stat` per candidate to filter.
