# Doctest coverage hardening — design

**Date:** 2026-06-24
**Status:** Approved (brainstorming complete) — ready for implementation plan
**Author:** otto contributors (with Claude)

## Background

A doctest audit (this session) found that otto's executable-documentation
guarantee is much narrower than it appears. Whether an example is actually
*executed* in CI depends on subtle, easily-violated conditions, and several
real examples had already rotted silently — most visibly the
`startMonitor`→`start_monitor` snake_case rename, which left four doc examples
showing a method name that no longer exists, because nothing ran them. (Those
four, plus an invalid getting-started lab JSON, were fixed in a prior commit;
a follow-up converted ~8 load-bearing prose examples to executed `{doctest}`
blocks, taking the sphinx doctest count from 53 → 83.)

This spec addresses the remaining **structural** blind spots so the whole
*class* of "looks-tested-but-isn't" becomes impossible going forward.

### How execution actually works today

| Where the example lives | Executed? | By what |
|---|---|---|
| `` ```{doctest} `` MyST fence in `.md` | yes | sphinx doctest (`make doctest`) |
| Public source docstring `>>>`, not under `::` | yes | sphinx via autodoc |
| `` ```python `` fence in `.md` (no `>>>`) | no | nothing — and a glob can't help (no `>>>` to find) |
| Docstring `>>>` under an `Example::` literal block | **no** | nothing in the gate |
| Docstring `>>>` on a **private** (`_foo`) member | **no** | nothing in the gate |
| `# doctest: +SKIP` | no | explicitly skipped |

The apparent second safety net is illusory: pytest's `--doctest-modules` is in
`addopts`, but `testpaths = ["tests/unit", "tests/integration", "tests/e2e"]`,
so it never scans `src/otto`. Source-tree docstring doctests run in **neither**
sphinx (when private or under `::`) **nor** pytest. And `make doctest` (sphinx)
only runs in the full gate (`make docs`), not the per-task `make coverage`.

### Why not `--doctest-glob='*.md'`

The audit's shorthand "the `--doctest-glob` net" does not do what it sounds
like. A glob only executes blocks that already contain `>>>`. The unguarded
prose examples are plain `` ```python `` fences of bare statements
(`host = UnixHost(...)`) with no `>>>`, so a glob would not touch them; it would
only *double-run* the `{doctest}` blocks sphinx already runs — and fail, because
pytest lacks sphinx's `doctest_global_setup` (which defines `run()`,
`LocalHost`, etc.). Rejected.

## Goals

1. **Guard the source docstrings** that no gate executes today (private
   helpers, `::`-swallowed examples) by *executing* them.
2. **Make the markdown failure mode impossible to reintroduce** via a lint.
3. **Remove the configmodule `+SKIP` charade** — three "passing" doctests that
   only run `import re` / `import UnixHost`, while the real functions are
   skipped — replacing it with honest illustration plus genuine execution where
   feasible.
4. **Fix the remaining prose staleness** (deleted-class references).
5. Keep the **runnable-only** discipline established earlier: an example is
   either genuinely executed or a plainly-illustrative fence. No mocks, no
   artificial surface-assertions, no `+SKIP`-disguised-as-passing.

## Non-goals

- No `--doctest-glob='*.md'` / pytest markdown collection (see above).
- No mocking of infra (monitor server, host connects) to force end-to-end
  execution of infra-dependent examples — they stay honest illustrations.
- No exhaustive prose audit beyond the known stale-token class.
- No change to the per-task gate (`make coverage`); this work lands in the
  docs gate (`make docs`), which the full gate already runs.

## Design

### §1 — New src-docstring gate, folded into `make docs`

Add a `doctest-src` make target that executes every source-tree docstring
doctest with a **clean** pytest invocation (drops `--cov`, `-n auto`, and the
180s timeout from `addopts`; preserves the `doctest_optionflags`
`NORMALIZE_WHITESPACE`/`ELLIPSIS` and `filterwarnings = error`, which are
separate ini keys):

```make
doctest-src: ## Run docstring doctests in src/ (catches private + ::-literal examples)
	uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto
```

Wire it into the docs aggregate and the nox docs session:

- `Makefile`: `docs: docs-lint docs-html doctest doctest-src`
- `noxfile.py` `docs` session: add
  `session.run("pytest", "-p", "no:cacheprovider", "-o", "addopts=--doctest-modules", "src/otto")`

**Current state (verified):** this invocation is already green —
`9 passed, 1 skipped` — collecting `interact._strip_ansi`, `interact._LineBuffer`
(private), `suite.OttoSuite.expect` (`::`-swallowed), `monitor.parsers.human_readable`,
`utils.{Status,CommandStatus,split_on_commas}`, and the configmodule examples.
So this is a pure lock-in: it guards examples that pass today but nothing
executes, with no pre-existing failures to repair. `utils`/`parsers` docstrings
are run by both sphinx and pytest — harmless duplication.

### §2 — Markdown doctest lint, in `docs-lint`

A small standalone checker (`scripts/lint_markdown_doctests.py`) that fails if
a doctest-prompt line appears in a non-executed fence.

**Algorithm.** Walk `docs/**/*.md` (skip `superpowers/**` and `_build/**`).
Track fenced code blocks by their info string. Flag any line matching
`^\s*>>>(\s|$)` that is **inside a fence whose info string is not `{doctest}`**,
**or** that is outside any fence. Report `file:line` for each and exit non-zero.

**False-positive handling (verified against the current tree):**

- Anchor on **line-start** `>>>`. This excludes regex/string uses such as
  `await host.expect(r">>> ", timeout=5.0)` in
  `cookbook/sessions-and-repeats.md` (the `>>>` is a remote-REPL prompt pattern,
  mid-line) and prose mentions like "write a `>>>` example".
- **Escape hatch:** a `<!-- doctest-lint: ignore -->` comment on the line
  immediately preceding a fence exempts that fence. This is for *intentional
  pedagogy that cannot run* — specifically `contributing.md`'s "how to write a
  doctest" block, whose `>>> add(1, 2)` references a fictional function.
- **Bare-in-prose prompts** (a `>>>` line outside any fence — `contributing.md`
  currently has a couple) are always flagged: a doctest-looking line should live
  in a fence. Remediation for `contributing.md` is to **wrap its teaching
  snippets in a fence carrying the ignore comment**, so no bare prose prompts
  remain and the intentional-illustration intent is explicit. The escape hatch
  therefore only ever attaches to a fence, never to loose prose.

Wire into the lint step:

- `Makefile` `docs-lint`: add `uv run python scripts/lint_markdown_doctests.py docs/`
  after the `doc8` line.
- `noxfile.py` `docs` session: add the same call after `doc8`.

After the escape-hatch is applied to `contributing.md`, the lint passes on the
current tree.

### §3 — configmodule `+SKIP` honesty fix

The three `# doctest: +SKIP` examples in
`src/otto/configmodule/configmodule.py` (`all_hosts`, `do_for_all_hosts`,
`run_on_all_hosts`) make their docstrings register as "passing" tests while
verifying only `import re` / `import UnixHost`.

**Constraint:** a configmodule *docstring* example is now run by **both** sphinx
(autodoc) and the new §1 pytest run, so it must be self-contained under both
(no reliance on sphinx's `doctest_global_setup`).

**Plan:**

- In the docstrings, **remove the `+SKIP` examples** and demote each function's
  example to a clean `::`-literal illustration (shown, not executed, not
  skipped). No example masquerades as a passing test.
- Add **one real prose `{doctest}`** to `docs/guide/library-usage.md` (which
  already documents context + host selection) that builds a tiny **in-memory**
  `Lab`/`OttoContext` and exercises `all_hosts(pattern)` and `get_host(name)`
  for real — filtering and lookup only, **no connection**. `do_for_all_hosts`
  and `run_on_all_hosts` remain honest illustrations; they actually connect to
  hosts and cannot run without infra.

**Verified working** in-memory setup (no connect):

```python
import re
from otto.storage.factory import create_host_from_dict
from otto.configmodule.lab import Lab
from otto.context import OttoContext, set_context, reset_context
from otto.configmodule import all_hosts, get_host

hosts = [create_host_from_dict(s) for s in [
    {"ip": "10.0.0.11", "element": "carrot", "creds": {"admin": "x"}, "labs": ["veg"]},
    {"ip": "10.0.0.12", "element": "tomato", "creds": {"admin": "x"}, "labs": ["veg"]},
]]
lab = Lab(name="veg", hosts={h.id: h for h in hosts})
tok = set_context(OttoContext(lab=lab))
# all_hosts(re.compile("tomato")) -> [tomato]; get_host("carrot") -> carrot
reset_context(tok)
```

The prose `{doctest}` ends with `reset_context(...)` so the in-memory context
does not leak into other doctests in the same document.

### §4 — `::` source docstrings (polish)

Execution-guarded by §1, so no change is *required*. Light polish: de-`::` the
public `OttoSuite.expect` example (drop the `Example::` literal-block marker)
so it also renders as a real doctest and runs under sphinx. Private helpers
(`_strip_ansi`, `_LineBuffer`) stay as-is — autodoc will not render private
members regardless, and §1 already guards them.

### §5 — Prose staleness

- `docs/guide/repo-setup.md:105`: "the global `ConfigModule` is created" — the
  `ConfigModule` class was deleted (WS#1). Reword to the current `OttoContext` /
  `get_context()` model.
- A quick targeted grep across user-facing docs for the same stale-token class
  (deleted-class names, "dataclass" mislabels of now-pydantic models); fix any
  found. (The monitor.md "SnmpMetric dataclass" instance was already fixed
  during the prior conversion.)

### §6 — Remaining conversions (bounded, YAGNI)

A quick audit for any *clearly* load-bearing, side-effect-free prose
`` ```python `` fence still worth converting to `{doctest}`. Convert only
obvious wins; do not force conversions on infra-dependent or global-state-
mutating examples (`register_*`, host connects, sessions). May be empty.

## Verification

- `make docs` now runs `docs-lint` (doc8 + markdown doctest lint), `docs-html`
  (`sphinx -W`), `doctest` (sphinx `{doctest}`), and `doctest-src` (pytest
  src docstrings). All must pass.
- Report the before/after counts: sphinx doctests (currently 83) and the
  `doctest-src` tally (currently `9 passed, 1 skipped`; after §3, `all_hosts`/
  `get_host` no longer appear as fake configmodule tests, and the new
  library-usage `{doctest}` adds real selection coverage).
- `nox -s docs` green (the gate as CI runs it).

## Sequencing

Each step is independently committable:

1. **§1 + §2** — gate target + lint + wiring (infra; apply the contributing.md
   escape-hatch so the lint is green).
2. **§3** — configmodule honesty fix + library-usage `{doctest}`.
3. **§4 + §5 + §6** — polish: de-`::` expect, prose staleness, bounded
   conversions.

## Risks & mitigations

- **Double-run drift (sphinx vs pytest) for shared docstrings.** utils/parsers
  run under both; both use the same `NORMALIZE_WHITESPACE`+`ELLIPSIS` flags, so
  output expectations are identical. Low risk.
- **`filterwarnings = error` in the clean pytest run.** Verified the run is
  green with it preserved; if a future import emits a warning, the target can
  relax it narrowly. Documented in the target.
- **In-memory context leak between doctests.** Mitigated by the trailing
  `reset_context` in the §3 `{doctest}`.
- **Lint false positives.** Mitigated by line-start anchoring + the
  `<!-- doctest-lint: ignore -->` escape hatch; verified against the current
  tree (the only real hits are contributing.md's teaching block).

## Appendix — verified facts (this session)

- `pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto` →
  `9 passed, 1 skipped`, with `filterwarnings=error` preserved.
- In-memory `Lab(name=, hosts={id: host})` + `OttoContext` →
  `all_hosts(re.compile("tomato"))` yields `[tomato]`, `get_host("carrot")`
  yields the carrot host, no connection.
- Current markdown lint offenders are all in `contributing.md` (the doctest
  teaching block) once line-start anchoring excludes the
  `cookbook/sessions-and-repeats.md` `r">>> "` regex pattern.
