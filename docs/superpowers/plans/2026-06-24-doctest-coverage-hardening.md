# Doctest Coverage Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the doctest blind spots so the "looks-tested-but-isn't" failure mode becomes structurally impossible: execute source docstrings in CI, lint markdown against un-executed `>>>` examples, and end the configmodule `+SKIP` charade.

**Architecture:** Three independent pieces wired into the existing `make docs` gate — (1) a `pytest --doctest-modules src/otto` run that executes source docstrings (including private/`::`-swallowed ones), (2) a standalone Python linter that forbids `>>>` in non-`{doctest}` markdown fences, and (3) honesty edits to the configmodule docstrings plus one real in-memory `{doctest}`. No production code behavior changes; this is gate + docs work.

**Tech Stack:** Python 3.10+, pytest (`--doctest-modules`), sphinx doctest builder, MyST markdown, GNU Make, nox.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-24-doctest-coverage-hardening-design.md` — the authority; this plan implements it.
- **Commits:** Do **not** self-commit. The repo's `prepare-commit-msg` hook needs a TTY and mislabels agent commits as un-attributed. Each task's final step **stages** the changes and provides a **paste-able commit message** for the human to run. End commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Never probe the dev repo destructively.** Tests write only to `tmp_path`, never to paths inside the repo.
- **Runnable-only discipline:** an example is either genuinely executed or a plainly-illustrative fence. No mocks, no surface-assertions, no `+SKIP`-disguised-as-passing.
- **The clean doctest-src invocation is exactly:** `uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto` (drops `--cov`/`-n auto`/timeout from `addopts`; preserves the `doctest_optionflags` `NORMALIZE_WHITESPACE`/`ELLIPSIS` and `filterwarnings = error`, which are separate ini keys). Verified green today: `9 passed, 1 skipped`.
- **Lab data dirs/`_build`/`superpowers` are out of lint scope.** The linter skips any path containing a `_build` or `superpowers` path component.

---

## Task 1: Markdown doctest linter (TDD)

Create the linter that flags `>>>` doctest prompts living in fences sphinx will not execute. Pure, deterministic logic — full TDD.

**Files:**
- Create: `scripts/lint_markdown_doctests.py`
- Test: `tests/unit/test_markdown_doctest_lint.py`

**Interfaces:**
- Produces: `lint_file(path: pathlib.Path) -> list[tuple[int, str]]` — returns `(line_number, reason)` offenses for one markdown file. `main(argv: list[str]) -> int` — CLI entry, prints offenses, returns process exit code (0 clean, 1 offenses).
- Lint rule: a fence opened with **≥4 backticks** is a *display* fence and its contents are not linted. Inside a **3-backtick** fence whose info string is not `{doctest}`, any line matching `^\s*>>>(\s|$)` is an offense. A `>>>` line outside any fence is an offense. `<!-- doctest-lint: ignore -->` on the line immediately preceding a fence exempts that fence.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_markdown_doctest_lint.py`:

```python
import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "lint_markdown_doctests.py"


def _load_linter():
    spec = importlib.util.spec_from_file_location("lint_markdown_doctests", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = '''\
# Title

A clean executed example:

```{doctest}
>>> 1 + 1
2
```

An un-executed example that SHOULD be flagged:

```python
>>> dangerous()
None
```

Mid-line `>>>` is a remote prompt pattern, not a doctest prompt:

```python
await host.expect(r">>> ", timeout=5.0)
```

A four-backtick block that *displays* a doctest fence — not linted:

````markdown
```{doctest}
>>> from otto.utils import Status
>>> Status.Success
```
````

Intentional non-runnable pedagogy, exempted:

<!-- doctest-lint: ignore -->
```python
>>> add(1, 2)
3
```

A bare prompt loose in prose is also flagged:

>>> stray()
'''


def test_flags_only_unexecuted_prompts(tmp_path):
    linter = _load_linter()
    md = tmp_path / "sample.md"
    md.write_text(FIXTURE)
    offenses = linter.lint_file(md)
    # Assert on the offending line *content* (robust to fixture line-number
    # drift): only the un-executed ```python prompt and the bare-prose prompt
    # are flagged. The {doctest} block, the mid-line r">>> " regex, the
    # 4-backtick display block, and the ignore-exempted block are all clean.
    lines = FIXTURE.splitlines()
    flagged = {lines[n - 1].strip() for n, _ in offenses}
    assert flagged == {">>> dangerous()", ">>> stray()"}, offenses


def test_clean_file_has_no_offenses(tmp_path):
    linter = _load_linter()
    md = tmp_path / "clean.md"
    md.write_text("# Ok\n\n```{doctest}\n>>> 2 + 2\n4\n```\n")
    assert linter.lint_file(md) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_markdown_doctest_lint.py -v`
Expected: FAIL — `FileNotFoundError` / `ModuleNotFoundError` (the script does not exist yet).

- [ ] **Step 3: Write the linter**

Create `scripts/lint_markdown_doctests.py`:

```python
#!/usr/bin/env python3
"""Fail if a doctest prompt (``>>>``) appears where Sphinx will not execute it.

Sphinx's doctest builder only runs ```{doctest}``` fenced blocks. A ``>>>`` line
in any other fence (```python```, ```pycon```, …) renders as code but is never
executed, so such "examples" can silently drift from the real API. This linter
makes that pattern a hard error.

Rules:
  * A fence opened with >=4 backticks is a *display* fence (used to show fence
    syntax) and its contents are not linted.
  * Inside a 3-backtick fence whose info string is not ``{doctest}``, any line
    matching ``^\\s*>>>`` is an offense.
  * A ``>>>`` line outside any fence is an offense.
  * ``<!-- doctest-lint: ignore -->`` on the line immediately preceding a fence
    exempts that fence (for intentional, non-runnable pedagogy).

Usage: python scripts/lint_markdown_doctests.py docs/
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROMPT = re.compile(r"^\s*>>>(\s|$)")
FENCE = re.compile(r"^\s*(?P<ticks>`{3,})(?P<info>.*)$")
IGNORE = "<!-- doctest-lint: ignore -->"
SKIP_PARTS = ("_build", "superpowers")


def lint_file(path: Path) -> list[tuple[int, str]]:
    offenses: list[tuple[int, str]] = []
    open_ticks = 0          # length of the open fence, 0 if none
    info = ""               # info string of the open fence
    ignored = False         # the open fence is exempt
    pending_ignore = False  # the previous line was the ignore comment
    for n, line in enumerate(path.read_text().splitlines(), 1):
        m = FENCE.match(line)
        if open_ticks == 0:
            if m:
                open_ticks = len(m.group("ticks"))
                info = m.group("info").strip()
                ignored = pending_ignore
                pending_ignore = False
                continue
            pending_ignore = line.strip() == IGNORE
            if PROMPT.match(line):
                offenses.append((n, "doctest prompt outside any fence"))
        else:
            if m and len(m.group("ticks")) >= open_ticks and not m.group("info").strip():
                open_ticks = 0
                info = ""
                ignored = False
                continue
            if open_ticks == 3 and info != "{doctest}" and not ignored and PROMPT.match(line):
                offenses.append((n, f"doctest prompt in ```{info or '(plain)'} fence (not {{doctest}})"))
    return offenses


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("docs")]
    failures = 0
    for root in roots:
        for md in sorted(root.rglob("*.md")):
            if any(part in SKIP_PARTS for part in md.parts):
                continue
            for n, why in lint_file(md):
                print(f"{md}:{n}: {why}")
                failures += 1
    if failures:
        print(
            f"\n{failures} doctest-lint offense(s). Move runnable examples into "
            f"```{{doctest}}``` fences, or mark intentional non-runnable ones "
            f"with '{IGNORE}' on the line before the fence."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_markdown_doctest_lint.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Stage + hand off commit message**

```bash
git add scripts/lint_markdown_doctests.py tests/unit/test_markdown_doctest_lint.py
```
Paste-able message for the human:
```
test(docs): add markdown doctest linter

scripts/lint_markdown_doctests.py flags `>>>` prompts in fences Sphinx
won't execute (anything but ```{doctest}```), with correct variable-length
fence tracking (>=4-backtick display fences skipped) and a
<!-- doctest-lint: ignore --> escape hatch. Covered by unit tests.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 2: Apply the lint to the tree + wire into `docs-lint`

Make the existing tree pass the new linter, then wire it into the gate.

**Files:**
- Modify: `docs/contributing.md:353-354` (add ignore comment before the `add()` teaching fence)
- Modify: `Makefile` (the `docs-lint` target)
- Modify: `noxfile.py` (the `docs` session)

**Interfaces:**
- Consumes: `scripts/lint_markdown_doctests.py` from Task 1.

- [ ] **Step 1: Run the linter against the real tree to see current offenses**

Run: `uv run python scripts/lint_markdown_doctests.py docs/`
Expected: exactly one offense — `docs/contributing.md:358: doctest prompt in ```python fence (not {doctest})` (the `>>> add(1, 2)` teaching example). The `cookbook/sessions-and-repeats.md` `r">>> "` lines and the prose `` `>>>` `` mentions are correctly NOT flagged (mid-line); the `````markdown` demo block is NOT flagged (4-backtick display fence).

- [ ] **Step 2: Exempt the contributing.md teaching fence**

In `docs/contributing.md`, the "Doctest quick reference" section shows a deliberately-fictional `add()` doctest. Add the ignore comment immediately before its fence. Change:

```markdown
In Python source files (collected by pytest):

```python
def add(a: int, b: int) -> int:
```

to:

```markdown
In Python source files (collected by pytest):

<!-- doctest-lint: ignore -->
```python
def add(a: int, b: int) -> int:
```

- [ ] **Step 3: Verify the tree is now clean**

Run: `uv run python scripts/lint_markdown_doctests.py docs/`
Expected: no output, exit 0.

- [ ] **Step 4: Wire the linter into `docs-lint` (Make) and the `docs` nox session**

In `Makefile`, change the `docs-lint` recipe from:

```make
docs-lint: ## Fast RST structural lint (doc8) — catches title/underline desync without a full sphinx build
	uv run doc8 docs/
```

to:

```make
docs-lint: ## Fast doc lints — doc8 (RST structure) + markdown doctest-fence guard
	uv run doc8 docs/
	uv run python scripts/lint_markdown_doctests.py docs/
```

In `noxfile.py`, in the `docs` session, after the `session.run("doc8", "docs/")` line add:

```python
    session.run("python", "scripts/lint_markdown_doctests.py", "docs/")
```

- [ ] **Step 5: Verify the wired gate**

Run: `make docs-lint`
Expected: doc8 runs, then the markdown linter runs, both succeed (exit 0).

- [ ] **Step 6: Stage + hand off commit message**

```bash
git add docs/contributing.md Makefile noxfile.py
```
Paste-able message:
```
build(docs): enforce the markdown doctest-fence guard in docs-lint

Wire scripts/lint_markdown_doctests.py into `make docs-lint` and the nox
docs session, and exempt contributing.md's intentional `add()` teaching
example. A `>>>` example in a non-{doctest} markdown fence now fails CI.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 3: Source-docstring gate (`doctest-src`) wired into `make docs`

Add the pytest run that executes source-tree docstrings — the only thing that guards private (`_strip_ansi`, `_LineBuffer`) and `::`-swallowed (`OttoSuite.expect`) examples.

**Files:**
- Modify: `Makefile` (new `doctest-src` target; add it to `docs` and `.PHONY`)
- Modify: `noxfile.py` (the `docs` session)

- [ ] **Step 1: Confirm the clean invocation passes today**

Run: `uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto`
Expected: `9 passed, 1 skipped` — collecting `interact._LineBuffer`, `interact._strip_ansi`, `suite.OttoSuite.expect`, `monitor.parsers.human_readable`, `utils.{Status,CommandStatus,split_on_commas}`, and the configmodule examples (`all_hosts`, `do_for_all_hosts` pass on their import lines; `run_on_all_hosts` skips). This is the pre-existing-green baseline; Task 4 changes the configmodule items.

- [ ] **Step 2: Add the `doctest-src` Make target**

In `Makefile`, add after the existing `doctest:` target:

```make
doctest-src: ## Run docstring doctests in src/ (catches private + ::-literal examples Sphinx skips)
	uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto
```

Add `doctest-src` to the `docs` aggregate target — change:

```make
docs: docs-lint docs-html doctest ## Build HTML docs and run doctests
```

to:

```make
docs: docs-lint docs-html doctest doctest-src ## Build HTML docs and run Sphinx + src doctests
```

Add `doctest-src` to the `.PHONY` line at the top of the Makefile (the line beginning `.PHONY: help all ci ...`), alongside the existing `doctest` entry.

- [ ] **Step 3: Add the same run to the `docs` nox session**

In `noxfile.py`, in the `docs` session, after the existing
`session.run("sphinx-build", "-E", "-b", "doctest", "docs/", "docs/_build/doctest")`
line add:

```python
    session.run("pytest", "-p", "no:cacheprovider", "-o", "addopts=--doctest-modules", "src/otto")
```

- [ ] **Step 4: Verify the new target and the full docs gate**

Run: `make doctest-src`
Expected: `9 passed, 1 skipped`.

Run: `make docs`
Expected: `docs-lint` (doc8 + markdown lint), `docs-html` (`build succeeded`), `doctest` (`53`→ current `83 tests ... 0 failures`), and `doctest-src` (`9 passed, 1 skipped`) all succeed.

- [ ] **Step 5: Stage + hand off commit message**

```bash
git add Makefile noxfile.py
```
Paste-able message:
```
build(docs): execute src/ docstring doctests in the docs gate

New `make doctest-src` (pytest --doctest-modules src/otto, clean
invocation) folded into `make docs` and the nox docs session. This is the
only gate that runs private-member and ::-literal docstring examples
(_strip_ansi, _LineBuffer, OttoSuite.expect) — green today, locking them in.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 4: configmodule `+SKIP` honesty fix + realized `{doctest}`

Remove the three fake `+SKIP` docstring "tests"; demote them to honest `::` illustrations; add one real in-memory `{doctest}` to `library-usage.md` that exercises `all_hosts`/`get_host` without connecting.

**Files:**
- Modify: `src/otto/configmodule/configmodule.py` (the `Examples:` blocks of `all_hosts` ~157-160, `do_for_all_hosts` ~214-220, `run_on_all_hosts` ~279-280)
- Modify: `docs/guide/library-usage.md` (append a new section)

**Interfaces:**
- Consumes: `doctest-src` gate from Task 3 (proves the demotion removes the fake tests) and the markdown lint from Task 2 (the new `{doctest}` must be a real `{doctest}` fence).
- Verified working in-memory setup: `Lab(name=, hosts={h.id: h})` + `OttoContext(lab=...)` + `set_context` → `all_hosts(re.compile("tomato"))` yields the tomato host; `get_host("carrot")` yields the carrot host; no connection.

- [ ] **Step 1: Demote `all_hosts` example**

In `src/otto/configmodule/configmodule.py`, replace the `all_hosts` `Examples:` block:

```python
    Examples:
        >>> import re
        >>> # assuming hosts: carrot_seed, tomato_seed, pepper_seed
        >>> seeds = list(all_hosts(re.compile(r"tomato")))  # doctest: +SKIP
    """
```

with:

```python
    Examples:
        Filter the active lab's hosts by id pattern (see
        :doc:`/guide/library-usage` for a runnable, in-memory example)::

            import re
            seeds = list(all_hosts(re.compile(r"tomato")))
    """
```

- [ ] **Step 2: Demote `do_for_all_hosts` example**

Replace the `do_for_all_hosts` `Examples:` block:

```python
    Examples:
        >>> import re
        >>> from otto.host import UnixHost
        >>> results = await do_for_all_hosts(  # doctest: +SKIP
        ...     UnixHost.oneshot, "uname -a",
        ...     pattern=re.compile(r"router"),
        ... )
    """
```

with:

```python
    Examples:
        Call an unbound async method on every matching host::

            import re
            from otto.host import UnixHost
            results = await do_for_all_hosts(
                UnixHost.oneshot, "uname -a",
                pattern=re.compile(r"router"),
            )
    """
```

- [ ] **Step 3: Demote `run_on_all_hosts` example**

Replace the `run_on_all_hosts` `Examples:` block:

```python
    Examples:
        >>> results = await run_on_all_hosts("uname -a")  # doctest: +SKIP
    """
```

with:

```python
    Examples:
        Run a command on every matching host::

            results = await run_on_all_hosts("uname -a")
    """
```

- [ ] **Step 4: Verify the fake configmodule tests are gone**

Run: `uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto`
Expected: `7 passed` (no skips). The three configmodule items (`all_hosts`, `do_for_all_hosts`, `run_on_all_hosts`) no longer collect — only `interact._LineBuffer`, `interact._strip_ansi`, `suite.OttoSuite.expect`, `monitor.parsers.human_readable`, and `utils.{Status,CommandStatus,split_on_commas}` remain, all real.

- [ ] **Step 5: Add the realized in-memory `{doctest}` to library-usage.md**

Append to `docs/guide/library-usage.md` (after the final "Reservation checks…" paragraph):

````markdown
## In-memory labs (no lab file)

You do not need a `hosts.json` on disk. Build a `Lab` from host dicts, install
it as the active context, and the zero-argument selectors (`all_hosts`,
`get_host`) operate on it directly — useful for tests and ad-hoc scripts.
Selection touches no network, so this runs as-is:

```{doctest}
>>> import re
>>> from otto.storage.factory import create_host_from_dict
>>> from otto.configmodule.lab import Lab
>>> from otto.context import OttoContext, set_context, reset_context
>>> from otto.configmodule import all_hosts, get_host
>>> hosts = [create_host_from_dict(spec) for spec in [
...     {"ip": "10.0.0.11", "element": "carrot", "creds": {"admin": "x"}, "labs": ["veg"]},
...     {"ip": "10.0.0.12", "element": "tomato", "creds": {"admin": "x"}, "labs": ["veg"]},
... ]]
>>> lab = Lab(name="veg", hosts={h.id: h for h in hosts})
>>> token = set_context(OttoContext(lab=lab))
>>> [h.element for h in all_hosts(re.compile("tomato"))]
['tomato']
>>> get_host("carrot").element
'carrot'
>>> reset_context(token)
```

The trailing `reset_context` restores the prior active context — always pair it
with `set_context` (or use `otto.open_context`, which does both for you).
````

- [ ] **Step 6: Verify the new doctest runs and the gate is green**

Run: `make doctest` (sphinx)
Expected: `build succeeded`, total tests increased (was `83`), `0 failures`; `guide/library-usage` now reports its own passing tests.

Run: `make docs`
Expected: all four sub-steps green (`docs-lint`, `docs-html`, `doctest`, `doctest-src` now `7 passed`).

- [ ] **Step 7: Stage + hand off commit message**

```bash
git add src/otto/configmodule/configmodule.py docs/guide/library-usage.md
```
Paste-able message:
```
docs: end the configmodule +SKIP charade; add a real in-memory doctest

The all_hosts/do_for_all_hosts/run_on_all_hosts docstrings counted as
"passing" doctests while only running import statements (the real calls
were +SKIP). Demote those examples to honest :: illustrations, and add a
genuinely-executed {doctest} to guide/library-usage.md that builds an
in-memory Lab and exercises all_hosts/get_host with no connection.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 5: Polish — de-`::` `expect`, fix prose staleness

De-`::` the public `OttoSuite.expect` example so sphinx also renders+runs it, and fix the known stale prose.

**Files:**
- Modify: `src/otto/suite/suite.py` (the `expect` docstring, ~223-238)
- Modify: `docs/guide/repo-setup.md:105`

- [ ] **Step 1: De-`::` the `expect` doctest**

In `src/otto/suite/suite.py`, the second `Examples:` paragraph ends with `::`, which makes sphinx render the following `>>>` block as a non-executed literal block. Remove the `::` so it becomes a real doctest under sphinx (pytest already runs it). Change:

```python
            The failure report always includes the source location and
            caller locals.  When *msg* is provided it appears *in addition
            to* the auto-captured source context, never replacing it::

                >>> from unittest.mock import MagicMock
```

to (drop the `::`, add a blank line, dedent the `>>>` block to the `Examples:` body indent so it is a normal doctest block, not literal):

```python
            The failure report always includes the source location and
            caller locals.  When *msg* is provided it appears *in addition
            to* the auto-captured source context, never replacing it:

            >>> from unittest.mock import MagicMock
            >>> from otto.suite.suite import OttoSuite
            >>> suite = OttoSuite()
            >>> suite._expect_failures = []
            >>> suite.logger = MagicMock()
            >>> x = 42
            >>> suite.expect(x == 99, "math is broken")
            >>> report = suite._expect_failures[0]
            >>> "Message: math is broken" in report
            True
            >>> "x = 42" in report
            True
```

Keep the `.. note::` block that follows it unchanged.

- [ ] **Step 2: Verify expect runs under BOTH sphinx and pytest**

Run: `uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto -k expect -v`
Expected: `src/otto/suite/suite.py::otto.suite.suite.OttoSuite.expect PASSED`.

Run: `make doctest`
Expected: `build succeeded`; `api/suite/suite` now contributes passing tests (it contributed 0 before, because the `::` hid the example).

- [ ] **Step 3: Fix the `ConfigModule` prose**

In `docs/guide/repo-setup.md`, change line 105 from:

```markdown
5. **Config module creation** -- The global `ConfigModule` is created with
   the loaded repos and lab, making hosts available to all commands.
```

to:

```markdown
5. **Context creation** -- The global `OttoContext` is created with the
   loaded repos and lab and installed via `set_context()`, making hosts
   available to the zero-argument accessors (`get_host`, `all_hosts`) in
   all commands.
```

- [ ] **Step 4: Targeted stale-token sweep**

Run: `grep -rEn 'ConfigModule|\bdataclass\b' docs/guide docs/cookbook docs/getting-started.md docs/overview.md docs/index.rst | grep -v _build`
Expected: review each hit. `ConfigModule` should now return nothing. For any remaining "dataclass" describing a type that is actually a pydantic model (e.g. another `SnmpMetric`-style reference), reword to "model" / "fields". Fix any found; if none, no change. (The known monitor.md "dataclass" instance was already fixed in a prior commit.)

- [ ] **Step 5: Verify the full docs gate**

Run: `make docs`
Expected: all sub-steps green.

- [ ] **Step 6: Stage + hand off commit message**

```bash
git add src/otto/suite/suite.py docs/guide/repo-setup.md
```
Paste-able message:
```
docs: run the expect() doctest under sphinx; fix stale ConfigModule prose

Drop the `::` that turned OttoSuite.expect's example into a non-executed
literal block, so sphinx renders+runs it (pytest already did). Reword the
repo-setup "ConfigModule" step to the current OttoContext/set_context model.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 6: Bounded extra conversions + final verification

Audit for any remaining clearly-load-bearing prose fence worth converting, then verify the whole gate and report the before/after numbers.

**Files:**
- Modify: any `docs/**/*.md` with a clearly load-bearing, side-effect-free `python` fence still worth converting (may be none).

- [ ] **Step 1: Enumerate remaining un-executed prose examples**

Run: `uv run python - <<'PY'`
```python
import pathlib, re
root = pathlib.Path("docs")
for md in sorted(root.rglob("*.md")):
    if any(p in md.parts for p in ("_build", "superpowers")):
        continue
    in_py = False
    for n, ln in enumerate(md.read_text().splitlines(), 1):
        s = ln.strip()
        if s.startswith("```"):
            in_py = s[3:].strip() in ("python", "py", "pycon")
            continue
        if in_py and re.search(r"\botto\b|UnixHost|OttoSuite|register_", ln):
            print(f"{md}:{n}: {ln.strip()[:80]}")
            break
PY
```
Expected: a list of files whose `python` fences reference otto APIs. For each, judge: is it **side-effect-free and runnable in-memory** (construct objects, validate, pure functions) AND load-bearing (could rot)? Convert only those, exactly as in the prior conversion pass (change `` ```python `` → `` ```{doctest} ``, prefix `>>> `/`... `, add an output line). **Skip** anything needing a connection, the monitor server, a loaded on-disk lab, or that mutates a global registry (`register_*`). It is acceptable for this step to convert nothing if no clear win remains — note that outcome explicitly.

- [ ] **Step 2: For each chosen conversion, verify it executes before committing**

For any block converted, run the document's doctests:
Run: `make doctest`
Expected: `build succeeded`, `0 failures`, and the converted document's test count increased.

- [ ] **Step 3: Final full-gate verification + report**

Run: `make docs`
Expected: `docs-lint` (doc8 + markdown lint), `docs-html` (`build succeeded`), `doctest` (sphinx, `0 failures`), `doctest-src` (`7 passed`) all green.

Capture and report the final numbers:
Run: `grep -E "^\s*[0-9]+ tests$" docs/_build/doctest/output.txt | tail -1`
Report: sphinx doctest total (started this effort at 53, was 83 before this plan, now higher), and the `doctest-src` tally (`7 passed`). State what, if anything, Step 1 converted.

- [ ] **Step 4: Stage + hand off commit message (only if Step 1 converted anything)**

```bash
git add docs/
```
Paste-able message:
```
docs: convert remaining load-bearing prose examples to {doctest}

Final sweep of side-effect-free, in-memory examples still rendered as
illustrative python fences; converted the clear wins to executed
{doctest} blocks. Full docs gate green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
If Step 1 converted nothing, skip this commit and note the audit found no further clear wins.

---

## Notes for the executor

- **Order matters across tasks but each is independently committable.** Task 2 depends on Task 1's script; Task 4's verification depends on Task 3's gate; Task 5/6 depend on the gate existing. Do them in order.
- **`make docs` is the single source of truth** for "is the doctest surface healthy." After every task, it must be green.
- **If `doctest-src` ever shows a NEW failure** (not the known green set), stop — it means a docstring example genuinely broke; fix the example or the code, do not weaken the gate.
- **Do not add `--doctest-glob='*.md'`** — see the spec's "Why not" section.
