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

## From `fix(cli)!: an instruction must be async def`

- **`@cli_command` has the same hole and is not gated.** A sync
  `@cli_command` that calls `ctx.all_hosts()` registers every host into a
  scope that is never entered, so nothing sweeps them — the identical silent
  failure `@instruction` now rejects, and the guide's own canonical example
  (`docs/guide/extending-cli.md`) is a lab-touching `ping`. The line that
  actually carries the weight is not "instruction vs cli_command" but
  `lab_free`: `@cli_command(lab_free=True)` is a defensible sync exemption,
  a lab-bound one is not. Documented as a caveat for now; gating it needs a
  sweep of in-tree sync leaves first.

- **The invariant is enforced at the sugar, not the seam.**
  `INSTRUCTIONS.register(InstructionEntry(...))` and `@run_app.command()` both
  reach `otto run` without passing the check. Airtight enforcement would live
  in `InstructionEntry.__post_init__` or `make_registry_group.get_command`.

- **`raise TypeError` vs the OttoError convention.** `errors.py` says "every
  exception otto raises subclasses `OttoError`", but
  `tests/unit/test_error_base.py` sweeps class DEFINITIONS, not raise sites,
  and `src/` has ~40 bare stdlib raises. Either the prose should say "every
  exception otto DEFINES" or the sweep should grow a raise-site rule. Same
  decay pattern the churn review's P5 describes.

- **A `!` in a conventional-commit type is inert in the changelog.**
  `cliff.toml` maps `^fix` to "Fixed" with no breaking-change parser, so
  `fix(cli)!:` renders as an ordinary bullet. Either teach cliff the marker or
  stop implying the changelog will carry it.

- **`SupportsHostSummaries` conformance checks ids, not completeness.**
  `testing/conformance.py`'s `_expect_host_summaries_conform` only asserts the
  summarized ids are a subset of `load_lab`'s. Any field a completer starts
  depending on (the `hop` idea explored and dropped in the link-completion
  work would have been the first) can be silently absent from a third-party
  backend with the conformance suite still green.

- **`repo_host_summaries` has no timeout.** It catches every exception, so a
  custom backend that FAILS is contained — but one that HANGS hangs the TAB.
  Measured fallback cost for a non-`SupportsHostSummaries` backend is
  O(labs × hosts) host constructions (~18 ms for 200 hosts in one lab), since
  the fallback loads every lab.

## From `fix(completion): scope link completion to the lab`

- **Implicit links are unimpairable, and nothing says so where it would be
  read.** `implicit_links` builds endpoints with `interface=None`, which
  `endpoint_placements` refuses, and hop-less hosts edge to `local`, which
  `ensure_not_local_link` refuses. So `find_link` resolves ids that no
  command can act on. `otto link list` surfaces this as `impairable=False`,
  but `find_link`'s own docstring does not, and it cost a wrong turn here.
  Worth either a note on `find_link` or an `impairable` helper on `Link`.

- **A declared link between two interface-less hosts is offered but not
  impairable.** `_resolve_endpoint` leaves `interface=None` when a host
  declares no `interfaces` map, so `endpoint_placements` refuses it.
  Completion cannot see that without interface data, which `HostSummary`
  deliberately excludes. Rare (a declared link usually names interfaces), and
  the fix is a repository seam for links rather than a wider summary.

## Cross-cutting

- **`typer.main.get_command_name` does not strip leading dashes.** A function
  named `_foo` derives the command name `-foo`, not `foo`. Harmless for real
  instructions; it silently makes some name-derivation assertions in
  `tests/unit/cli/test_run.py` (~199, ~211) vacuous.
