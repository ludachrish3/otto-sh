# Make `all_hosts()` visible where users learn to write instructions and tests

**Status:** approved by Chris, 2026-11-19.

## Intent

`otto.all_hosts()` is the encouraged way to act on a lab. It yields every host
this run's fleet admits. A test or instruction then narrows that set with a
full-match regex or a list comprehension, so the same code works however many
hosts of a kind a lab holds. Today the pattern is hard to find. Its home is one
section of `writing-instructions.md`. The `otto init` scaffold never mentions
it, and the example projects almost all name single hosts with `get_host()`.
Users should meet it wherever they learn to write instructions and tests.

## Goals

- **One home page,** `docs/setup/selecting-hosts.md`, next to the instruction
  and test pages in "Set up a project". It covers:
  - what `all_hosts()` yields: the projects' fleet, with containers and
    `local` left out unless asked for (link to
    `configuration/lab/project-scope.md` for what decides the fleet);
  - `pattern=` with full-match regexes;
  - list-comprehension recipes:
    - by host kind (`isinstance(h, UnixHost)`);
    - by element (`h.element.name`);
    - by element metadata;
    - splitting the fleet into groups;
    - combining with `pattern=`;
  - `run_on_all_hosts` / `do_for_all_hosts` for fanning work out;
  - the two loud failures, an empty fleet and a pattern that matches nothing;
  - `get_host()` as the other half, for when you mean one specific host.
- **Visible where instructions and tests are taught:**
  - `writing-instructions.md`, `writing-tests.md` and `test-recipes.md` each
    show an `all_hosts()` walk with a filter, and link to the home page.
  - `writing-instructions.md`'s existing section is trimmed so it points at
    the home page.
- **The `otto init` scaffold:**
  - the instructions template gains a commented example instruction that
    walks `all_hosts()` and filters it;
  - the conftest keeps its `get_host("example-device")` fixture and gains a
    fleet fixture beside it.

  Both stay commented until the lab is real, as today.
- **Example projects show a mix.** Code that really means "every host of a
  kind" switches to `all_hosts()` plus a filter. Code that means one
  particular device keeps `get_host()`. Each example project ends up showing
  both.

## Non-goals

- No API change. `pattern=` and comprehensions are the filtering story
  (Chris, 2026-11-19).
- `docs/overview.md` and the getting-started path are not changed (Chris,
  2026-11-19).
- `get_host()` is not removed or discouraged anywhere it is the right call
  (Chris, 2026-11-19).

## Constraints

- Docs principle: one home per topic, link, never restate. The full
  explanation lives only on `selecting-hosts.md`.
- Docs code blocks are checked by the repo's docs linters and the API
  teaching check (`make check-api-teaching`, `scripts/lint_markdown_doctests.py`).
  New examples must pass them, including keyword-only call shapes.
- Example projects are downloadable archives, and some have captured output
  (`docs/examples/captures.toml`). A changed example re-captures on the bed,
  through that file's own mechanism.
- The scaffold templates are pinned by the init tests. Uncommented scaffold
  code must run against the placeholder lab, and `all_hosts()` raises on an
  empty fleet, so fleet examples stay commented.

## Approaches

1. **One home page, linked from the teaching pages (chosen).** It follows the
   one-home rule, and each teaching page shows the pattern in its own example.
2. **The full explanation on every page that uses it.** Rejected: it breaks
   the one-home rule, and the copies drift.
3. **Lead with it on the overview and getting-started pages.** Rejected by
   Chris: visible where instructions and tests are taught, not on the main
   overview.

## Left to the builder

- Exact page wording and the recipes' host and element names (use the
  example labs' real ones where a block is checked against them).
- Which example call sites switch, applying the "every host of a kind vs one
  device" rule and keeping both forms in each project.
- Where the new page sits in `docs/setup/index.md`'s toctree, next to the
  instruction and test pages.
