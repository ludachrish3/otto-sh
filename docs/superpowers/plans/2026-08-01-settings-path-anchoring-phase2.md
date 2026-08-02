# Settings Path Anchoring — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the repo-root path convention everywhere it is documented — one canonical statement, every other page linking to it — and make `otto init` scaffold the new form.

**Architecture:** `docs/guide/setup/repo-setup.md` is the settings reference ("This page explains every setting"). Its existing `### Variable expansion` section becomes the single normative statement of the convention. Eight other pages drop their local explanations and link to it with MyST's `{doc}` role. The `otto init` scaffold — the most-copied example in the project — emits bare relative paths.

**Tech Stack:** MyST-flavoured Markdown, Sphinx (`-E -a -W`, warnings are errors), doc8, Python 3.10+.

**Spec:** `docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md` §10 and §11.2. Phase 1 (the code) is merged to main as `4e7d6438`. **Phase 3 (`${sut_dir}` narrowing) is NOT in scope** — the variable still expands everywhere and must keep working.

## Global Constraints

- 🚨 **THE TEST BED IS OFF-LIMITS.** Another agent is running chaos testing against the lab VMs. Run **hostless targets only**. Permitted: `make docs-lint`, `make docs-html`, `make check-python`, `.venv/bin/pytest tests/unit/...`. **Forbidden:** `make coverage`, `make coverage-python`, `make coverage-integration`, `make coverage-unix`, `make coverage-embedded`, `make nox*`, `make dashboard`, `make all`, `make ci`, `make validate*`, `make release`, `make docs-media`, and any bare `pytest` that could collect outside `tests/unit/` (it would reach `tests/integration/` and `tests/e2e/`). Always pass an explicit `tests/unit/...` path.
- **DOCS ARE THE DELIVERABLE — every claim must be true of the code at `4e7d6438`.** Do not describe phase-3 behavior as if it shipped. Specifically: `${sut_dir}` **still expands in every settings table today**. Never write that it is removed, deprecated, rejected, or restricted.
- **The normative rule, stated once, in these exact terms:** every path in `.otto/settings.toml` is `expanduser()`-expanded; if it is still relative it resolves against the repo root (the directory containing `.otto/`); absolute paths pass through unchanged.
- **Never claim a path is "relative to the CWD"** anywhere — that was the bug phase 1 removed.
- **No new `${sut_dir}` in any example** for a setting otto types as a path (`labs`, `libs`, `tests`, `[docker]` paths, `[monitor]` TLS, `[reservations.json] path`, `[coverage.tiers] harvest_dirs`).
- **Sphinx runs with `-W`** — a broken `{doc}` reference fails the build. Every cross-reference you add must resolve. Paths in `{doc}` are relative to the *referencing* file.
- **Do not run `make docs-media`** (regenerates GUI media via Playwright — slow, and unnecessary for text edits).
- Use `.venv/bin/pytest`, not `uv run` (which can dirty `uv.lock`).
- **Commits:** this is a worktree, so commit each task yourself. End every commit message body with the trailer `Assisted-by: Claude Opus 5`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `docs/guide/setup/repo-setup.md` | The settings reference | **Canonical statement of the convention** + example |
| `src/otto/cli/init_templates.py` | `otto init` scaffold text | Bare relative paths in the emitted TOML |
| `tests/unit/cli/test_init_scaffold.py` | Pins the scaffold's contents | Update the three path assertions |
| `docs/overview.md` | Product tour | Example + prose |
| `docs/getting-started.md` | First-run walkthrough | Example + prose |
| `docs/guide/setup/host-database.md` | Lab data sourcing | Example |
| `docs/guide/docker.md` | `[docker]` reference | 5 example paths |
| `docs/guide/reservations.md` | `[reservations]` reference | Example + prose |
| `docs/guide/coverage.md` | Coverage reference | Comment + table row |
| `docs/guide/hosts/os-profiles.md` | `[os_profiles]` reference | Prose |

Task 1 must land first — Tasks 2 and 3 reference the anchor it creates.

---

### Task 1: The canonical statement in the settings reference

`docs/guide/setup/repo-setup.md` opens with "This page explains every setting", making it the natural single home. Its current `### Variable expansion` section documents only `${sut_dir}` and never states what a bare relative path means — the gap this task closes.

**Files:**

- Modify: `docs/guide/setup/repo-setup.md` (the settings example, and the `### Variable expansion` section)

**Interfaces:**

- Consumes: nothing.
- Produces: a section anchor that Tasks 2-3 link to. The section MUST be titled exactly `### Path resolution` so cross-references are predictable. Other pages will link to the page with ``{doc}`setup/repo-setup` `` (or the correct relative path from their own location).

- [ ] **Step 1: Update the settings example**

In `docs/guide/setup/repo-setup.md`, in the ```` ```toml ```` block under "## The settings file", change these three lines:

```toml
labs  = ["${sut_dir}/../lab_data"]
libs  = ["${sut_dir}/pylib"]
tests = ["${sut_dir}/tests"]
```

to:

```toml
labs  = ["../lab_data"]
libs  = ["pylib"]
tests = ["tests"]
```

Leave every other line of that block unchanged.

- [ ] **Step 2: Replace the `### Variable expansion` section**

Replace this entire section (heading and body):

```text
### Variable expansion

`${sut_dir}` is replaced with the absolute path to the repo root at load
time.  Use it to keep paths relative and portable.  Expansion runs
inside every settings table, including string values nested under
`[host_preferences]`.
```

with (the outer fence below is four backticks so the nested TOML block is
part of the content you paste, not a delimiter):

````text
### Path resolution

Every path in this file is expanded with `~` (your home directory), and
if it is still relative it resolves against **the repo root** — the
directory containing `.otto/`.  Absolute paths are used as written.

```toml
tests    = ["tests"]                     # <repo>/tests
libs     = ["../shared/pylib"]           # <repo>/../shared/pylib
tls_cert = "~/.config/otto/tls/cert.pem" # $HOME/.config/otto/tls/cert.pem
```

This file is committed and shared by everyone working on the repo, so a
path is never interpreted relative to the directory you happen to run
`otto` from.  Use `~` when you deliberately want a per-user location,
such as TLS material or an SSH `known_hosts` file.

When several repos are active at once (`OTTO_SUT_DIRS`), each
`settings.toml` resolves against its own repo root — the same text means
the right thing in every repo.

#### `${sut_dir}`

`${sut_dir}` expands to the absolute path of the repo root, in every
settings table.  For the settings above it is redundant — a plain
relative path already resolves there — but it remains useful in the
tables otto passes through to a backend without interpreting them
(`[lab.<backend>]`, `[reservations.<backend>]`, and `ssh_options`),
where otto cannot know which values are paths:

```toml
[lab.sqlbackend]
db_url = "sqlite:///${sut_dir}/lab.db"
```
````

Note for the implementer: everything between the four-backtick fences above is the replacement content, including its own three-backtick TOML blocks. Reproduce those inner fences exactly — they are part of the page, not delimiters of this instruction.

- [ ] **Step 3: Verify docs lint passes**

Run: `make docs-lint`

Expected: doc8 and the markdown-doctest linter both clean.

- [ ] **Step 4: Verify the Sphinx build passes**

Run: `make docs-html`

Expected: a clean `-E -a -W` rebuild with no warnings. **If the build reports a warning about your new content, fix it — `-W` makes warnings errors, so a warning here is a failed build.**

- [ ] **Step 5: Commit**

```bash
git add docs/guide/setup/repo-setup.md
git commit -m "$(cat <<'EOF'
docs(settings): state the repo-root path convention canonically

repo-setup.md is the settings reference, but its "Variable expansion"
section documented only ${sut_dir} and never said what a bare relative
path means -- the ambiguity phase 1 removed from the code.

Replaces it with "Path resolution": ~ expands, still-relative resolves
against the repo root, absolute passes through. Explains why a
committed, team-shared file cannot use CWD-relative paths, and keeps
${sut_dir} documented as what it is today -- redundant for typed
settings, still useful in the passthrough tables otto does not
interpret.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 2: The `otto init` scaffold

The scaffold is the most-copied settings example in the project — every new repo starts from it. It currently emits `${sut_dir}`-prefixed paths.

**Files:**

- Modify: `src/otto/cli/init_templates.py` (the settings template, around the `labs`/`tests`/`libs` lines)
- Modify: `tests/unit/cli/test_init_scaffold.py` (the three path assertions)

**Interfaces:**

- Consumes: nothing from Task 1 (no cross-reference needed here).
- Produces: the scaffold now emits `labs = ["lab_data"]`, `tests = ["tests"]`, `libs = ["pylib"]`.

**Note on escaping:** `init_templates.py` builds its template with f-strings, so the literal `${sut_dir}` appears in source as `${{sut_dir}}`. Your replacement removes the variable entirely, so the doubled braces go away with it — but be careful not to disturb any other `{{`/`}}` on nearby lines.

- [ ] **Step 1: Update the test first (it pins the scaffold)**

In `tests/unit/cli/test_init_scaffold.py`, change:

```python
    assert data["labs"] == ["${sut_dir}/lab_data"]
    assert data["tests"] == ["${sut_dir}/tests"]
    assert data["libs"] == ["${sut_dir}/pylib"]
```

to:

```python
    assert data["labs"] == ["lab_data"]
    assert data["tests"] == ["tests"]
    assert data["libs"] == ["pylib"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/cli/test_init_scaffold.py -v`

Expected: FAIL — the assertion shows the scaffold still emits `['${sut_dir}/lab_data']`.

- [ ] **Step 3: Update the scaffold template**

In `src/otto/cli/init_templates.py`, change these four lines:

```python
# Where otto looks for things, relative to this repo's root (${{sut_dir}}).
labs = ["${{sut_dir}}/lab_data"]   # directories searched for lab.json
tests = ["${{sut_dir}}/tests"]     # defines where test discovery happens
libs = ["${{sut_dir}}/pylib"]      # added to sys.path at startup
```

to:

```python
# Where otto looks for things. Relative paths resolve against this repo's
# root (the directory holding .otto/); "~" expands to your home directory.
labs = ["lab_data"]   # directories searched for lab.json
tests = ["tests"]     # defines where test discovery happens
libs = ["pylib"]      # added to sys.path at startup
```

Keep the surrounding blank lines and comment alignment as they are.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/cli/test_init_scaffold.py -v`

Expected: PASS.

- [ ] **Step 5: Run the whole CLI unit suite**

Run: `.venv/bin/pytest tests/unit/cli/ -q`

Expected: all pass. `tests/unit/cli/test_init_templates.py` contains an "uncomment drift" test that pins the raw template text — if it fails, you changed spacing or comment style it depends on. **If anything here fails, STOP and report** rather than editing that test to match.

- [ ] **Step 6: Verify a scaffolded repo actually resolves**

This is the check that proves the scaffold is not just syntactically valid but semantically correct under phase 1's anchoring. Run:

```bash
.venv/bin/pytest tests/unit/cli/test_init_scaffold.py tests/unit/config/test_settings_path_anchoring.py -q
```

Expected: all pass.

- [ ] **Step 7: Run the static gate**

Run: `make check-python`

Expected: ruff lint + format clean, `ty` clean.

- [ ] **Step 8: Commit**

```bash
git add src/otto/cli/init_templates.py tests/unit/cli/test_init_scaffold.py
git commit -m "$(cat <<'EOF'
docs(init): scaffold bare relative paths, not ${sut_dir}

The otto init scaffold is the most-copied settings example in the
project, so it should model the convention phase 1 established: a
relative path resolves against the repo root. Drops the ${sut_dir}
prefix from labs/tests/libs and says what relative means in the
accompanying comment.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 3: The remaining seven pages

Each page drops its local explanation and links to the canonical section. Two of these pages (`coverage.md`, `reservations.md`) already state the repo-root rule correctly — those sentences become redundant once the canonical statement exists.

**Files:**

- Modify: `docs/overview.md`, `docs/getting-started.md`, `docs/guide/setup/host-database.md`, `docs/guide/docker.md`, `docs/guide/reservations.md`, `docs/guide/coverage.md`, `docs/guide/hosts/os-profiles.md`

**Interfaces:**

- Consumes: the `### Path resolution` section created by Task 1 in `docs/guide/setup/repo-setup.md`.
- Produces: nothing later tasks depend on.

**Cross-reference paths differ per file** — `{doc}` targets are relative to the referencing file, and Sphinx `-W` fails the build on a bad one:

| From | Reference to use |
| --- | --- |
| `docs/overview.md` | ``{doc}`guide/setup/repo-setup` `` |
| `docs/getting-started.md` | ``{doc}`guide/setup/repo-setup` `` |
| `docs/guide/docker.md` | ``{doc}`setup/repo-setup` `` |
| `docs/guide/reservations.md` | ``{doc}`setup/repo-setup` `` |
| `docs/guide/coverage.md` | ``{doc}`setup/repo-setup` `` |
| `docs/guide/setup/host-database.md` | ``{doc}`repo-setup` `` |
| `docs/guide/hosts/os-profiles.md` | ``{doc}`../setup/repo-setup` `` |

- [ ] **Step 1: `docs/overview.md`**

Change the three example lines:

```toml
labs  = ["${sut_dir}/../lab_data"]
libs  = ["${sut_dir}/pylib"]
tests = ["${sut_dir}/tests"]
```

to:

```toml
labs  = ["../lab_data"]
libs  = ["pylib"]
tests = ["tests"]
```

Then replace the sentence:

```text
`${sut_dir}` is replaced with the repository root at load time.  The `init`
list names Python modules that otto imports at startup — this is where you
register your instructions and shared options.
```

with:

```text
Relative paths resolve against the repository root — see
{doc}`guide/setup/repo-setup`.  The `init` list names Python modules that
otto imports at startup — this is where you register your instructions and
shared options.
```

- [ ] **Step 2: `docs/getting-started.md`**

Change the three example lines:

```toml
labs  = ["${sut_dir}/lab_data"]
tests = ["${sut_dir}/tests"]
libs  = ["${sut_dir}/pylib"]
```

to:

```toml
labs  = ["lab_data"]
tests = ["tests"]
libs  = ["pylib"]
```

Then replace the sentence:

```text
`${sut_dir}` is automatically replaced with the repository root directory at
load time.
```

with:

```text
Relative paths resolve against the repository root; `~` expands to your home
directory.  See {doc}`guide/setup/repo-setup` for the full rule.
```

- [ ] **Step 3: `docs/guide/setup/host-database.md`**

Change:

```toml
labs = ["${sut_dir}/lab_data"]
```

to:

```toml
labs = ["lab_data"]
```

This is inside a ```` ```toml ```` block; change only that line.

- [ ] **Step 4: `docs/guide/docker.md`**

Change all five paths in the ```` ```toml ```` example:

```toml
dockerfile = "${sut_dir}/docker/api.Dockerfile"
context = "${sut_dir}/docker"
```

→

```toml
dockerfile = "docker/api.Dockerfile"
context = "docker"
```

and

```toml
dockerfile = "${sut_dir}/docker/db.Dockerfile"
context = "${sut_dir}/docker"
```

→

```toml
dockerfile = "docker/db.Dockerfile"
context = "docker"
```

and

```toml
path = "${sut_dir}/docker/compose.yml"
```

→

```toml
path = "docker/compose.yml"
```

Preserve the inline `#` comments and their column alignment on the lines that have them.

Then, immediately after the closing ```` ``` ```` of that block, add:

```text
Relative paths resolve against the repo root — see
{doc}`setup/repo-setup`.
```

- [ ] **Step 5: `docs/guide/reservations.md`**

Change:

```toml
path = "${sut_dir}/.otto/reservations.json"
```

to:

```toml
path = ".otto/reservations.json"
```

Then replace the now-redundant explanation:

```text
Relative paths are resolved against the repo root.  `${sut_dir}` expands
to the repo root too, so either works.
```

with:

```text
Relative paths resolve against the repo root — see
{doc}`setup/repo-setup`.
```

- [ ] **Step 6: `docs/guide/coverage.md`**

Change the inline comment:

```text
harvest_dirs = ["build"]     # swept for .gcda at report time; "${sut_dir}" expands
```

to:

```text
harvest_dirs = ["build"]     # swept for .gcda at report time; relative to the repo root
```

Check the line immediately following it — the original comment wrapped onto a continuation line. If a dangling continuation remains after your edit, fold it in or remove it so the comment reads as one coherent sentence.

Then change the table row:

```text
| `harvest_dirs` | `unit`-kind only: build directories swept for `.gcda` at report time. `"${sut_dir}"` expands to the repo's SUT directory; relative paths resolve against the repo root. |
```

to:

```text
| `harvest_dirs` | `unit`-kind only: build directories swept for `.gcda` at report time. Relative paths resolve against the repo root (see {doc}`setup/repo-setup`). |
```

- [ ] **Step 7: `docs/guide/hosts/os-profiles.md`**

Change:

```text
is a raw field default merged beneath each matching host's own fields (with
`${sut_dir}` expansion applied).
```

to:

```text
is a raw field default merged beneath each matching host's own fields (with
the usual path expansion applied — see {doc}`../setup/repo-setup`).
```

- [ ] **Step 8: Confirm no stale references remain**

Run:

```bash
grep -rn '\${sut_dir}' docs/ --include="*.md" --include="*.rst" | grep -v "docs/superpowers/"
```

Expected: **exactly one** hit — the `db_url = "sqlite:///${sut_dir}/lab.db"` example in `docs/guide/setup/repo-setup.md` that Task 1 deliberately added. Any other hit is a page this task missed. If you find one, fix it the same way and note it in your report.

- [ ] **Step 9: Verify docs lint passes**

Run: `make docs-lint`

Expected: clean. doc8 enforces line length — if you introduced a long line, wrap it.

- [ ] **Step 10: Verify the Sphinx build passes**

Run: `make docs-html`

Expected: a clean `-E -a -W` rebuild, no warnings. A bad `{doc}` target surfaces here as `WARNING: unknown document` and fails the build. If that happens, re-check the reference against the table at the top of this task.

- [ ] **Step 11: Commit**

```bash
git add docs/overview.md docs/getting-started.md docs/guide/setup/host-database.md \
        docs/guide/docker.md docs/guide/reservations.md docs/guide/coverage.md \
        docs/guide/hosts/os-profiles.md
git commit -m "$(cat <<'EOF'
docs: point every settings example at the repo-root convention

Seven pages carried their own ${sut_dir} examples, and two restated the
repo-root rule in their own words. Each now shows a bare relative path
and links to the canonical statement in repo-setup.md, so the rule has
one home instead of nine paraphrases.

Assisted-by: Claude Opus 5
EOF
)"
```

---

## Phase Completion

Verify the whole phase in one pass:

```bash
grep -rn '\${sut_dir}' docs/ src/otto/cli/init_templates.py --include="*.md" --include="*.rst" --include="*.py" | grep -v "docs/superpowers/"
```

Expected: exactly one hit — the passthrough-table example in `repo-setup.md`.

Then hand back to Chris. Report:

- That `make docs-lint` and `make docs-html` (clean `-E -a -W`) are green.
- That `make docs` in full (which adds `doctest` and `doctest-src`) has **not** been run as a single target, and neither has `make coverage` — the lab VMs are in use.
- Phase 3 (`${sut_dir}` narrowing) remains unplanned. Note that Task 1's `#### ${sut_dir}` subsection is written to survive phase 3 unchanged: it already frames the variable as redundant-for-typed-settings and necessary-for-passthrough-tables, which is exactly what phase 3 makes enforceable.
