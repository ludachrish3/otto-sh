# Release-target Cleanup + Publishing-docs Scrub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the unused manual-publish Makefile targets and make the publishing docs accurately describe how each release artifact is published.

**Architecture:** Three independent edits — (1) the Makefile loses the `publish`/`publish-test` recipes and the stale `test` `.PHONY` entry, and the `release` recipe's closing message is rewritten; (2) `docs/release_process.md` drops the "Manual fallbacks" section and gains a "Documentation publishing" section clarifying that ReadTheDocs builds independently of GitHub Actions; (3) `docs/contributing.md` replaces references to the removed `make test` target. A final task runs the docs gate and stages everything for a single human commit.

**Tech Stack:** GNU Make, Markdown, Sphinx (docs build via `make docs`).

## Global Constraints

- **No self-commit in otto-sh.** The `prepare-commit-msg` hook needs `/dev/tty`; an agent-run `git commit` mis-tags the AI-assist trailer. Each task ends by **staging** (`git add`) and verifying; the actual `git commit` is left to Chris, who is given a paste-able message in the final task.
- **Do not modify the workflow YAML.** `release.yml`, `release-testpypi.yml`, `ci.yml`, `nightly.yml` are correct; only prose describing them changes.
- **Keep the `changelog` Makefile target** — it is a standalone tool referenced by a comment in `pyproject.toml`, not a publish target.
- **Docs build is the gate.** Any Markdown edit must survive `make docs` (Sphinx `-W`, warnings-as-errors). No Python changes here, so the coverage/nox/typecheck gates are not in scope.
- Source-of-truth spec: `docs/superpowers/specs/2026-06-27-release-targets-cleanup-design.md`.

---

### Task 1: Makefile — remove manual-publish targets, fix `.PHONY`, rewrite `release` closing message

**Files:**
- Modify: `Makefile` (`.PHONY` at L3; the `release` recipe closing `echo` block ~L111-120; delete the `publish-test`/`publish` targets ~L122-129)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a Makefile where `make help` lists no `publish`/`publish-test` targets and the `release` recipe still parses. No other task depends on this.

- [ ] **Step 1: Edit `.PHONY` (L3) — remove `test`, `publish-test`, `publish`**

Current line 3:

```make
.PHONY: help all ci nox nox-unit nox-unix nox-embedded validate clean-dist dev build test coverage coverage-unit coverage-unix coverage-embedded docs docs-html docs-inventories doctest doctest-src typecheck clean changelog release publish-test publish stability stability-unit stability-unix stability-embedded repeat vm-health qemu-restart
```

Replace with (drops `build test` → `build`, and removes `publish-test publish`):

```make
.PHONY: help all ci nox nox-unit nox-unix nox-embedded validate clean-dist dev build coverage coverage-unit coverage-unix coverage-embedded docs docs-html docs-inventories doctest doctest-src typecheck clean changelog release stability stability-unit stability-unix stability-embedded repeat vm-health qemu-restart
```

- [ ] **Step 2: Rewrite the `release` recipe's closing `echo` block**

Find this block at the end of the `release` recipe (currently ~L111-120):

```make
		&& echo \
		&& echo "Regenerated CHANGELOG.md, bumped version, tagged, and built dist/." \
		&& echo "Pushing the tag fires .github/workflows/release.yml, which" \
		&& echo "publishes to PyPI via OIDC (gated by the 'pypi' environment)." \
		&& echo "Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo \
		&& echo "Manual fallbacks (require UV_PUBLISH_TOKEN in env):" \
		&& echo "    make publish-test    # upload dist/ to TestPyPI" \
		&& echo "    make publish         # upload dist/ to PyPI"
```

Replace with:

```make
		&& echo \
		&& echo "Regenerated CHANGELOG.md, bumped version, tagged, and built dist/." \
		&& echo "Pushing the tag fires .github/workflows/release.yml, which builds," \
		&& echo "publishes to PyPI via OIDC (gated by the 'pypi' environment), and" \
		&& echo "creates the GitHub Release." \
		&& echo "Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo \
		&& echo "To rehearse first, dispatch release-testpypi.yml from the Actions tab."
```

- [ ] **Step 3: Delete the `publish-test` and `publish` targets**

Remove these lines (currently ~L122-129, plus the blank line at L126 between them and the trailing blank at L130 — leave exactly one blank line before the `nox-unit:` target):

```make
publish-test: ## Manual fallback: upload dist/ to TestPyPI (prefer dispatching release-testpypi.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--publish-url https://test.pypi.org/legacy/ \
		--check-url   https://test.pypi.org/simple/

publish: ## Manual fallback: upload dist/ to PyPI — permanent (prefer pushing a v* tag to fire release.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--check-url https://pypi.org/simple/
```

After deletion the `release` recipe should be followed by one blank line, then `nox-unit:`.

- [ ] **Step 4: Verify the Makefile parses and the targets are gone**

Run:

```bash
make help
grep -nE 'publish-test|^publish:|uv publish' Makefile; echo "exit=$?"
grep -nE '(^| )test( |$)' Makefile | grep -i phony
```

Expected:
- `make help` prints the help text with **no** `publish` or `publish-test` rows and **no** Make parse error.
- The first `grep` prints nothing and `exit=1` (no matches).
- The second `grep` prints nothing (the `.PHONY` line no longer contains a standalone `test`).

- [ ] **Step 5: Stage the Makefile**

```bash
git add Makefile
```

(Do not commit — see Global Constraints. The commit happens in Task 4.)

---

### Task 2: `docs/release_process.md` — drop "Manual fallbacks", add "Documentation publishing"

**Files:**
- Modify: `docs/release_process.md` (delete the "Manual fallbacks" section ~L64-75; add a "Documentation publishing" section in its place)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: a runbook with no `make publish`/`make publish-test` references and an explicit note that ReadTheDocs builds independently of Actions. The docs build in Task 4 verifies it.

- [ ] **Step 1: Delete the "Manual fallbacks" section**

Remove the blank line and the entire section (currently ~L64-75):

```markdown

## Manual fallbacks

If the workflow is unavailable, `dist/` can be uploaded by hand with a
`UV_PUBLISH_TOKEN` in the environment:

```bash
make publish-test   # upload dist/ to TestPyPI
make publish        # upload dist/ to PyPI — permanent
```

Prefer the tag-driven workflow; the manual targets exist only as a fallback.
```

- [ ] **Step 2: Add a "Documentation publishing" section**

In place of the deleted section (i.e. as the new final section, after "TestPyPI dry-run"), add:

```markdown
## Documentation publishing

The hosted documentation at
[otto-sh.readthedocs.io](https://otto-sh.readthedocs.io) is **not** built or
published by GitHub Actions. Read the Docs builds it independently off its own
webhook, using
[`.readthedocs.yaml`](https://github.com/ludachrish3/otto-sh/blob/main/.readthedocs.yaml)
(`sphinx-build -W`, so warnings fail the build). It tracks the configured
branch/version rather than the `v*` release tag — a documentation change lands
when it reaches that branch, independent of cutting a PyPI release.
```

- [ ] **Step 3: Verify the section swap**

Run:

```bash
grep -nE 'make publish|UV_PUBLISH_TOKEN|Manual fallbacks' docs/release_process.md; echo "exit=$?"
grep -n 'Documentation publishing' docs/release_process.md
```

Expected:
- The first `grep` prints nothing and `exit=1` (no surviving manual-fallback references).
- The second `grep` prints the new heading line.

- [ ] **Step 4: Stage the file**

```bash
git add docs/release_process.md
```

---

### Task 3: `docs/contributing.md` — replace removed `make test` references

**Files:**
- Modify: `docs/contributing.md` (the "Running tests" code block ~L246-250; the trailing note ~L268)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: a contributing guide whose test-running instructions match the current Makefile (no `make test`). Verified by Task 4's docs build plus a grep.

- [ ] **Step 1: Replace the "Running tests" code block**

Current block (~L246-250):

```markdown
```bash
make test                     # run all tests
make test TESTS=test_host     # filter by keyword
make coverage                 # run tests and enforce coverage threshold
```
```

Replace the three command lines with:

```markdown
```bash
make coverage                 # run the full suite and enforce the coverage gate
uv run pytest -k test_host    # run a subset by keyword
```
```

- [ ] **Step 2: Fix the trailing note**

Current (~L268):

```markdown
`make test TESTS=<kw>` filters any run by keyword. Recover a wedged embedded bed
with `make qemu-restart`; probe the whole lab with `make vm-health`.
```

Replace the first sentence so it reads:

```markdown
`uv run pytest -k <kw>` filters any run by keyword. Recover a wedged embedded bed
with `make qemu-restart`; probe the whole lab with `make vm-health`.
```

- [ ] **Step 3: Verify no stale `make test` references remain**

Run:

```bash
grep -nE 'make test' docs/contributing.md; echo "exit=$?"
```

Expected: prints nothing and `exit=1` (no matches).

- [ ] **Step 4: Stage the file**

```bash
git add docs/contributing.md
```

---

### Task 4: Run the docs gate and hand off the commit

**Files:**
- None modified. Verifies the staged Markdown builds clean, then prepares the commit.

**Interfaces:**
- Consumes: the staged edits from Tasks 1-3.
- Produces: a green docs build and a paste-able commit message for Chris.

- [ ] **Step 1: Build the docs under warnings-as-errors**

Run:

```bash
make docs
```

Expected: completes successfully (doc8 + markdown doctest-fence lint + Sphinx `-W` HTML build + doctests all pass). If Sphinx reports a warning (treated as an error), fix the offending Markdown in the relevant task's file and re-run before proceeding.

- [ ] **Step 2: Confirm everything is staged**

Run:

```bash
git status --short
```

Expected: `Makefile`, `docs/release_process.md`, and `docs/contributing.md` are staged (`M ` in the left column). Optionally also stage the spec and this plan if Chris wants them in the same commit:

```bash
git add docs/superpowers/specs/2026-06-27-release-targets-cleanup-design.md docs/superpowers/plans/2026-06-27-release-targets-cleanup.md
```

- [ ] **Step 3: Hand the commit to Chris (do not self-commit)**

Provide this paste-able message:

```
chore(release): drop manual-publish make targets; scrub publishing docs

Only `make release` is used to cut releases; pushing the resulting v* tag
fires release.yml, which builds, publishes to PyPI via OIDC, and creates the
GitHub Release. The `publish`/`publish-test` make targets duplicated that path
and were never used, so remove them (and the stale `test` .PHONY entry), and
rewrite the `release` recipe's closing message accordingly.

Docs scrub: drop the "Manual fallbacks" section from release_process.md and add
a "Documentation publishing" section clarifying that Read the Docs builds the
docs independently off its own webhook — not GitHub Actions. Replace the
references to the removed `make test` target in contributing.md.
```

---

## Self-Review

**Spec coverage:**
- Remove `publish`/`publish-test` targets → Task 1, Step 3. ✓
- Edit `.PHONY` (drop `publish-test`, `publish`, stale `test`) → Task 1, Step 1. ✓
- Trim `release` echo block (drop manual-fallback lines, keep tag-push guidance, name all three artifacts, point at TestPyPI dry-run) → Task 1, Step 2. ✓
- Delete "Manual fallbacks" section in `release_process.md` → Task 2, Step 1. ✓
- Add PyPI-vs-RTD-vs-GitHub-Release accuracy (RTD builds independently) → Task 2, Step 2. ✓
- Fix stale `make test` refs in `contributing.md` (both spots) → Task 3, Steps 1-2. ✓
- Keep `changelog`, leave workflows/getting-started/README untouched → enforced by Global Constraints and absence of any task touching them. ✓
- Verification (`make help`, greps, `make docs`) → Tasks 1-4 verification steps. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every edit shows exact before/after text. ✓

**Type consistency:** No code types involved. Command and path names (`make coverage`, `uv run pytest -k`, `make docs`, file paths) are consistent across tasks. ✓
