# pytest-randomly: adopt in otto's own suite, and ship it to otto users

**Date:** 2026-08-09
**Status:** dev-group dependency added (`pytest-randomly>=4.1.0`, `[dependency-groups].dev`); nothing else done
**Decision (Chris, 2026-08-09):** it becomes a **real dependency**, not an optional extra — an otto user must
not have to install anything to get it. Raised the extra-vs-runtime question and it was answered: no extra.
**For:** its own workstream. Order-randomization is a suite-wide behavior change; it does not belong inside
the fd-watermark/guard work that surfaced it.

---

## 1. Why

Random ordering exposes the assumption a test suite never states: that tests run in file order. otto's suite
is 5700+ tests with heavy shared state (registries, contexts, loop trackers, monkeypatched globals), and the
audit has already found several leaks of exactly that shape by hand. Randomization finds them mechanically.

It is also a genuine feature for otto's users. otto orchestrates lab/hardware suites through `otto test`;
those suites accumulate the same order coupling, and their authors have less test-infra machinery than we do.

## 2. What is already true

- `pytest-randomly==4.1.0` is installed and in the `dev` group. **It auto-activates on install** — every bare
  `pytest` and every gate in this repo is now running shuffled.
- `pytest`, `pytest-asyncio`, `pytest-timeout` are already **runtime** deps of otto (see the comment at
  `pyproject.toml:47`), so there is precedent and a stated test for what belongs there: otto cannot interpret
  user test files without them. pytest-randomly is a different case — it changes behavior rather than enabling
  interpretation — but the decision above is to ship it anyway, unconditionally.

## 2b. First two seeds, and what they found

The full gate was run shuffled twice. Seed `1828725891`: green. Seed `424242`: **two failures**, both
fixed in the same commit as this file.

- `tests/unit/suite/test_otto_suite.py::TestOttoTestDir::test_test_dir_created_per_test` — a REAL
  order bug, ~50% under shuffle. The inner pytest run this test spawns is ordered independently, and
  the generated inner module had `test_alpha` do `write_text` while `test_beta` appended, so
  beta-first truncated beta's line. Made both append and the assertions set-shaped, matching the
  neighbouring `test_parametrized_names_sanitized`, which already had the right shape. Green over six
  seeds after.
- `tests/unit/test_env_hermeticity.py::test_bootstrap_state_cannot_leak_between_tests` — not a repo
  bug in the default configuration, but a trap for **item 6 below**. It spawns a child with
  `-p no:randomly`, which unregisters `--randomly-seed`; an ambient
  `PYTEST_ADDOPTS=--randomly-seed=N` is inherited by that child, which then exits 4 on "unrecognized
  arguments" and gets reported as a bootstrap leak. Fixed by clearing `PYTEST_ADDOPTS` for the child,
  which is what the experiment wanted anyway.

**Generalise both:** every in-process or subprocess pytest invocation in the suite is now a second,
independently-ordered run that also inherits ambient addopts. That is the same hazard already
recorded for `--cov`/`-n auto` hijacking an in-process `Config`. Sweeping those call sites is item 7.

## 3. Work items

1. **Triage otto's own suite under shuffle.** Run each lane repeatedly with different seeds and fix what
   falls out. Expect order coupling in: registry/context fixtures (`tests/conftest.py` has ~12 autouse
   restore fixtures), the monitor DB lanes, anything asserting on accumulated state. Record the seed in
   every failure report — `-p randomly --randomly-seed=N` reproduces exactly.
2. ~~**Decide the interaction with `serial_timing` and xdist grouping.**~~ **Checked 2026-08-09.**
   pytest-randomly declares `pytest_collection_modifyitems` `tryfirst=True`, so it shuffles BEFORE the
   root conftest's front-load sort; that sort is stable on a 0/1 key, so heavy-`xdist_group` dispatch
   order survives and the shuffle is preserved within each tier. `serial_timing` is marker-based
   deselection and does not depend on order. Still worth a pin so the property cannot regress silently.
3. **Move it to a runtime dependency** in `[project].dependencies`, with a comment in the same style as the
   pytest/pytest-asyncio/pytest-timeout block explaining the choice.
4. **Decide otto's default for user suites, and document it.** Shipping the plugin means every `otto test`
   suite starts shuffling. That is the intent, but hardware suites are where ordering is most likely to be
   load-bearing (provision → configure → verify chains, per-device serialization, expensive module-scoped
   setup). At minimum: document `-p no:randomly` and `--randomly-dont-reorganize`, and say plainly in the
   `otto test` docs that ordering is randomized by default and how to pin a seed when reproducing a failure.
   Worth considering whether `otto test` should surface the seed in its own output on failure.
5. **Check the reseeding side effect.** pytest-randomly reseeds `random`, `numpy.random` and Faker before
   every test. Anything in otto or a user suite that seeds its own RNG for reproducible payloads will change
   behavior. Grep otto for `random.seed` and note it in the docs.
6. **Gate wiring.** Decide whether the gates run shuffled (they should, or the plugin buys nothing) and
   whether nightly runs multiple seeds. A fixed seed in CI defeats the purpose; a free seed makes failures
   non-reproducible without reading the seed out of the report — the report line is the mitigation.
   Note that the natural implementation, `PYTEST_ADDOPTS=--randomly-seed=N`, is precisely what broke a
   test above; do item 7 first.
7. **Sweep the nested pytest invocations.** Every test that runs pytest inside itself — in-process via
   `pytest.main`/`Config`, or as a subprocess — is a second run with its own ordering and its own
   inherited addopts. Two of them bit on the first two seeds. Give each one an explicit, hermetic
   invocation: clear `PYTEST_ADDOPTS` for children, and decide per site whether the inner run should be
   shuffled (it is a real ordering surface) or pinned (it is a fixture, and its order is not the
   subject). Start from `grep -rl "pytest.main\|-m., .pytest\|_run_inner_pytest" tests/`.

## 4. Risks worth stating up front

- **Blast radius.** This will make gates red before it makes them green. Land the triage before the runtime
  dependency, or otto users inherit our unfinished work.
- **Auto-activation is the whole hazard.** There is no "installed but off" state; opting out is a flag the
  user has to know about. That is exactly why item 4 is not optional.
- **Interaction with the wall-clock-bound class.** Shuffling changes which tests are neighbours, which
  changes contention, which is the documented root of the `serial_timing` roster. Expect new sightings and
  treat them as the known class, not as new bugs.
