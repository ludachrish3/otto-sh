# Extend declared-harness-bounds Part A to per-test `@pytest.mark.timeout(N)`

**Origin.** `tests/unit/test_declared_harness_bounds.py` (commit `61512fa7`)
gates four lane-level runaway guards and says, in its own words, that it does
not reach per-test marks:

> None of the four reaches a per-test `@pytest.mark.timeout(N)`, of which the
> suite holds ~54, nine below this module's own Python floor. Extending Part A
> to those is real work with real dispositions, and is deliberately not done
> here (review R2).

Nightly **#305** is the first cost of that gap. `test_exec_pool_high_fanout`
carried `timeout(10)` against a measured cost of 0.05 s and was killed at 10 s
when the CI runner froze for 12.63 s — a runaway guard deciding an outcome,
which is the one thing Part A exists to say a runaway guard must never do. The
fix (`ae020e14`) raised that file's seven sub-floor guards to the 60 s floor,
but it fixed one file by hand; nothing stops the next one.

Every number below was measured against **`ae020e14`** by an AST census
(decorators *and* module-level `pytestmark`, literal *and* computed bounds,
plus marks living inside generated-test-source strings).

## The census, and why the "nine" matters

| class | count at `ae020e14` | in scope for a gate? |
|---|---|---|
| literal bound **≥ 60 s** | 35 | pass |
| literal bound **< 60 s** | 11 | **the question** |
| **computed** bound (`soak_timeout(...)`) | 6 | must not be read as a literal |
| inside a **generated test source** string | 5 | must never be counted |
| total in real code | 52 | |

The module's "~54, nine below" is **reproducible, and reproducing it is what
recovers the exclusion rules the extension needs.** Running the same census
against `61512fa7` gives 46 marks in real code (plus 5 in generated source) and
**16** below floor — not nine. The gap is exactly the three classes a human
counted out without writing down:

- 5 module-scope `pytestmark = pytest.mark.timeout(30|45)` in
  `tests/integration/host/` (a different AST shape, and arguably a lane bound
  rather than a per-test one),
- 1 in `tests/repo1/` — a **fixture project**, test *data*, not this suite,
- 1 `timeout(0.25)` in `tests/unit/suite/test_timeout_enforcement.py` — the
  **subject under test**, since `otto test` must honor the marker it is being
  asked to enforce.

`16 − 5 − 1 − 1 = 9`. So the deferred item was sized correctly, and the four
lines above are the dispositions review R2 meant. They are the deliverable, not
a preamble to it.

## What the extension must get right

**1. AST, never text.** `tests/unit/suite/test_retry_semantics.py` writes five
test files as string literals, carrying `@pytest.mark.timeout(1)` three times
plus one `timeout(3)` and one `timeout(1e300)`. A grep- or regex-based gate
flags all five; they are the subject under test and every one of them is
correct as written — the `1e300` is the "disable it" idiom, pinning that a
budget `setitimer` cannot represent falls through to an *unarmed* attempt
instead of surfacing `OverflowError`. An AST walk
over real code ignores them for free. (Note for whoever builds it: those strings
are indented, so `ast.parse` on them raises `IndentationError` unless dedented
— which is how the first cut of this census silently reported zero.)

**2. A computed bound is a declared bound.** The six tunnel-stability marks
read `timeout(soak_timeout(per_cycle=90.0, base=180.0))`; `soak_timeout`
returns `base + per_cycle * SOAK_CYCLES` (`tests/e2e/tunnel_stability/_harness.py:47`,
`SOAK_CYCLES` defaulting to 5), so the values are 420-900 s and the floor is
never the issue — and the `base` term alone clears it even at `CYCLES=0`. A gate demanding
a literal would either fail them or force the number to be inlined, which is
strictly worse. Rule: a non-literal bound satisfies the requirement — the
original gate's whole point is that a *human wrote the number*, and a named
helper with a docstring is the strongest possible evidence of that.

**3. The floor's rationale does NOT transfer, and the rule has to be restated.**
Part A's floors "exist solely to stop a declaration from restating the default
it replaced" — the risk was a lane silently inheriting vitest's invisible
5000 ms. A per-test mark is *always* explicitly written; there is no default to
restate. So "was it declared" is vacuous here, and the real question — the one
#305 answers — is whether the bound sits far enough above the test's cost that
machine noise cannot reach it. That is a different rule and the extension must
say so out loud, or it will read as the same rule and mislead.

Since a gate cannot know a test's runtime statically, the options are:

- **(a) flat floor + justified allowlist.** One sentence of coverage: "every
  per-test timeout mark in real suite code is ≥ 60 s, except these N, each with
  a written reason." Recommended.
- **(b) headroom against measured duration** — compare each mark to the test's
  JUnit `time` and require, say, 20x. Strictly more faithful, and it fails the
  repo's own bar: the coverage cannot be stated in one sentence, the data is
  environment-dependent, and a fast machine would license a tight bound that a
  slow one then fires on. **Not recommended**, recorded so it is not
  re-proposed.

The repo has already ruled on this shape once — the AST discriminator scanner
in `61512fa7` was built, worked, and was *deleted* because "a gate whose
coverage cannot be stated in one sentence is worse than no gate." (b) is that
same trade. (a) is the one that survives it.

## Dispositions for the 11 sub-floor marks

| # | mark | disposition |
|---|---|---|
| 1 | `test_lifecycle.py:503` `timeout(5)` | **raise to 60.** Its own docstring calls it "a backstop only — a regression that drops the deadline-arming would otherwise hang up to the module's 180s default". That argument is satisfied by any finite bound; 60 serves it and survives a 12 s freeze. |
| 2 | `test_lifecycle.py:546` `timeout(5)` | **raise to 60**, same reason. |
| 3 | `test_gate_fresh.py:408` `timeout(20)` | **raise to 60.** Spawns real subprocesses and relays interrupts; a hang is the failure mode, so the guard only ever fires on one. |
| 4 | `test_gate_fresh.py:456` `timeout(20)` | **raise to 60**, same reason. |
| 5-9 | `tests/integration/host/*` module-scope `pytestmark` 30/45 s | **raise to 60+, and decide whether module-scope is in scope at all.** These bound live lab-VM tests, where the standing rule is already "never kill a live-bed run at a tight timeout". Needs a call on whether `pytestmark` counts as a per-test bound or a lane bound. |
| 10 | `tests/repo1/tests/test_device.py:58` `timeout(30)` | **exclude by path.** A fixture project consumed as test data; its marks are not this suite's guards. |
| 11 | `test_timeout_enforcement.py:32` `timeout(0.25)` | **exclude, explicitly.** Subject under test — the tight value *is* the assertion. Needs a named, commented exemption, not a silent path skip. |

Items 1-4 are mechanical and could land in one commit today. Items 5-9 need one
ruling (module-scope in or out). Items 10-11 are the allowlist the gate ships
with, and each needs its reason written next to it — a bare path list is how
the exclusion quietly grows.

## Cost, honestly

The gate itself is small — one AST walk, one floor constant, one allowlist,
mirroring Part A's existing structure. The work is items 5-11: one design
ruling and two exemptions that have to be argued rather than asserted. That is
what review R2 meant by "real dispositions", and it is still true; this note
exists so the next person starts from the census and the rules above instead of
re-deriving them.

**Not urgent, but no longer theoretical.** The gap has produced one nightly.
After `ae020e14` the count by the module's own definition is **4** (items 1-4),
down from nine — 7 fixed by #305, 2 added since by `test_gate_fresh`. Which is
the argument for the gate: the number goes down when someone is looking and up
when nobody is.
