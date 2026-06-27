# Release-target cleanup + publishing-docs scrub — design

**Date:** 2026-06-27
**Status:** Approved, ready for implementation plan

## Problem

The Makefile carries two `uv publish` "manual fallback" targets (`publish`,
`publish-test`) that duplicate what GitHub Actions already does via OIDC
trusted publishing. In practice only `make release` is used: it prepares a
release locally, and pushing the resulting `v*` tag fires
`.github/workflows/release.yml`, which builds, publishes to PyPI, and creates
the GitHub Release. The redundant targets (and the docs that promote them) add
maintenance surface and present a manual path nobody takes.

Separately, the docs describing publishing are inaccurate in two ways:

1. They imply (or invite the assumption) that GitHub Actions publishes the
   documentation. It does not — ReadTheDocs builds independently off its own
   webhook via `.readthedocs.yaml`.
2. `docs/contributing.md` still references a `make test` target (and a
   `TESTS=<kw>` filter) that no longer exists on `main`.

## Goals

- Remove the unused manual-publish Makefile targets and any references that
  promote them.
- Make `docs/release_process.md` accurately describe how each release artifact
  is published (PyPI + GitHub Release via Actions on tag push; docs via RTD's
  own webhook — not Actions).
- Fix the stale `make test` references in `docs/contributing.md`.

## Non-goals

- No changes to the `release.yml`, `release-testpypi.yml`, `ci.yml`, or
  `nightly.yml` workflows — they are correct; only the prose describing them
  changes.
- Keep the `changelog` target (it is a genuine standalone tool, referenced by
  a comment in `pyproject.toml`, and is not a publish target).
- No changes to `getting-started.md` / `README.md` — their publishing
  references are already accurate.
- No broader documentation audit beyond the specific stale references called
  out here.

## Changes

### 1. Makefile

- **Delete** the `publish-test` target (currently ~L122–125) and the `publish`
  target (~L127–129).
- **Edit `.PHONY`** (L3): remove `publish-test` and `publish`; also remove the
  stale `test` entry (no `test:` recipe exists).
- **Trim the trailing `echo` block of the `release` recipe** (~L111–120). It
  currently prints tag-push guidance *plus* a "Manual fallbacks (require
  UV_PUBLISH_TOKEN)" block that names the two removed targets. The new ending
  keeps the tag-push guidance, names all three artifacts the tag push produces,
  and points at the TestPyPI dry-run workflow as the rehearsal path. It drops
  the `UV_PUBLISH_TOKEN` manual-fallback lines. Target wording:

  ```
  Pushing the tag fires .github/workflows/release.yml, which builds,
  publishes to PyPI via OIDC (gated by the 'pypi' environment), and
  creates the GitHub Release.
  Push with:
      git push --follow-tags

  To rehearse first, dispatch release-testpypi.yml from the Actions tab.
  ```

- The `release` doc-comment header block (the `## release: ...` help line and
  the `VENV_BIN` comment at L9–17), the `changelog` target, and all other
  targets remain untouched.

### 2. docs/release_process.md

- **Delete the "Manual fallbacks" section** (the `make publish-test` /
  `make publish` runbook, ~L66–75).
- **Add a short framing of how each artifact is published** so the runbook is
  accurate end-to-end:
  - **PyPI** and the **GitHub Release** are produced by `release.yml` on a
    `v*` tag push (PyPI via OIDC trusted publishing, gated by the `pypi`
    GitHub Environment).
  - **Documentation** is built and hosted by **ReadTheDocs**, which builds
    independently off its own webhook via `.readthedocs.yaml`
    (`fail_on_warning: true`). It is **not** driven by GitHub Actions and is
    not tied to the `v*` tag — it tracks the configured branch/versions.
- Keep the "Overview", "Cutting a release", "What the tag push triggers", and
  "TestPyPI dry-run" sections (accurate as-is).

### 3. docs/contributing.md

In the "Running tests" block (~L246–249) and the trailing note (~L268):

- Replace `make test` ("run all tests") with `make coverage` (the full gated
  run).
- Replace keyword filtering via `make test TESTS=<kw>` with
  `uv run pytest -k <kw>` (the `TESTS=` filter no longer exists). Apply in both
  the code block and the trailing prose sentence.

## Verification

- `make help` renders without the removed targets and without errors.
- `grep -n 'publish-test\|^publish:' Makefile` returns nothing.
- `make docs` is green (doc8 + Sphinx `-W` + doctests) — confirms the edited
  Markdown still builds clean under the warnings-as-errors gate.
- No remaining `make test` references in `docs/contributing.md`.
- A dry read of `docs/release_process.md` confirms no surviving mention of
  `make publish` / `make publish-test`, and the RTD-builds-independently note
  is present.

## Risks

Low. All changes are to a Makefile (removing unused recipes) and Markdown
prose. The only gate that can fail is the docs build (`-W`), which the
verification step exercises directly.
