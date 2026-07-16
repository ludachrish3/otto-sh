# Docs Toctree Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure otto's Sphinx docs into functional areas per
`docs/superpowers/specs/2026-07-16-docs-toctree-restructure-design.md`:
grouped User Guide, design-only Architecture (per-command lifecycle pages
dissolved), a new top-level Python-library section, wired extensibility
links, and no implementation-phasing language.

**Architecture:** Pure docs restructure — `git mv` plus MyST/RST editing.
No `src/` behavior changes; only docstring/help-text *path strings* that
name doc files are updated. Every task ends with a green
`sphinx-build -W` (nitpicky, warnings-as-errors — the build IS the test)
and a commit.

**Tech Stack:** Sphinx + MyST markdown, doc8, `make docs` / `nox -s docs`.

## Global Constraints

- **The spec is the law.** Read
  `docs/superpowers/specs/2026-07-16-docs-toctree-restructure-design.md`
  before starting any task. Its "Editorial rules" section governs every
  content decision; tasks below reference the rules by name.
- **Single source of truth:** one canonical page per topic; other mentions
  are links (max a sentence of context).
- **Behavior vs. mechanism:** flags/workflows/examples → User Guide only;
  design/invariants/trade-offs → Architecture only; link across, never
  paraphrase.
- **Current state only:** no "later phase", no roadmap/`todo/` pointers, no
  future-feature promises. Restate as current fact or delete.
- **Move files with `git mv`** to preserve history.
- **Per-task gate:** `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
  → exit 0. (Sphinx is nitpicky + `-W`: any broken `{doc}`/`{ref}`/include
  fails the build. This is the failing-test/passing-test cycle for docs.)
- **Final gate is `make docs`** (fresh `-E -a` build + doctests) — a scoped
  green is not the gate.
- **`{ref}` anchor targets are location-independent** — file moves never
  break them. Only `{doc}` roles (relative paths) and `:file:`/image paths
  need fixing.
- **MyST toctrees** in `.md` files use the ` ```{toctree} ` fence (copy the
  idiom from the existing `docs/guide/host/index.md`).
- **Commits:** conventional prefix, end body with
  `Assisted-by: Claude Fable 5 <noreply@anthropic.com>`.
- **Do not touch** `docs/api/`, `docs/_static/` sources,
  `docs/superpowers/`, or web/monitor code.
- Work happens in a **worktree** (Task 0); self-committing there is
  allowed per repo policy.

## Reference: inbound-ref sweep command

Used by every move task. After `git mv`, find every reference to the old
docname (both `{doc}` roles in docs and path strings elsewhere):

```bash
grep -rn "PAGENAME" docs --include='*.md' --include='*.rst' | grep -v _build | grep -v superpowers
grep -rn "docs/guide/PAGENAME\|docs/cookbook" src scripts web/src README.md tests 2>/dev/null
```

Fix each hit: `{doc}` paths are relative to the referencing file (e.g. from
`docs/guide/test.md`, `` {doc}`options` `` becomes `` {doc}`run/options` ``;
from `docs/architecture/subsystems/*.md`, `` {doc}`../../guide/options` ``
becomes `` {doc}`../../guide/run/options` ``). The sphinx `-W` build then
verifies nothing was missed.

---

### Task 0: Worktree + toolchain + green baseline

**Files:** none (environment only)

**Interfaces:**
- Produces: a worktree at `../otto-docs-restructure` on branch
  `docs-toctree-restructure`, with deps synced, web dist built, and a green
  baseline `make docs` — every later task builds on this.

- [ ] **Step 1: Create the worktree and branch**

```bash
cd /home/vagrant/otto-sh
git worktree add ../otto-docs-restructure -b docs-toctree-restructure
cd ../otto-docs-restructure
```

- [ ] **Step 2: Sync toolchain (fresh worktrees have nothing)**

```bash
uv sync
(cd web && npm ci)
make web
```

- [ ] **Step 3: Green baseline build**

Run: `make docs`
Expected: exit 0. (conf.py auto-runs the termynal/media capture scripts —
they need the web dist from Step 2. If Playwright/Chromium is missing, run
`uv run playwright install chromium` once.)

- [ ] **Step 4: No commit** — nothing changed. Record the baseline is green.

---

### Task 1: Current-state sweep — purge phasing/roadmap language

**Files:**
- Modify: `docs/architecture/lifecycles/cov.md:9-18`
- Modify: `docs/guide/coverage.md:394-400,665-668`
- Modify: `docs/guide/tunnel.md:276-282`
- Modify: `docs/guide/monitor.md:378-382`
- Modify: `docs/guide/cli-reference.md:341`
- Modify: `docs/guide/reservations.md:233-236`
- Modify: `docs/guide/link.md:320-324`
- Modify: `docs/guide/embedded.md:140`

(Line numbers are as of 2026-07-16; locate by the quoted text if drifted.)

**Interfaces:**
- Produces: docs free of phasing language, so later content-move tasks
  never re-copy it. Verified by the grep in Step 3.

- [ ] **Step 1: Fix each offender per the spec's current-state rule**

Every edit follows one recipe: delete the future-promise, keep (or add) the
statement of what otto does *today*. Concretely:

1. `architecture/lifecycles/cov.md` — delete the entire
   ` ```{admonition} Roadmap items pending ` block (lines 9–18). Nothing
   replaces it; the page already states what ships.
2. `guide/coverage.md` (~396) — rewrite the sentence pair mentioning
   "`cov_reset` LLEXT function mirroring `cov_dump` (a later phase)" and
   "there is simply nothing this phase can clean yet" to current fact, e.g.:
   "Embedded targets expose no counter-reset hook, so `otto cov clean`
   applies to Unix coverage hosts only; on embedded hosts it is a no-op
   rather than an error."
3. `guide/coverage.md` (~667) — delete "… is planned follow-up work" and
   the feature it promises; if the surrounding sentence describes current
   behavior, end it at the current behavior.
4. `guide/tunnel.md` (~278) — replace "This phase keeps `otto.tunnel`
   monitor-free; wiring … (topology/edge views) is a later phase. See …"
   with current fact: "`otto.tunnel` does not feed the monitor: tunnels do
   not appear in topology or edge views." Keep any `{doc}` link that points
   at *existing* monitor docs; drop links whose target is the promise.
5. `guide/monitor.md` (~380) — same recipe: "… (on the collection interval,
   storing edges, topology views) is a later phase" → state what monitor
   records today, delete the rest.
6. `guide/cli-reference.md:341` — change the `clean` row's parenthetical
   "(Unix coverage hosts only — embedded reset is a later phase)" to
   "(Unix coverage hosts only)".
7. `guide/reservations.md` (~235) — delete the sentence ending "lives on
   the roadmap."; keep the description of current behavior around it.
8. `guide/link.md` (~322) — "and none is planned" → delete those four
   words; the remaining sentence states the current absence.
9. `guide/embedded.md:140` — table cell "Reserved; not yet implemented." →
   "Not supported."

Read each page's surrounding paragraph before editing — the replacement
must read as native prose, not a patch scar.

- [ ] **Step 2: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 3: Verify the purge (the "test" for this task)**

```bash
grep -rniE 'later phase|this phase (keeps|can)|roadmap|planned follow|none is planned|not yet implemented' \
  docs/guide docs/architecture docs/cookbook --include='*.md' --include='*.rst'
```
Expected: no output. ("phase 1"/"phase 2" bootstrap hits elsewhere are fine
and won't match this pattern.)

- [ ] **Step 4: Commit**

```bash
git add -A docs && git commit -m "docs: purge implementation-phasing and roadmap language

Docs capture the current state of otto; future plans live in todo/, not
in published pages.

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: User Guide — `setup/` area (Project setup)

**Files:**
- Create: `docs/guide/setup/index.md`
- Move: `docs/guide/repo-setup.md` → `docs/guide/setup/repo-setup.md`
- Move: `docs/guide/lab-config.md` → `docs/guide/setup/lab-config.md`
- Move: `docs/guide/host-database.md` → `docs/guide/setup/host-database.md`
- Move: `docs/guide/editor-schemas.md` → `docs/guide/setup/editor-schemas.md`
- Modify: `docs/guide/index.rst`
- Modify: every file the ref sweep finds (≈9 for repo-setup, 13 for
  lab-config, 6 for host-database, 3 for editor-schemas), plus
  `src/otto/cli/init.py` (path strings at lines ~6, 31, 43, 59, 74, 132)
  and `README.md:218`.

**Interfaces:**
- Produces: docnames `guide/setup/index`, `guide/setup/repo-setup`,
  `guide/setup/lab-config`, `guide/setup/host-database`,
  `guide/setup/editor-schemas` — later tasks link lab-config from the
  hosts area and editor-schemas from the schema dissolution.

- [ ] **Step 1: Move the pages**

```bash
mkdir -p docs/guide/setup
git mv docs/guide/repo-setup.md docs/guide/setup/repo-setup.md
git mv docs/guide/lab-config.md docs/guide/setup/lab-config.md
git mv docs/guide/host-database.md docs/guide/setup/host-database.md
git mv docs/guide/editor-schemas.md docs/guide/setup/editor-schemas.md
```

- [ ] **Step 2: Write `docs/guide/setup/index.md`** (thin umbrella — orient
and route, don't summarize):

````markdown
# Project setup

Everything otto knows about your project starts in two files: a
`.otto/settings.toml` at the repository root, and one or more `lab.json`
files describing the hosts otto can reach. `otto init` scaffolds both and
doctors an existing setup:

```console
$ otto init
```

The pages below cover the settings file and project discovery
({doc}`repo-setup`), defining hosts and links ({doc}`lab-config`), plugging
in a host source other than `lab.json` files ({doc}`host-database`), and
generating editor autocomplete schemas for the files you edit by hand
({doc}`editor-schemas`).

```{toctree}
repo-setup
lab-config
host-database
editor-schemas
```
````

- [ ] **Step 3: Update `docs/guide/index.rst`** — replace the four moved
entries (`repo-setup`, `lab-config`, `host-database`, `editor-schemas`)
with a single `setup/index` entry at the top of the toctree.

- [ ] **Step 4: Fix inbound references**

Run the reference-sweep grep (see header) for each of the four stems. In
docs, adjust `{doc}` relative paths. Outside docs:

- `src/otto/cli/init.py`: `docs/guide/repo-setup.md` →
  `docs/guide/setup/repo-setup.md`; `docs/guide/host-database.md` →
  `docs/guide/setup/host-database.md` (three occurrences);
  `docs/guide/lab-config.md` → `docs/guide/setup/lab-config.md`.
- `README.md`: `docs/guide/lab-config.md` →
  `docs/guide/setup/lab-config.md`.

Then check nothing asserts on the old strings:
```bash
grep -rn "guide/repo-setup\|guide/lab-config\|guide/host-database\|guide/editor-schemas" tests/
```
Expected: no output (if there are hits, update those test expectations to
the new paths).

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Unit-test the touched src module** (string-only change, but
prove it):

Run: `uv run pytest tests/unit/cli -k init -x -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "docs(guide): group project-setup pages under guide/setup/

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: User Guide — `hosts/` area

**Files:**
- Move: `docs/guide/host/` → `docs/guide/hosts/` (whole directory:
  `index.md`, `capabilities.md`, `configuration.md`, `connections.md`,
  `commands/index.md`, `commands/netcat.md`)
- Move: `docs/guide/embedded.md` → `docs/guide/hosts/embedded.md`
- Move: `docs/guide/os-profiles.md` → `docs/guide/hosts/os-profiles.md`
- Move: `docs/guide/extending-backends.md` → `docs/guide/hosts/extending-backends.md`
- Move: `docs/guide/extending-embedded.md` → `docs/guide/hosts/extending-embedded.md`
- Modify: `docs/guide/hosts/index.md`, `docs/guide/index.rst`, inbound refs
  (≈13 files for embedded, 8 for os-profiles, 7 for extending-backends,
  4 for extending-embedded, plus every `host/…` ref), `README.md:219-220`.

**Interfaces:**
- Consumes: `guide/setup/lab-config` (Task 2) for the definitions link.
- Produces: docnames `guide/hosts/index`, `guide/hosts/embedded`,
  `guide/hosts/os-profiles`, `guide/hosts/extending-backends`,
  `guide/hosts/extending-embedded`, `guide/hosts/commands/index`, etc.
  Task 9 embeds termynal captures into `guide/hosts/index.md` and
  `guide/hosts/connections.md`.

- [ ] **Step 1: Move**

```bash
git mv docs/guide/host docs/guide/hosts
git mv docs/guide/embedded.md docs/guide/hosts/embedded.md
git mv docs/guide/os-profiles.md docs/guide/hosts/os-profiles.md
git mv docs/guide/extending-backends.md docs/guide/hosts/extending-backends.md
git mv docs/guide/extending-embedded.md docs/guide/hosts/extending-embedded.md
```

- [ ] **Step 2: Update `docs/guide/hosts/index.md`**

Three edits (keep everything else as-is — this page is already the area
landing page):

1. After the opening paragraph, add the single-source-of-truth pointer:

   > Hosts are *defined* in `lab.json` — see {doc}`../setup/lab-config`.
   > This section is about *using* them.

2. Extend its toctree with the four new children (after the existing
   entries): `embedded`, `os-profiles`, `extending-backends`,
   `extending-embedded`.

3. Add a closing `## Extending` section (two sentences + links):

   > Hosts are otto's most extensible area: register new connection or
   > transfer backends ({doc}`extending-backends`) and bring up embedded
   > targets otto doesn't ship support for ({doc}`extending-embedded`).
   > The registry machinery behind every seam is described in
   > {doc}`../../architecture/subsystems/extension-points`.

- [ ] **Step 3: Update `docs/guide/index.rst`** — replace `host/index`,
`embedded`, `os-profiles`, `extending-embedded`, `extending-backends` with
one `hosts/index` entry (second position, after `setup/index`).

- [ ] **Step 4: Fix inbound references** — sweep stems `guide/host`,
`embedded`, `os-profiles`, `extending-backends`, `extending-embedded`.
Note the pages themselves moved one level deeper: any `{doc}` refs *inside
them* that pointed at guide-root siblings need a `../` prefix (the build
catches every miss). Update `README.md`:
`docs/guide/embedded.md` → `docs/guide/hosts/embedded.md`,
`docs/guide/os-profiles.md` → `docs/guide/hosts/os-profiles.md`.

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "docs(guide): gather host pages under guide/hosts/

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: User Guide — `run/` area (Running instructions)

**Files:**
- Move: `docs/guide/run.md` → `docs/guide/run/index.md`
- Move: `docs/guide/options.md` → `docs/guide/run/options.md`
- Modify: `docs/guide/run/index.md` (toctree + extending note),
  `docs/guide/test.md` (link to options' new home), `docs/guide/index.rst`,
  inbound refs (≈13 files for run, 9 for options).

**Interfaces:**
- Produces: docnames `guide/run/index`, `guide/run/options`. Task 10
  embeds `complete-instructions`/`help-run` termynal captures into
  `guide/run/index.md`.

- [ ] **Step 1: Move**

```bash
mkdir -p docs/guide/run
git mv docs/guide/run.md docs/guide/run/index.md
git mv docs/guide/options.md docs/guide/run/options.md
```

- [ ] **Step 2: Edit `docs/guide/run/index.md`** — per the spec, a
single-command area's page IS the index; add at the end:

````markdown
```{toctree}
options
```
````

and, in the section where the page first mentions options/flags, make sure
the canonical link is `` {doc}`options` `` (it was `` {doc}`options` `` as a
sibling before, so most links inside this page keep working — verify).

- [ ] **Step 3: Confirm `docs/guide/test.md` links, not restates** — its
references to options must point at `` {doc}`run/options` ``. If test.md
carries paragraphs duplicating options.md content (check while editing),
cut to one sentence + link per the single-source-of-truth rule.

- [ ] **Step 4: Update `docs/guide/index.rst`** — replace `run` and
`options` entries with `run/index` (third position).

- [ ] **Step 5: Fix inbound references** — sweep stems `guide/run` and
`options`. Careful with `options`: it's a common word — sweep for the
role text `` {doc}`options ``, `` guide/options `` instead of the bare stem.

- [ ] **Step 6: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "docs(guide): make run an area page with options as its child

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: User Guide — `network/` area (Links & tunnels)

**Files:**
- Create: `docs/guide/network/index.md`
- Move: `docs/guide/link.md` → `docs/guide/network/link.md`
- Move: `docs/guide/tunnel.md` → `docs/guide/network/tunnel.md`
- Modify: `docs/guide/index.rst`, inbound refs (≈6 files each).

**Interfaces:**
- Produces: docnames `guide/network/index`, `guide/network/link`,
  `guide/network/tunnel`. Task 13 links these from the new
  `architecture/subsystems/network` page.

- [ ] **Step 1: Move**

```bash
mkdir -p docs/guide/network
git mv docs/guide/link.md docs/guide/network/link.md
git mv docs/guide/tunnel.md docs/guide/network/tunnel.md
```

- [ ] **Step 2: Write `docs/guide/network/index.md`**

````markdown
# Links & tunnels

otto sees the lab's network twice. **Links** are the static topology — the
edges declared in `lab.json` (or derived from each host's management hop)
that {doc}`otto link <link>` inspects and impairs. **Tunnels** are dynamic:
host-resident `socat` chains that {doc}`otto tunnel <tunnel>` builds to
carry a service's traffic across the lab.

Rule of thumb: impair a *link* when you want to shape a path that already
exists; build a *tunnel* when you need a path that doesn't.

```{toctree}
link
tunnel
```
````

- [ ] **Step 3: Update `docs/guide/index.rst`** — replace `link` and
`tunnel` with `network/index`.

- [ ] **Step 4: Fix inbound references** — sweep stems `guide/link`,
`guide/tunnel`, and the sibling refs between the two pages themselves
(they cross-reference each other; those stay `` {doc}`link` `` /
`` {doc}`tunnel` `` as same-directory siblings).

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "docs(guide): group link and tunnel under guide/network/

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: User Guide — final index (order, intro, command→section table)

**Files:**
- Modify: `docs/guide/index.rst` (full rewrite)

**Interfaces:**
- Consumes: all area docnames from Tasks 2–5.
- Produces: the final guide toctree every remaining page hangs off.

- [ ] **Step 1: Replace `docs/guide/index.rst` with:**

```rst
User Guide
==========

How to use each of otto's functional areas, ordered the way a project
grows: set up the project, work with hosts, automate, shape the network,
observe the results, share the lab.

Each area corresponds to a first-party command:

.. list-table::
   :header-rows: 1

   * - Command
     - Section
   * - ``otto init`` / ``otto schema``
     - :doc:`Project setup <setup/index>`
   * - ``otto host``
     - :doc:`Hosts <hosts/index>`
   * - ``otto run``
     - :doc:`Running instructions <run/index>`
   * - ``otto test``
     - :doc:`Running test suites <test>`
   * - ``otto docker``
     - :doc:`Docker containers <docker>`
   * - ``otto link`` / ``otto tunnel``
     - :doc:`Links & tunnels <network/index>`
   * - ``otto monitor``
     - :doc:`Monitoring <monitor>`
   * - ``otto cov``
     - :doc:`Coverage <coverage>`
   * - ``otto reservation``
     - :doc:`Reservations <reservations>`

.. toctree::
   :maxdepth: 2

   setup/index
   hosts/index
   run/index
   test
   docker
   network/index
   monitor
   coverage
   reservations
   extending-cli
   cli-reference
```

(doc8 checks RST structure — keep the underline lengths exact.)

- [ ] **Step 2: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0. A "document isn't included in any toctree" warning here
means a page was dropped from the list — fix, don't suppress.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "docs(guide): final area ordering + command-to-section table

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Python library section (library-usage + cookbook)

**Files:**
- Move: `docs/guide/library-usage.md` → `docs/library/index.md`
- Move: `docs/cookbook/async-patterns.md` → `docs/library/async-patterns.md`
- Move: `docs/cookbook/sessions.md` → `docs/library/sessions.md`
- Move: `docs/cookbook/suite-recipes.md` → `docs/library/suite-recipes.md`
- Move: `docs/cookbook/connection-options.md` → `docs/library/connection-options.md`
- Delete: `docs/cookbook/index.rst`
- Modify: `docs/library/index.md`, `docs/index.rst`, `docs/guide/index.rst`
  (remove `library-usage` if still listed), inbound refs (4 files for
  library-usage; recipes have zero inbound `{doc}` refs), `README.md:221`.

**Interfaces:**
- Produces: docnames `library/index`, `library/async-patterns`,
  `library/sessions`, `library/suite-recipes`, `library/connection-options`.

- [ ] **Step 1: Move**

```bash
mkdir -p docs/library
git mv docs/guide/library-usage.md docs/library/index.md
git mv docs/cookbook/async-patterns.md docs/library/async-patterns.md
git mv docs/cookbook/sessions.md docs/library/sessions.md
git mv docs/cookbook/suite-recipes.md docs/library/suite-recipes.md
git mv docs/cookbook/connection-options.md docs/library/connection-options.md
git rm docs/cookbook/index.rst
```

- [ ] **Step 2: Adapt `docs/library/index.md`** — keep the existing
"Using otto as a library" content as the body; retitle the H1 to
`# Python library` (keep the old title as the first sentence if it aids
flow); append before the end:

````markdown
## Recipes

Patterns for common situations — runnable doctests where possible,
illustrative code where a live host is required:

```{toctree}
async-patterns
sessions
suite-recipes
connection-options
```
````

Also fix any `{doc}` refs inside the five moved pages (they now live at a
different depth: guide-root refs gain a `../guide/` prefix; the build
catches each one).

- [ ] **Step 3: Update `docs/index.rst`** — replace the `cookbook/index`
entry with `library/index`, placed directly after `guide/index`.

- [ ] **Step 4: Update `README.md`** — the `docs/cookbook/` bullet becomes
`docs/library/` ("using otto as a Python library + recipes").

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "docs: merge cookbook + library-usage into a top-level Python-library section

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Architecture — promote the shared lifecycle page

**Files:**
- Move: `docs/architecture/lifecycles/index.md` → `docs/architecture/lifecycle.md`
- Modify: `docs/architecture/lifecycle.md` (`:file:` depths + temp toctree),
  `docs/architecture/index.rst`, inbound refs (`{doc}`../lifecycles/index``,
  `` {doc}`index` `` from sibling lifecycle pages, `` {doc}`lifecycles/index` ``
  from architecture/index intro prose).

**Interfaces:**
- Produces: docname `architecture/lifecycle`. The nine per-command pages
  remain temporarily, reachable via a toctree at the bottom of
  `lifecycle.md`; Tasks 9–12 delete them one group at a time.

- [ ] **Step 1: Move**

```bash
git mv docs/architecture/lifecycles/index.md docs/architecture/lifecycle.md
```

- [ ] **Step 2: Fix paths inside `lifecycle.md`**

- The two `:file:` termynal includes lose one `../`:
  `../../_static/generated/termynal/help-otto.html` →
  `../_static/generated/termynal/help-otto.html` (same for
  `complete-lab-names.html`).
- Its toctree of per-command pages (if present as a toctree) gains the
  `lifecycles/` prefix: `cov` → `lifecycles/cov`, etc. If the page links
  them with `{doc}` roles instead, same prefix fix. Add a temporary
  MyST toctree at the bottom if none exists, listing all nine
  `lifecycles/<name>` pages, so the `-W` build keeps every page reachable:

````markdown
```{toctree}
:hidden:

lifecycles/cov
lifecycles/docker
lifecycles/host
lifecycles/init
lifecycles/monitor
lifecycles/reservation
lifecycles/run
lifecycles/schema
lifecycles/test
```
````

- Sibling refs *from* the nine lifecycle pages *to* the old index
  (`` {doc}`index` ``) become `` {doc}`../lifecycle` ``.

- [ ] **Step 3: Update `docs/architecture/index.rst`** — in the "Command
lifecycles" toctree block, replace `lifecycles/index` with `lifecycle`
(full index rewrite waits for Task 14).

- [ ] **Step 4: Fix remaining inbound refs** — sweep `lifecycles/index`
across docs (e.g. `subsystems/extension-points.md:5`,
`subsystems/data-boundary.md`, `subsystems/registries.md`).

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "docs(architecture): promote shared command-lifecycle page to architecture/lifecycle

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Dissolution tasks (9–12) — shared method

Tasks 9–12 dissolve the nine per-command lifecycle pages. Each follows the
same five-move recipe; the task lists only the page-specific routing.

1. **Route the termynal embeds** (user-facing CLI output → guide). Move
   each ` ```{raw} html ` block to the named guide page/section. Depth of
   the `:file:` path at the destination: guide root pages use
   `../_static/generated/termynal/…`; `guide/<area>/` pages use
   `../../_static/…`. Place `help-<cmd>.html` embeds under a `## otto
   <cmd> --help` section near the top of the guide page (after the intro),
   unless the task says otherwise.
2. **Move remaining user-facing prose** (flag behavior, workflows) into the
   guide page — but first check whether the guide page already covers it;
   if so, delete rather than duplicate (single source of truth).
3. **Merge design content** into the destination architecture page under
   the given heading, deduplicating against what that page already says.
   Design content keeps its graphviz diagrams.
4. **End the destination architecture page with a "Where the code lives"
   block** (a short MyST list of the modules the page's content named —
   collect the `{mod}`/`{class}` targets already cited on the page):

   ````markdown
   ## Where the code lives

   - {mod}`otto.<pkg>` — <one clause>
   ````
5. **Delete the lifecycle page** (`git rm`), remove its entry from
   `lifecycle.md`'s hidden toctree, and sweep inbound refs
   (`grep -rn "lifecycles/<name>" docs --include='*.md' --include='*.rst' | grep -v _build`),
   pointing each at the new architecture page or the guide page —
   whichever the referencing sentence is actually about.

Every task ends with the standard build gate + commit.

---

### Task 9: Dissolve `host.md` and `docker.md` (merge into existing pages)

**Files:**
- Modify: `docs/architecture/subsystems/hosts.md`,
  `docs/architecture/subsystems/docker-hosts.md`,
  `docs/guide/hosts/index.md`, `docs/guide/hosts/connections.md`,
  `docs/guide/hosts/commands/index.md`, `docs/guide/docker.md`,
  `docs/architecture/lifecycle.md` (toctree entries)
- Delete: `docs/architecture/lifecycles/host.md`,
  `docs/architecture/lifecycles/docker.md`

**Interfaces:**
- Consumes: `guide/hosts/*` docnames (Task 3), shared method above.
- Produces: `subsystems/hosts.md` gains a `## The CLI layer: verbs from
  methods` section; `subsystems/docker-hosts.md` absorbs the compose
  lifecycle. Tasks 14/15 rely on both pages being area-complete.

- [ ] **Step 1: Route `lifecycles/host.md`**

- Termynal embeds: `complete-host-ids.html` and `complete-host-verbs.html`
  → `docs/guide/hosts/index.md` (in/near its completion or command-listing
  discussion); `complete-term-backends.html` →
  `docs/guide/hosts/connections.md` (the `--term` discussion);
  `help-host.html` → `docs/guide/hosts/index.md` under a new
  `## otto host --help` section.
- Design sections "Class-scoped synthesis", "Rendering and exit codes",
  "What is unique about `host`", and the *mechanism* half of "Completion,
  scoped like the verbs" → `docs/architecture/subsystems/hosts.md`, new
  section `## The CLI layer: verbs from methods` (after the existing class
  hierarchy content). Dedupe: hosts.md already covers the class hierarchy —
  keep synthesis/rendering/completion mechanics only.

- [ ] **Step 2: Route `lifecycles/docker.md`**

- `help-docker.html` → `docs/guide/docker.md` under `## otto docker --help`.
- The compose-lifecycle/placeholder design (numbered lifecycle, ordering
  rule) → `docs/architecture/subsystems/docker-hosts.md`. That page
  already documents placeholders (lines ~68–133) — merge, don't duplicate:
  keep docker-hosts.md's framing, fold in only what's new (the compose
  up/down ordering and "what is unique about `docker`").

- [ ] **Step 3: Apply shared-method steps 4–5** ("Where the code lives"
blocks on both architecture pages; `git rm` both lifecycle pages; toctree +
inbound-ref sweep for `lifecycles/host` and `lifecycles/docker`).

- [ ] **Step 4: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs(architecture): dissolve host and docker lifecycle pages into their design pages

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Dissolve `run.md` + `test.md` → new `subsystems/execution.md`

**Files:**
- Create: `docs/architecture/subsystems/execution.md`
- Modify: `docs/guide/run/index.md`, `docs/guide/test.md`,
  `docs/architecture/lifecycle.md`
- Delete: `docs/architecture/lifecycles/run.md`,
  `docs/architecture/lifecycles/test.md`

**Interfaces:**
- Consumes: `guide/run/index` (Task 4).
- Produces: docname `architecture/subsystems/execution` — Task 14 lists it;
  Task 15 links it from the guide's run/test extending notes.

- [ ] **Step 1: Create `docs/architecture/subsystems/execution.md`**

Skeleton (fill each section by *moving* the named source content):

```markdown
# Instructions and suites — the execution pipeline

<intro: one paragraph fusing run.md's and test.md's intros — both are
"ordinary Python + a registry + a synthesized Typer subcommand";
the runner under suites is stock pytest with an otto plugin.>

## Registration synthesizes the CLI
<from run.md "What registration synthesizes" + test.md "From class to
subcommand": the options-to-parameters machinery is SHARED — say it once.>

## Handing off to pytest
<from test.md, minus the complete-suites termynal embed>

## Selection runs
<from test.md "Selection runs" — keep the two-layer completion mechanism
(ast floor + collected set); the *flag behavior* prose stays in
guide/test.md, linked>

## Non-fatal assertions
<from test.md "Non-fatal assertions" — design rationale>

## Suites vs instructions
<from test.md "Suites vs instructions" + run.md "What is unique about run">

## Where the code lives
<per shared method>
```

Both source pages' graphviz diagrams move here (test.md's pipeline digraph
survives; drop run.md's only if fully redundant with it).

- [ ] **Step 2: Route the user-facing parts**

- `complete-instructions.html` + `help-run.html` → `docs/guide/run/index.md`
  (completion demo near its subcommand/`--list` discussion; help block
  under `## otto run --help`).
- `complete-suites.html`, `complete-test-names.html`, `help-test.html` →
  `docs/guide/test.md` (same pattern). The prose around
  `complete-test-names` describing *what `--tests` matches* (base-name
  matching, parametrizations) is behavior → guide/test.md if not already
  there; the floor/collected-set mechanism is design → execution.md.

- [ ] **Step 3: Apply shared-method step 5** (delete both pages, toctree,
sweep `lifecycles/run` and `lifecycles/test` — test.md is referenced by
name from several pages, e.g. cov/monitor cross-refs).

- [ ] **Step 4: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs(architecture): fuse run+test lifecycles into subsystems/execution design page

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Dissolve `init.md` + `schema.md` → new `subsystems/bootstrap.md` + data-boundary

**Files:**
- Create: `docs/architecture/subsystems/bootstrap.md`
- Modify: `docs/architecture/subsystems/data-boundary.md`,
  `docs/guide/setup/repo-setup.md`, `docs/guide/setup/editor-schemas.md`,
  `docs/architecture/lifecycle.md`
- Delete: `docs/architecture/lifecycles/init.md`,
  `docs/architecture/lifecycles/schema.md`

**Interfaces:**
- Consumes: `guide/setup/*` (Task 2).
- Produces: docname `architecture/subsystems/bootstrap` (the
  multi-project & bootstrap design page from the spec).

- [ ] **Step 1: Create `docs/architecture/subsystems/bootstrap.md`**

```markdown
# Bootstrap and multi-project design

<intro: how otto composes a process from multiple repos — settings
discovery, sys.path, init modules. Pull the multi-project design threads
from lifecycles/init.md ("Areas, not a monolith", "The doctor is the
ingest code") and any multi-repo content currently living only in
architecture/lifecycle.md that goes deeper than the shared path needs;
lifecycle.md keeps the two-phase walk itself and links here.>

## Areas, not a monolith
<from init.md>

## The doctor is the ingest code
<from init.md — the design point: `otto init` validates with the same
code paths bootstrap uses, so the doctor can't drift from reality>

## Where the code lives
<per shared method: {mod}`otto.bootstrap`, {mod}`otto.cli.init`>
```

- [ ] **Step 2: Route the user-facing parts**

- `help-init.html` → `docs/guide/setup/repo-setup.md` under
  `## otto init --help` (repo-setup owns "what happens during project
  initialization").
- init.md's "What the scaffold teaches" — if it describes what the user
  *gets* (files, examples), it's guide content → repo-setup.md (dedupe
  against getting-started.md, which already shows `otto init` output; link
  rather than repeat).
- `help-schema.html` → `docs/guide/setup/editor-schemas.md` under
  `## otto schema --help`.
- schema.md's "What is unique about `schema`" design note → merge into
  `docs/architecture/subsystems/data-boundary.md` (the schemas are exports
  of the same boundary models — one short subsection, e.g.
  `## Exported schemas`).

- [ ] **Step 3: Apply shared-method step 5** (delete both, toctree, sweep
`lifecycles/init` + `lifecycles/schema`).

- [ ] **Step 4: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs(architecture): dissolve init+schema lifecycles into bootstrap and data-boundary pages

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Dissolve `cov.md`, `monitor.md`, `reservation.md` → three new design pages

**Files:**
- Create: `docs/architecture/subsystems/coverage.md`,
  `docs/architecture/subsystems/monitoring.md`,
  `docs/architecture/subsystems/reservations.md`
- Modify: `docs/guide/coverage.md`, `docs/guide/monitor.md`,
  `docs/guide/reservations.md`, `docs/architecture/lifecycle.md`
- Delete: `docs/architecture/lifecycles/cov.md`,
  `docs/architecture/lifecycles/monitor.md`,
  `docs/architecture/lifecycles/reservation.md`

**Interfaces:**
- Produces: docnames `architecture/subsystems/coverage`,
  `…/monitoring`, `…/reservations` for Task 14's index.

- [ ] **Step 1: `cov.md` → `subsystems/coverage.md`**

The page is already almost pure design (pipeline stages, build/counter
identity invariant, tier model). Recipe:

- New page title: `# Coverage — the collection pipeline`.
- Move everything except: the `help-cov.html` embed
  (→ `guide/coverage.md` under `## otto cov --help`).
- Dedupe "Tiers and what is committed" against `guide/coverage.md` (which
  documents tier declaration/workflow at length): architecture keeps *why
  only the manual tier is committed* and the validity-pass design
  (valid/stale/aging by blob SHA); the how-to-declare-tiers prose links to
  the guide.
- The roadmap admonition is already gone (Task 1).
- Add "Where the code lives" ({mod}`otto.coverage` fetcher → merge →
  capture → renderer → reporter chain).

- [ ] **Step 2: `monitor.md` → `subsystems/monitoring.md`**

- New page title: `# Monitoring — the observation pipeline`.
- `help-monitor.html` → `guide/monitor.md` under `## otto monitor --help`.
- Dedupe against `guide/monitor.md` (very long, user-facing): architecture
  keeps the pipeline design (collection → sessions → format:1 producer →
  dashboard hydration), guide keeps flags/dashboard usage; link both ways.
- Add "Where the code lives".

- [ ] **Step 3: `reservation.md` → `subsystems/reservations.md`**

- New page title: `# Reservations — the gate`.
- `help-reservation.html` → `guide/reservations.md` under
  `## otto reservation --help`.
- Keep "What is unique about `reservation`" as the design core; dedupe
  against guide/reservations.md.
- Add "Where the code lives" ({mod}`otto.reservations`).

- [ ] **Step 4: Apply shared-method step 5** for all three (delete, toctree,
sweep `lifecycles/cov`, `lifecycles/monitor`, `lifecycles/reservation` —
note Task 10 may have created refs to `{doc}`cov`` / `{doc}`monitor`` from
execution.md's moved content; point them at the new subsystem pages).

- [ ] **Step 5: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "docs(architecture): dissolve cov/monitor/reservation lifecycles into design pages

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: New `subsystems/network.md` (links & tunnels design)

**Files:**
- Create: `docs/architecture/subsystems/network.md`
- Modify: `docs/guide/network/link.md`, `docs/guide/network/tunnel.md`
  (hoist mechanism content, leave links)

**Interfaces:**
- Consumes: `guide/network/*` (Task 5).
- Produces: docname `architecture/subsystems/network` for Task 14's index.

- [ ] **Step 1: Read `guide/network/link.md` (524 lines) and
`guide/network/tunnel.md` (377 lines) and mark mechanism content**

Apply the behavior/mechanism rule to each section. Expected hoists (verify
while reading — hoist only what is genuinely design):

- link.md: the static-topology model (how edges are declared vs derived
  from management hops), canonical link keys, how impairment maps onto
  `tc` and where state lives.
- tunnel.md: the socat-chain design (ordered host-resident processes,
  tags, bidirectionality), how endpoints/placement are resolved, liveness
  probing.

Flag-by-flag usage, examples, and troubleshooting stay in the guide.

- [ ] **Step 2: Write `docs/architecture/subsystems/network.md`**

```markdown
# Links and tunnels

<intro: the two network models — static declared topology vs dynamic
host-resident chains — and why they are separate subsystems that share
lab data.>

## Static links
<hoisted from guide/network/link.md>

## Tunnels
<hoisted from guide/network/tunnel.md>

## Where the code lives
<{mod}`otto.link`, {mod}`otto.tunnel` — verify module paths with
`ls src/otto` before writing them>
```

Honesty rule: this page covers what exists today. If a topic has no design
story yet, omit it — no "future work" section.

- [ ] **Step 3: Replace each hoisted guide passage with one sentence +
`` {doc}`../../architecture/subsystems/network` `` link.**

- [ ] **Step 4: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs(architecture): add links-and-tunnels design page

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: Architecture — final index

**Files:**
- Modify: `docs/architecture/index.rst` (full rewrite),
  `docs/architecture/lifecycle.md` (drop the now-empty hidden toctree)
- Delete: `docs/architecture/lifecycles/` (directory should already be
  empty; `rmdir` or verify `git status` shows nothing left)

**Interfaces:**
- Consumes: every docname produced by Tasks 8–13.

- [ ] **Step 1: Verify the lifecycles directory is empty**

```bash
ls docs/architecture/lifecycles/ 2>/dev/null
```
Expected: no such directory (or empty). If pages remain, a dissolution
task was skipped — stop and finish it first.

- [ ] **Step 2: Replace `docs/architecture/index.rst` with:**

```rst
Architecture
============

These pages describe how otto is put together and why it is shaped the way
it is — written for contributors. The :doc:`User Guide <../guide/index>`
explains how to *use* each functional area; each page here explains the
moving parts behind one, and the two link across rather than repeat each
other.

Start with the overview and the shared command lifecycle, then jump to the
area you are changing. The extensibility pages describe the registry
machinery every seam shares; the utilities are the cross-cutting spines;
the principles are the recurring design rules.

.. toctree::
   :caption: Overview
   :maxdepth: 1

   overview
   lifecycle

.. toctree::
   :caption: Design by area
   :maxdepth: 1

   subsystems/hosts
   subsystems/docker-hosts
   subsystems/execution
   subsystems/network
   subsystems/monitoring
   subsystems/coverage
   subsystems/reservations
   subsystems/bootstrap
   subsystems/data-boundary

.. toctree::
   :caption: Extensibility
   :maxdepth: 1

   subsystems/registries
   subsystems/extension-points

.. toctree::
   :caption: Utilities
   :maxdepth: 1

   utilities/logging
   utilities/results

.. toctree::
   :caption: Principles
   :maxdepth: 1

   principles
```

- [ ] **Step 3: Remove the hidden `lifecycles/*` toctree from
`lifecycle.md`** (all entries were deleted by Tasks 9–12; the empty block
goes too).

- [ ] **Step 3b: "Where the code lives" completeness pass** — the spec
wants every design page to end with one. Tasks 9–13 added them to the
pages they touched; check the remaining design pages
(`subsystems/registries.md`, `subsystems/extension-points.md`,
`subsystems/data-boundary.md`, `utilities/logging.md`,
`utilities/results.md`) and append the block (same format as the shared
method's step 4) where missing, collecting the `{mod}`/`{class}` targets
each page already cites.

- [ ] **Step 4: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0, and no "not included in any toctree" warnings.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs(architecture): design-by-area index with extensibility section

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: Extensibility wiring (guide ↔ architecture)

**Files:**
- Modify: `docs/architecture/subsystems/extension-points.md`,
  `docs/guide/setup/host-database.md`, `docs/guide/reservations.md`,
  `docs/guide/run/index.md`, `docs/guide/test.md`,
  `docs/guide/extending-cli.md`
  (`docs/guide/hosts/index.md` got its Extending section in Task 3)

**Interfaces:**
- Consumes: final docnames from all prior tasks.

- [ ] **Step 1: Add a per-seam map to `extension-points.md`**

After its existing seam discussion, add a section:

```markdown
## Seams and their guides

Each seam's user-facing how-to lives in the guide:

- Connection & transfer backends — {doc}`../../guide/hosts/extending-backends`
- Embedded targets & command frames — {doc}`../../guide/hosts/extending-embedded`
- Host sources — {doc}`../../guide/setup/host-database`
- Reservation backends — {doc}`../../guide/reservations`
- Instructions, suites & options — {doc}`../../guide/run/index`, {doc}`../../guide/test`
- New top-level commands — {doc}`../../guide/extending-cli`
```

Verify each seam name against what extension-points.md already lists —
this block maps *its* seams, it doesn't invent new ones. If the page names
a seam with no guide page, link the closest guide section rather than
dropping it.

- [ ] **Step 2: Add the reverse links in the guide** — in each listed guide
page, at its extension-related section (or a new short `## Extending`
section where none exists), add one sentence:

> The registry machinery behind this seam — and every other way otto can
> be extended — is described in
> {doc}`Extension points <../../architecture/subsystems/extension-points>`.

(Adjust the relative path per page depth. Skip pages that already carry an
equivalent link; the goal is exactly one such pointer per area, not one
per paragraph.)

- [ ] **Step 3: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: wire extensibility links between guide areas and architecture

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: Audience tracks (landing page + contributing)

**Files:**
- Modify: `docs/overview.md` (three tracks), `docs/contributing.md`
  (architecture map opener)

**Interfaces:**
- Consumes: `guide/index`, `library/index`, `architecture/index` docnames.

- [ ] **Step 1: Add three tracks to `docs/overview.md`**

Near the top (after the what-is-otto intro), add:

```markdown
## Where to start

- **Drive your lab from the CLI** → the {doc}`User Guide <guide/index>`,
  one section per functional area.
- **Script otto from Python** → the {doc}`Python library <library/index>`
  section (the {doc}`API reference <api/index>` backs it).
- **Work on otto itself** → {doc}`Architecture <architecture/index>`, one
  design page per area, plus {doc}`contributing`.
```

(Check `{doc}` path prefixes from overview.md's location at docs root.)

- [ ] **Step 2: Open `docs/contributing.md` with the architecture map**

After its intro paragraph, add a short pointer paragraph: contributions
start at {doc}`architecture/index` — one design page per functional area,
mirroring the User Guide's sections; each page ends with "Where the code
lives". (One paragraph, not a duplicated list of pages.)

- [ ] **Step 3: Build gate**

Run: `uv run doc8 docs/ && uv run sphinx-build -W -b html docs/ docs/_build/html`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: audience tracks on overview + architecture map in contributing

Assisted-by: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 17: Final verification

**Files:** none new — fixes only if gates fail.

- [ ] **Step 1: Full fresh docs gate**

Run: `make docs`
Expected: exit 0 (doc8 + markdown-doctest lint + fresh `-E -a -W` HTML
build + Sphinx doctests + src doctests).

- [ ] **Step 2: Phasing grep still clean**

```bash
grep -rniE 'later phase|roadmap|planned follow|not yet implemented' \
  docs/guide docs/library docs/architecture --include='*.md' --include='*.rst'
```
Expected: no output.

- [ ] **Step 3: No stale path strings anywhere**

```bash
git grep -nE 'guide/(repo-setup|lab-config|host-database|editor-schemas|embedded|os-profiles|extending-backends|extending-embedded|library-usage|options)\.md|guide/host/|docs/cookbook|lifecycles/' \
  -- ':!docs/_build' ':!docs/superpowers' ':!todo'
```
Expected: no output. (`guide/options.md` etc. as *file paths*; hits inside
`todo/` are fine and excluded.)

- [ ] **Step 4: Structure spot-check** — open
`docs/_build/html/index.html` and confirm the sidebar shows: User Guide's
nine areas + extending-cli + cli-reference; Python library; Architecture's
four captions. Confirm `guide/hosts/index.html` renders the termynal
help block (media capture worked at the new embed depth).

- [ ] **Step 5: Run the repo's Python gate for the touched src file**

Run: `uv run nox -s lint && uv run ty check src/otto/cli/init.py`
Expected: PASS (init.py only had string edits; lint = ruff check +
format --check).

- [ ] **Step 6: Commit any fixes, then hand back for review/merge**

The branch is complete when `make docs` is green on a fresh build. Merge
decision (PR vs local merge) is the user's; use
superpowers:finishing-a-development-branch.
