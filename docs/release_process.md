# Release Process

This is the runbook for cutting an otto release. The mechanics are driven by
`make release` locally and the `release.yml` workflow on GitHub — you should
not need to edit version strings or upload artifacts by hand.

## Overview

Otto uses a static version in `pyproject.toml`, bumped by
[bump-my-version](https://callowayproject.github.io/bump-my-version/), and a
`CHANGELOG.md` regenerated from [Conventional Commits](https://www.conventionalcommits.org/)
history by [git-cliff](https://git-cliff.org/). Publishing to PyPI happens in
GitHub Actions via PyPI **trusted publishing** (OIDC) — there are no long-lived
tokens in the repository.

The distribution name is `otto-sh`; the installed CLI command is `otto`.

## Cutting a release

1. **Land all changes on `main` and push them.** Releases are cut from a
   clean `main` that matches `origin/main`. The push gate excuses only the
   release's own commits (step 3), so a commit left unpushed here would need
   its lanes stamped again on the released tree before the push.
   `release-preflight` refuses to start while any commit is unpushed.
   Commit messages should follow Conventional Commits so git-cliff can
   classify them in the changelog.

2. **Run `make release`.** This validates and prepares everything locally:

   ```bash
   make release              # git-cliff's conventional-commit census decides the version
   make release BUMP=minor   # raise the census's bump (minor / major) -- a LOWER BUMP= is refused
   make release NEW_VERSION=0.4.0rc1   # explicit version, e.g. a prerelease
   ```

   The version is decided by `scripts/release_bump.py`, which reads
   `git-cliff --bumped-version` (driven by `cliff.toml`'s `[bump]` section) as
   the census, and never lets a human override it downward: otto is pre-1.0
   and follows the Cargo/npm convention that, while major is `0`, a breaking
   (`!`) commit bumps MINOR and everything else bumps PATCH, so a plain
   `make release` always ships at least what the commit history demands. A
   `BUMP=` below the census is refused with the offending commits named;
   `NEW_VERSION=` is the prerelease escape hatch and only warns, never
   refuses, since a prerelease's exact number is the human's call. At 1.0,
   `cliff.toml`'s `[bump]` section flips to standard semver (a breaking
   change becomes a MAJOR bump).

   The census is cross-checked against `scripts/release_events.py`, the
   release event model `make check-breaking` also reads. A census that is
   only a patch bump while a breaking commit exists since the last tag is
   refused (git-cliff and the model disagree), and so is a correction since
   the last tag that does not validate (see
   [Repairing a missing mark](contributing.md#repairing-a-missing-mark)).
   `NEW_VERSION=` skips the first check and turns the second into a warning.

   `make release` runs its cheap stages first, so a cheap failure costs
   minutes rather than the hour the test stages take:

   - The **[coverage stamp](contributing.md#the-coverage-stamp)** is checked
     once, before anything is built, and the answer is kept for the coverage
     stage. A dirty tree, a non-primary venv, or a command-line variable
     other than `BUMP=`/`NEW_VERSION=`/`BUDGET_APPROVE=` stops the release
     here.
   - **`release-preflight`** refuses commits not yet pushed to the branch's
     upstream (or a branch with no upstream) and names each one; push, then
     run the release again. It also refuses untracked files under `docs/`, `src/`,
     `tests/` and `schemas/` and names each one (an untracked file under
     `src/` or a test root is usually refused one step earlier, by the
     stamp check, in its own words; the remedy is the same). The release builds and
     tests the primary checkout in place, so it would read them even though
     no commit carries them, and neither CI nor `make gate-fresh` ever sees
     them. Commit, ignore or move each one, then run the release again. The
     preflight then clears the gitignored run output the release regenerates
     anyway: `reports/*-observations`, `tests/project5/build`, and any
     `src/otto/` directory left holding only `__pycache__` (it would import
     as a namespace package). You can run `make release-preflight` by hand.
   - `clean-dist`, `web-install`, a first **docs** build (its prerequisites
     build the web assets, so a broken web build also surfaces here), the
     Python static checks and `budget-delta`.

   Then the coverage stage: when the stamp check found no valid stamp for
   HEAD's committed tree, the release runs `make coverage` itself and checks
   again — then `nox-release` (the
   **[tiered nox matrix](contributing.md#cross-version-testing-with-nox)**
   on the canary and the middle Pythons; the primary's session is what the
   stamp proves), the web build with the
   all-browser dashboard e2e and the TypeScript gate, the two matrix stages
   (`release-matrix`, `release-kmodcov-matrix` — they commit), the release
   campaign (`release-campaign` — every lane the default gates exclude, once
   each; its contents are described under
   [the last two rows are the point](architecture/quality-gates.md#the-last-two-rows-are-the-point)),
   and the docs again (this build renders the matrices the release just
   re-measured, so it must come after them); only then does it regenerate
   `CHANGELOG.md` at the new version, bump the version in `pyproject.toml`
   and `uv.lock`, commit, tag `vX.Y.Z`, and build `dist/`. It needs the dev
   VM with the lab VMs and the Zephyr guests up and **exclusive use of the
   bed** (the campaign holds it), and the campaign adds about 9 minutes
   (the chaos lanes are not in it — they are on-demand only); a
   red lane anywhere stops the release before any version commit (the two
   matrix stages may already have committed a refreshed matrix; that commit
   is correct and stays). Push it with `git push` before running the
   release again, since `release-preflight` refuses unpushed commits; the
   push gate asks no stamp for a matrix re-date (step 3).

3. **Push the tag:**

   ```bash
   git push --follow-tags
   ```

   The pre-push gate asks no stamp for the release's own commits (the matrix
   re-dates and the version bump), as long as nothing else on `main` is
   unpushed; see
   [the landing's gate is stamped](contributing.md#the-landings-gate-is-stamped).

## What the tag push triggers

Pushing a `v*` tag fires [`.github/workflows/release.yml`](https://github.com/ludachrish3/otto-sh/blob/main/.github/workflows/release.yml):

- A **guard** job inspects the tag. Only final-version tags (`vX.Y.Z`)
  proceed to PyPI. Prerelease tags (`v0.4.0rc1`, `a2`, `b1`, `.dev0`) are
  skipped here — those are for the TestPyPI dry-run workflow instead.
- **build** produces the sdist and wheel via `make wheel-check`: it builds
  the web frontends first (they ship inside the wheel for air-gapped labs),
  runs the air-gap/brand gates, then `uv build` plus embed assertions —
  including that the wheel carries the `py.typed` marker described in the
  {doc}`library docs <cookbook/python-library>`. The build backend itself refuses an asset-less wheel,
  so a bare `uv build` without the web step fails rather than publishing a
  frontend-less package. The build then goes through `make wheel-smoke`:
  the wheel is installed over otto's locked runtime dependencies alone (no
  dev group, nothing from `src/`) and `scripts/wheel_smoke.py` imports every
  module and runs `otto test` from it, with pytest's plugin autoload on and
  off. Every other lane runs an editable otto in the dev venv, where a
  development dependency can hide what an installed otto lacks. CI's
  `wheel-smoke` job runs the same target on every push.
- **publish** uploads to PyPI via OIDC, gated by the `pypi` GitHub
  Environment.
- **docs** builds the HTML documentation on a clean runner and packs the two
  tarballs that {doc}`installation` promises: `otto-docs-<version>.tar.gz`
  (live URLs) and `otto-docs-offline-<version>.tar.gz` (external documentation
  links rewritten to relative paths by
  `scripts/relativize_docs_links.py`, for readers inside an air gap). It runs
  in parallel with **build** and **publish**.
- **github-release** creates the GitHub Release, attaching the artifacts, the
  two docs tarballs, and release notes generated by git-cliff.

### If the docs job fails

`make release` builds the docs twice before it bumps the version (once among
its first stages and once after the matrices), so a docs failure normally stops
the release locally and no tag is ever cut. A failure on the
runner therefore means an environment problem there (npm, apt, the Chromium
install, a network blip), not broken documentation.

**publish does not depend on docs, deliberately** — a hiccup packing a docs
tarball must not block the one irreversible step of the release. The cost is
that such a run publishes to PyPI without creating the GitHub Release. Nothing
is stuck; recover with whichever of these fits:

1. **Re-run the failed jobs** — `gh run rerun <run-id> --failed`, or the button
   in the Actions tab. The re-run targets the same tag, and **publish** is not
   re-run because it already succeeded, so PyPI is never uploaded to twice.
   When **docs** passes, **github-release** creates the release.
2. **Create the release by hand.** The tag already exists on GitHub, so this
   creates the missing release rather than editing anything:
   `gh release create vX.Y.Z --verify-tag --notes-file <notes> dist/*.whl dist/*.tar.gz`.
3. **Attach the tarballs to an existing release** — build them locally with the
   same commands the job runs, then upload:

   ```bash
   make docs
   uv run python scripts/relativize_docs_links.py docs/_build/html docs/_build/offline
   tar -C docs/_build -czf otto-docs-X.Y.Z.tar.gz --transform 's,^html,otto,' html
   tar -C docs/_build -czf otto-docs-offline-X.Y.Z.tar.gz --transform 's,^offline,otto,' offline
   gh release upload vX.Y.Z otto-docs-*.tar.gz
   ```

Never move a pushed tag to retry a release. If a tagged commit really does
carry broken documentation, ship the fix as the next patch version.

## TestPyPI dry-run

To rehearse a release without touching production PyPI, dispatch
[`release-testpypi.yml`](https://github.com/ludachrish3/otto-sh/blob/main/.github/workflows/release-testpypi.yml)
from the Actions tab (`workflow_dispatch`). It builds and uploads to TestPyPI
via the `testpypi` environment. Prerelease versions (e.g. `0.4.0rc1`) are the
natural fit for this path.

## Documentation publishing

The hosted documentation at
[otto-sh.readthedocs.io](https://otto-sh.readthedocs.io) is **not** built or
published by GitHub Actions. Read the Docs builds it independently off its own
webhook, using
[`.readthedocs.yaml`](https://github.com/ludachrish3/otto-sh/blob/main/.readthedocs.yaml)
(`sphinx-build -W`, so warnings fail the build).

Read the Docs builds both the tracked branch and the `v*` release tags, and
serves them under separate URLs:

| URL | Serves |
| --- | --- |
| `/en/latest/` | The tracked branch. The **default** — the bare `otto-sh.readthedocs.io` redirects here. |
| `/en/stable/` | The highest release tag by semantic-version ordering (not the most recently created tag). |
| `/en/v%OTTO_VERSION%/` | That specific tag, pinned. |

A documentation change reaches `/en/latest/` as soon as it lands on the tracked
branch, independent of cutting a PyPI release; it reaches `/en/stable/` only
when a release is tagged.

Because the default is `latest`, a reader arriving at the bare URL lands on
development docs having passed no version selector. Those builds therefore carry
a dismissible banner saying so and linking to `/en/stable/`, and their page title
gains a `+dev` marker (`otto %OTTO_VERSION%+dev documentation`). Both come from
[`scripts/docs_build_stamp.py`](https://github.com/ludachrish3/otto-sh/blob/main/scripts/docs_build_stamp.py),
which classifies the build from Read the Docs' environment variables, falling
back to `git describe`. Nothing here needs doing at release time — tagging is
what flips it.
