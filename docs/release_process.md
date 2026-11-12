# Release Process

This is the runbook for cutting an otto release. The mechanics are driven by
`make release` and `make release-tag` locally and the `release.yml` workflow on GitHub — you should
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

`main` takes no direct push, so a release lands like any change: through a
pull request ([Gating a branch before it
lands](contributing.md#gating-a-branch-before-it-lands)). It takes four steps
and, end to end, about two hours: `make release` (about an hour), the release
pull request's CI (about 25 minutes) and the merge, then `make release-tag`,
which waits the 25 minutes of `main`'s own CI run before it tags.

1. **Branch from `main`'s tip and push the branch.**

   ```bash
   git fetch origin main
   git checkout -b release/next FETCH_HEAD
   git push -u origin release/next
   ```

   The version is not known yet, so the branch name does not carry it. The
   push is a tree identical to `main`'s, which the pre-push hook passes with
   an empty range. `release-preflight` refuses to start anywhere else (see
   below). Commit messages since the last tag should follow Conventional
   Commits so git-cliff can classify them in the changelog. A landing never
   writes a coverage stamp, so run `make coverage` on this tip now if you
   would rather it not run inside `make release`
   ([the coverage stamp](contributing.md#the-coverage-stamp)).

2. **Run `make release`.** This validates and prepares everything locally,
   then opens the release pull request:

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

   The census is cross-checked against the release event model in
   `scripts/release_events.py`. A census that is
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
     other than `BUMP=`/`NEW_VERSION=` stops the release
     here.
   - **`release-preflight`** refuses four things, naming what it found:
     `main` or a detached `HEAD` (the release lands through a pull request,
     so it needs a branch); commits not yet pushed to the branch's upstream,
     or a branch with no upstream; a branch that is not `main`'s tip, fetched
     (the release certifies the tree `main` will carry once its pull request
     merges, so it starts from `main` exactly); and untracked files under
     `docs/`, `src/`, `tests/` and `schemas/` (an untracked file under
     `src/` or a test root is usually refused one step earlier, by the stamp
     check, in its own words; the remedy is the same). The release builds and
     tests the checkout in place, so it would read them even though no commit
     carries them, and CI never sees them. Commit, ignore or move each one,
     then run the release again. The preflight then clears the gitignored
     run output the release regenerates anyway: `reports/*-observations`,
     `tests/project5/build`, `tests/project_ctr/build`, and any `src/otto/`
     directory left holding only `__pycache__` (it would import as a
     namespace package). Last, it pins the live `main` ruleset (15843311)
     and the repository's merge settings against the committed expectation
     (`tests/unit/test_landing_ruleset.py`), so it needs `gh` auth and the
     network. You can run `make release-preflight` by hand.
   - The run then writes its **release record**,
     `reports/release/record.json`, as `pending`: a run that fails anywhere
     leaves a pending record, never an older certificate.
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
   and `uv.lock` and commit (no tag: the tag waits for the merge), and build
   `dist/` (`wheel-check`). It needs the dev
   VM with the lab VMs and the Zephyr guests up and **exclusive use of the
   bed** (the campaign holds it), and the campaign adds about 9 minutes
   (the chaos lanes are not in it — they are on-demand only); a
   red lane before the bump stops the release before any version commit (the
   two matrix stages may already have committed a refreshed matrix on the
   branch; that commit is correct, but `release-preflight` refuses a branch
   that is not `main`'s tip, so land it through its own pull request and
   start the release again from the new tip). `wheel-check` is the one stage
   after the bump: a red `wheel-check` leaves the bump committed on the
   release branch, unpushed, with the record `pending`. That bump never
   reached `main`, so it is not a case for [the recertification
   run](#release-recertification), which recovers a bump `main`
   already carries: reset the branch to `main`'s tip (`git reset --hard
   origin/main`), land the fix for what `wheel-check` reported through its
   own pull request, and start the release again; the census bumps to the
   same version.

   On green the run certifies its head in the record (the commit, its tree
   and the version) and lands the branch like any branch: `make gate-tip`
   (usually `make check-breaking` alone, since the re-dated matrices and the
   bump stamp nothing; a matrix whose verdicts improved earns the static
   lanes), `git push -u origin <branch>`, `make gate-status`, and `gh pr
   create --title "chore(release): vX.Y.Z"` with the changelog section as its
   body. It records the pull request's URL (the record's `landed` state) and
   prints `opened <url>: rebase-merge it, then run: make release-tag`.

3. **Merge the release pull request.** The maintainer reviews the diff (the
   changelog, the bump, the re-measured matrices) and presses **Rebase and
   merge** once its CI and the `local-gate` status are green. Nothing else
   should land while the release pull request is open, and until the tag is
   pushed: `make release-tag` tags only the tree the run certified.

4. **Run `make release-tag`** from the same checkout. It tags `main`'s tip
   and pushes the tag, which fires the release workflow (below). It
   refuses, in this order, unless:

   - the record is `certified`, `landed` or `tip-certified` (a missing
     record, or a `pending` one, which is a run that never finished, is
     refused before anything touches the remote);
   - `main`'s tip tree is the record's tree (a rebase-merge changes the
     commits, never the tree, so another tree means something else landed)
     and its `pyproject.toml` carries the record's version;
   - `main`'s CI on that tip is green, judged by the same verdict the release
     workflow's guard reads; it waits while the run is pending, up to 70
     minutes, and refuses naming the run's URL on expiry;
   - `main` did not move during the wait: CI certifies a commit, not a tree,
     so a new tip with the same tree is still an unchecked commit.

   The tag's name and message are `[tool.bumpversion]`'s `tag_name` and
   `tag_message` in `pyproject.toml`. When an earlier run tagged and then
   failed to push, the local tag is pushed again if it names `main`'s tip
   with that message; a local tag of that name on anything else is refused,
   with `git tag -d` as the remedy.

(release-recertification)=

**Recovery: the recertification run.** If something else landed between
`make release` and the merge, `main`'s tip tree is not the record's and
`make release-tag` refuses. The bump is already in `main`'s history,
untagged, so the release cannot simply be repeated. Branch from `main`'s tip
again (step 1, under another name such as `release/recert`) and run `make
release`: it sees that the remote has no tag for the current version and that
the census names that same version, and runs in **recertification mode**:
every validation stage as usual, the matrices re-measured and committed when
they changed, the changelog regenerated for the same version and committed
when it changed, and **no bump**. When the run committed nothing, there is
nothing to merge: it certifies `main`'s tip itself (`tip-certified`) and
prints `main's tip is certified; run: make release-tag`. When it committed,
the branch lands through its pull request as in step 2, and `make
release-tag` follows the merge. A census above the current version (a
breaking change landed between the bump and the merge) runs an ordinary
release that bumps again; a census below it is refused.

A [hotfix](contributing.md#hotfix) that leaves `main`'s CI red blocks the
release until its follow-up lands: `make release-tag` and the release
workflow's guard both require green CI on the commit they tag.

## What the tag push triggers

Pushing a `v*` tag fires [`.github/workflows/release.yml`](https://github.com/ludachrish3/otto-sh/blob/main/.github/workflows/release.yml):

- A **guard** job inspects the tag. Only final-version tags (`vX.Y.Z`)
  proceed to PyPI. Prerelease tags (`v0.4.0rc1`, `a2`, `b1`, `.dev0`) are
  skipped here — those are for the TestPyPI dry-run workflow instead. For a
  final tag it then checks the landing: the tag's commit must be on `main`
  (an ancestor of `origin/main`), and `main`'s CI on that commit must be
  green by the same verdict `make release-tag` waits for
  (`scripts/local_gate_status.py ci-verdict`: every job
  `.github/required_contexts.json` lists, less `local-gate` and
  `gate-admission`, which judge pull requests). A tag made by hand off `main`, or on a
  commit whose CI is red or pending, publishes nothing.
- **build** produces the sdist and wheel via `make wheel-check`: it builds
  the web frontends first (they ship inside the wheel for air-gapped labs),
  runs the air-gap/brand gates, then `uv build` plus embed assertions —
  including that the wheel carries the `py.typed` marker described in the
  {doc}`library docs <extend/python-library>`. The build backend itself refuses an asset-less wheel,
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
