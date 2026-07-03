# `--tests` completion: cache of *collected* test names (follow-up)

## Context / current state

`otto test --tests` tab completion currently uses a **static `ast` scan**
(`collect_test_names` in `src/otto/configmodule/completion_cache.py`): it
reads `def test_*` / `Test*` methods straight from the source files, never
importing or collecting. Fast, safe (no user code at tab time), and it covers
every statically-defined name — but by design it **cannot** see names that
only exist after a real pytest collection:

- parametrized-only ids (`test_x[case-a]` — the base `test_x` IS offered, but
  not the per-parametrization ids),
- dynamically generated tests (`pytest_generate_tests`, conftest fixtures).

The honest boundary is documented (guide/test.md, guide/cli-reference.md,
architecture test + registries pages), pointing users to `otto test
--list-tests` for the fully-expanded list. Names are cached under the
`tests` key of the completion cache (schema v9), with a live static-scan
fallback. `_tests_completer` lives in `src/otto/cli/test.py`.

## The idea (from Chris)

Build a cache of **collected** test names (real pytest collection, so
parametrization + dynamic tests are included). On a cold collected-cache, the
completer either offers nothing OR runs collection once and caches it —
Chris's lean: **collect-and-cache on first miss**. "Slow on first attempt is
better than no completion at all."

## Design considerations (decide before implementing)

Real collection is `Repo.collect_tests()` (`src/otto/configmodule/repo.py:265`)
— a full `pytest.main()` collection pass. It (a) imports/executes user test
modules and conftests, (b) can take seconds, (c) has event-loop-cleanup
hazards (see the `collect_tests` body comment re pytest-asyncio self-pipe).
That collides with completion's standing rule — *never run user code at tab
time, never traceback into the shell, be low-latency*. So the open question
is **where the collection cost is paid**:

- **Option A — collect at tab time on cold cache (Chris's literal proposal).**
  First TAB triggers `collect_tests`, caches node ids, returns them; later TABs
  are fast. Must: hard-timeout the collection (e.g. a few seconds) and bail to
  the static scan / empty on timeout; fully swallow all output/exceptions so
  the shell never sees a traceback; guard against re-entrancy. Risk: a first
  TAB that hangs for seconds, and running arbitrary test-import side effects
  from a keystroke.

- **Option B — warm the collected cache as a side effect of real `otto test`
  runs (recommended to evaluate first).** Every real `otto test` /
  `--list-tests` / selection run already collects; capture those node ids and
  write them to the cache then. Completion stays pure-read (no user code at tab
  time), and after the first real test run the completion is fully accurate.
  Cold cache falls back to today's static scan (never empty). This keeps the
  architecture's invariant intact and still delivers collected-accuracy — the
  cost is paid by a command the user ran deliberately, not by a keystroke.

- **Option C — explicit warm command / flag** (`otto test --warm-completion`
  or fold into `--clear-autocomplete-cache`'s counterpart). Cheap, explicit,
  no surprise latency; requires the user to know about it.

A likely-good answer is **B as the primary mechanism + A as an opt-in**
(env var or setting) for users who want collect-on-first-TAB and accept the
latency. Keep the static scan as the always-available floor so completion is
never empty.

## Touchpoints

- `src/otto/configmodule/completion_cache.py` — cache schema (currently v9;
  a new *collected* field likely means v10), `read_cache`/`write_cache`,
  `collect_test_names` (static floor stays). `CACHE_TTL_SECONDS` (24h) and the
  fingerprint logic (test-file mtime/size) govern staleness — a collected
  cache wants the same fingerprint keying so edits invalidate it.
- `src/otto/cli/test.py` — `_tests_completer` (prefer collected → static →
  live), and the real run paths (`run_selection` / `run_suite` /
  `--list-tests`) as the Option-B warm points; `Repo.collect_tests` +
  `CollectedTest` (`repo.py:105`) give the node ids.
- `src/otto/cli/main.py` — cache writer call site (slow path) if any
  collected data is written there (careful: the slow path runs before every
  real command — do NOT add a collection pass there, that's the trap noted
  during the static-scan implementation).

## Definition of done

- `--tests` completion includes parametrized / dynamic ids after the cache is
  warm, via whichever mechanism is chosen.
- Completion never runs user code at tab time *unless* the user opted into
  Option A; and even then it is timeout-bounded and never tracebacks into the
  shell.
- Cold cache still completes (static scan floor), never empty by surprise.
- Docs updated: the "honest boundary" prose in guide/test.md +
  guide/cli-reference.md + architecture test/registries pages should change
  from "needs `--list-tests`" to describe the new warm-cache behavior.
- Tests: cache round-trip for the collected field; completer precedence
  (collected → static → live); the warm path writes the cache.
