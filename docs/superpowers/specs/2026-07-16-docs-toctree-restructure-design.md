# Docs toctree restructure — functional areas, design-only architecture, discoverable extensibility

**Date:** 2026-07-16
**Status:** Approved design, pending implementation plan

## Goal

Reduce toctree sprawl and make the docs tell a story of how to use otto. Three
moves:

1. **User Guide** regroups its 21 flat pages into functional areas that
   correlate to the first-party commands.
2. **Architecture** becomes design-only — one design page per functional
   area, mirroring the guide, written for contributors. User-facing content
   moves out.
3. **Extensibility** becomes discoverable: each guide area describes its own
   seams; one architecture section owns the registry mechanics; the two link
   to each other.

The API docs are unchanged.

## Editorial rules (apply to every page touched)

These are the tests the page-by-page audit applies:

- **Single source of truth.** One canonical page per topic; every other
  mention is a link, not a restatement. Duplication is allowed only where a
  sentence of context saves the reader a round-trip, and never more than a
  sentence or two.
- **Behavior vs. mechanism.** Flags, workflows, and examples live only in the
  User Guide. Why a subsystem is shaped the way it is — its moving parts,
  invariants, and trade-offs — lives only in Architecture. Each side links
  across; neither paraphrases the other.
- **Thin landing pages.** A *new* umbrella index (`setup/index`,
  `network/index`) orients (a paragraph and a worked example) and routes; it
  does not summarize its children. Where an area is one command, the command's
  existing page simply becomes the index (`run/index`, `hosts/index`) — no
  extra layer.
- **Extensibility placement.** A guide area's "Extending" material covers only
  what is specific to that seam (what you register, a snippet). The shared
  registry/init-module mechanics live once, in Architecture → Extensibility,
  and are linked, never restated.
- **Docs capture the current state of otto.** No implementation phasing
  ("a later phase", "this phase"), no roadmap/backlog references (`todo/`
  pointers, "planned follow-up work", "lives on the roadmap"), no dreams of
  future features, no past direction changes. A functional area genuinely
  under construction may be marked in-progress — but as a statement of what
  works today, not a promise of what is coming. Where a page says "X is a
  later phase", either delete the sentence or restate it as current fact
  ("X is not supported"). *Not* covered by this rule: design terms that
  happen to use the word "phase" (the two-phase bootstrap), placeholder
  objects as a mechanism (Docker placeholder hosts), and CLI features named
  "Next steps".

## Top-level toctree

```text
index.rst
├─ overview
├─ getting-started
├─ guide/index            User Guide
├─ library/index          Python library          ← NEW (library-usage + cookbook)
├─ architecture/index     Architecture
├─ contributing
├─ release_process
└─ api/index
```

The cookbook disappears as a top level: `guide/library-usage.md` becomes the
`library/` section index and the four cookbook recipes move under it. One
front door for library developers.

`index.rst`/`overview` open with three explicit tracks: *drive your lab from
the CLI → User Guide*, *script it in Python → Python library*, *work on
otto → Architecture*.

## User Guide target tree

Structure choice: per-area subdirectories with index landing pages
(generalizing the existing `guide/host/` pattern). **No redirects** — nothing
external deep-links the published docs yet. Areas with a single page stay as
flat pages at the guide root; a directory is only created where there are
multiple pages to group.

```text
guide/index.rst  (ordered as a journey: set up → hosts → automate → network → observe → share)
├─ setup/index              Project setup            (otto init)
│  ├─ repo-setup            ← guide/repo-setup.md
│  ├─ lab-config            ← guide/lab-config.md    ← canonical home for host definitions
│  ├─ host-database         ← guide/host-database.md (pluggable host sources)
│  └─ editor-schemas        ← guide/editor-schemas.md
├─ hosts/index              Hosts                    (otto host)
│  ├─ commands/…            ← guide/host/commands/…
│  ├─ capabilities          ← guide/host/capabilities.md
│  ├─ connections           ← guide/host/connections.md
│  ├─ configuration         ← guide/host/configuration.md
│  ├─ embedded              ← guide/embedded.md
│  ├─ os-profiles           ← guide/os-profiles.md
│  ├─ extending-backends    ← guide/extending-backends.md
│  └─ extending-embedded    ← guide/extending-embedded.md
├─ run/index                Running instructions     (otto run)   ← guide/run.md as index
│  └─ options               ← guide/options.md       ← canonical home; test links here
├─ test                     Running test suites      (otto test)  ← guide/test.md
├─ docker                   Docker containers        (otto docker)
├─ network/index            Links & tunnels          (otto link / otto tunnel) ← NEW thin index
│  ├─ link                  ← guide/link.md
│  └─ tunnel                ← guide/tunnel.md
├─ monitor                  Monitoring               (otto monitor)
├─ coverage                 Coverage                 (otto cov)
├─ reservations             Reservations             (otto reservation)
├─ extending-cli            Extending the CLI        (new top-level commands)
└─ cli-reference            CLI reference            (flat lookup surface, unchanged)
```

Notes:

- **Lab host definitions**: canonical under Project setup (`setup/lab-config`).
  `hosts/index` opens with one line + link: hosts are *defined* in `lab.json`
  (→ lab-config); this section is about *using* them. Sphinx permits a page in
  only one toctree, so link-from-the-other-section is the mechanism.
- **`network/index`** is a genuinely useful thin page: static links vs.
  tunnels, when to reach for each, then routes to the two pages.
- **`options`** is canonical under Running instructions (it is introduced
  there first); `test` links to it rather than restating.
- **`extending-cli`** stays a root-level guide page: registering new top-level
  commands is the CLI's own seam, belonging to neither run nor test.
- **Monitoring and Reservations** are their own areas — both are first-party
  commands and fold into no other area.
- `guide/index` gets a small *command → section* table so `otto <cmd> --help`
  users can jump straight to the right area.

## Architecture target tree

```text
architecture/index.rst
├─ overview                                   (keep)
├─ lifecycle                ← lifecycles/index.md — the shared path only
├─ Design by area (caption)
│  ├─ subsystems/hosts             (absorbs design content of lifecycles/host.md)
│  ├─ subsystems/docker-hosts      (absorbs lifecycles/docker.md)
│  ├─ subsystems/execution         ← NEW: instruction & suite execution
│  │                                  (from lifecycles/run.md + lifecycles/test.md)
│  ├─ subsystems/network           ← NEW: links & tunnels design (currently no page; seed
│  │                                  from what exists, stub the gaps honestly)
│  ├─ subsystems/monitoring        ← from lifecycles/monitor.md
│  ├─ subsystems/coverage          ← from lifecycles/cov.md
│  ├─ subsystems/reservations      ← from lifecycles/reservation.md
│  ├─ subsystems/bootstrap         ← NEW: multi-project & bootstrap (from lifecycles/init.md)
│  └─ subsystems/data-boundary     (keep; absorbs design content of lifecycles/schema.md)
├─ Extensibility (caption)          ← THE single extensibility section
│  ├─ subsystems/registries        (keep — the engine)
│  └─ subsystems/extension-points  (keep — the seam map)
├─ Utilities (caption): logging, results       (keep)
└─ principles                                  (keep)
```

- The **per-command lifecycles subtree dissolves**. Each page is audited:
  user-facing content (flags, workflows) moves to the matching guide area;
  design content merges into the per-area design page; the lifecycle page is
  then deleted. Only the shared command-lifecycle page survives, as
  `architecture/lifecycle`, because the entry/bootstrap/dispatch/teardown path
  is genuinely design.
- **Registries and extension-points stay two pages** under one Extensibility
  caption — each is already coherent; merging would create one long page for
  no dedup gain.
- Each design page ends with a short **"where the code lives"** block
  (module pointers), formalizing what several pages already do informally.

## Extensibility wiring

- Each guide area gains (or keeps) an "Extending" subsection/page covering the
  seam-specific how-to: hosts (backends, embedded targets), setup
  (host-database sources), reservations (backends), run/test (instructions,
  suites, options), plus the root `extending-cli` page.
- Every such subsection links to Architecture → Extensibility for mechanics;
  `extension-points` links back to each guide-side how-to per seam.
- Result: users discover seams where they work; contributors get one home for
  the machinery.

## Audience improvements (in scope)

- **CLI users**: area landing pages lead with a runnable example before
  concepts; command → section table on `guide/index`; `cli-reference` remains
  a flat lookup page.
- **Library developers**: the new `library/` section is their single front
  door; API docs unchanged.
- **Contributors**: guide ↔ architecture mirror by area; "where the code
  lives" blocks; `contributing.md` opens with a map of the architecture
  section instead of assuming readers find it.

## Out of scope

- API docs (`api/`) — unchanged.
- URL redirects — explicitly skipped (nothing deep-links yet).
- Rewriting page prose beyond what the audit rules require — this is a
  restructure, not a rewrite.

## Current-state sweep — known offenders (2026-07-16)

Found by grep; the audit fixes these and stays alert for phrasing the grep
missed.

Must fix (phasing/roadmap leakage):

- `architecture/lifecycles/cov.md:9-15` — "Roadmap items pending" admonition
  referencing `todo/coverage_roadmap.md` and "the plan's later phases".
- `guide/coverage.md:396-399` — "a later phase … nothing this phase can clean
  yet"; `guide/coverage.md:667` — "planned follow-up work".
- `guide/tunnel.md:278-280` — "This phase keeps `otto.tunnel` monitor-free;
  wiring … is a later phase".
- `guide/monitor.md:380` — "(storing edges, topology views) is a later phase".
- `guide/cli-reference.md:341` — "embedded reset is a later phase".
- `guide/reservations.md:235` — "lives on the roadmap".

Reword as current fact:

- `guide/link.md:322` — "none is planned" → describe current behavior only.
- `guide/embedded.md:140` — "Reserved; not yet implemented" → "not supported".
- `guide/monitor.md:761` — PEN placeholder note → keep, phrased as fact.

Confirmed false positives (leave alone): bootstrap "phase 1/phase 2"
(two-phase composition root — a design term), Docker "placeholder" hosts (a
mechanism), the "Next steps" CLI output feature.

## Migration & verification notes

- Sweep every `{doc}`/`{ref}` cross-reference after file moves; Sphinx builds
  with nitpicky `-W`, so a broken ref fails the build — that is the gate.
- `make docs` runs Playwright captures against the built web dist; run it (not
  a bare sphinx-build) to certify.
- Move pages with `git mv` to preserve history.
- The lifecycles audit is the only content-splitting work; everything else is
  moves plus new thin index pages (`setup/index`, `network/index`,
  `library/index` adaptation) and link insertion.
