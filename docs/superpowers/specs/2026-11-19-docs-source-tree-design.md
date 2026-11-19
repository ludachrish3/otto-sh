# One answer to "which docs files are hand-written source"

**Status:** approved by Chris, 2026-11-19. Fixes #1188.

## Intent

Several checkers walk `docs/` and judge the pages they find. Each one decides
for itself which files to skip, so the decision is restated in nine places:

- `scripts/api_docs_blocks.py`
- `scripts/lint_markdown_doctests.py`
- `scripts/lint_docs_versions.py`
- `scripts/lint_docs_citations.py`
- `tests/unit/docs/test_docs_images_are_local.py`
- `tests/unit/docs/test_no_hand_counted_strip.py`

`tests/unit/tunnel/test_docs_hosts_examples_resolve.py`,
`tests/unit/api_snapshot/test_hook_renames.py` and
`tests/unit/docs/test_sphinx_srcs_cover_included_sources.py` are the other
three; review found the last two.

None of them knows the git-ignored pages that renderers write at build time.
After a local `make docs`, `make check-api-teaching` therefore judges the
release-notes pages. Those pages quote commit messages, which may name APIs
that no longer exist, and the check fails. CI never builds the docs before
that check, so only local runs fail.

## Goals

- **One module owns the answer: `scripts/docs_tree.py`.** It holds:
  - the never-source directories: `_build` (build output), `superpowers`
    (archived specs and plans) and `_inventories` (vendored);
  - the registry of rendered pages. This is `docs/conf.py`'s
    `_RENDERED_PAGES`, moved here; `conf.py` imports it, so the table stays
    the one list. Each entry names the renderer module and the docs-relative
    output the renderer owns. The release-notes renderer owns the whole
    `release-notes/` directory.
  - one walker, `docs_sources(root, *, suffixes, also_skip)`. It walks a
    docs root and yields the hand-written source files under it.
- **Every checker calls the walker.** Each checker-specific extra goes in
  `also_skip`, with its reason stated at the call site:
  - `examples/`, for the citations lint and the `--hosts` guard;
  - `_static/` and `_templates/`, for the citations lint.
- **Every checker skips the rendered pages** (Chris, 2026-11-19). A checker
  judges what a person writes. A rendered page is judged by its renderer's
  own tests, and its fix belongs in the renderer's source, not in the page.

## Non-goals

- Sphinx's `exclude_patterns` is not changed. It decides what Sphinx builds,
  which is a different question.
- Committed rendered pages, such as `docs/cli/host/families.md` and
  `docs/command-contracts/`, are still found as source. They are in git and
  reviewed as diffs.
- No checker's own rules change. That includes rules about a page's content:
  the citations lint still exempts a page whose first heading carries a
  `GENERATED` header, which is how it treats those committed rendered pages.
- Walks that answer a different question stay as they are. That covers
  `git ls-files` scans, which never see a git-ignored page; doc8, which
  checks `.rst` only, while every rendered page is `.md`; `SPHINX_SRCS`; and
  scans of the built HTML.

## Constraints

- The walker works on any root it is given, relative to that root. The lints
  take their docs directory as an argument, and their tests run on temporary
  trees. A checker's own `also_skip` entries are relative to that root too: an
  entry ending in `/` names a directory, and an entry with a suffix names a
  file. A bare name is refused. `examples/` therefore means `docs/examples/`;
  the `--hosts` guard used to skip a directory of that name at any depth, and
  no such nested directory exists.
- `.gitignore` names each rendered output by hand. A test asks git itself
  (`git check-ignore`) whether each registry output is ignored, and fails if
  one is not. This is one-directional: an ignored path the registry doesn't
  name is simply not a rendered page.
- AGENTS.md "one decision, one place": every site above moves in this change.

## Approaches

1. **One module, every checker on it, every checker skipping rendered pages
   (chosen).**
2. **Only the teaching check skips rendered pages.** Rejected: the other
   checkers would keep a second, narrower list, which is the defect being
   fixed.
3. **Add `release-notes` to the teaching check's tuple.** Rejected: it fixes
   one copy of the decision and leaves five.

## Left to the builder

- The registry entry's exact shape.
- How the walker matches skips: whole path parts or docs-relative prefixes.
  It must stay correct for every current caller.
