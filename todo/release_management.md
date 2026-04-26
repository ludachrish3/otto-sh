# Otto Release Management — Phased Plan

## Context

Chris wants to get comfortable with release management for `otto` before the code is actually ready for public release. The ask spans CI, PyPI publishing, MIT licensing, version bumping, multi-Python testing, and stability/race-condition hardening of host sessions. The goal of this plan is to stage the work so each phase is independently valuable and so the first real PyPI upload (Phase 4) is rehearsed, not discovered.

Current state (verified):

- Build backend is `uv_build`; version is static `"0.1.0"` in [pyproject.toml](../pyproject.toml); runtime lookup via `importlib.metadata.version()` in [src/otto/version.py](../src/otto/version.py).
- Only Python 3.10 is tested (`.python-version`, `requires-python = ">=3.10"`).
- No `LICENSE` file, no `license` field in pyproject.toml, no `.github/` directory.
- Makefile's `all` target does `clean-dist → typecheck → coverage → docs → build`. Coverage threshold 85%.
- **Otto has no internal stability/concurrency test coverage today.** The existing [tests/unit/suite/test_stability.py](../tests/unit/suite/test_stability.py) and [tests/unit/suite/test_stability_e2e.py](../tests/unit/suite/test_stability_e2e.py) cover otto's ability to run stability tests against *other* products (the `--iterations`/`--duration` suite feature) — they are not soak tests of otto itself. The only targeted concurrency test of otto internals is [tests/unit/host/test_remoteHost.py](../tests/unit/host/test_remoteHost.py) (`test_oneshot_telnet_concurrent_does_not_deadlock`). This strengthens the case for Phase 3's dedicated `test_session_concurrency.py`.
- Known race-condition hotspots in [src/otto/host/session.py](../src/otto/host/session.py): `SessionManager._oneshot_pool` (free-list, unsynchronized), `_named_sessions` dict (alive-check + use is not atomic across awaits), `_ensure_session()` default-session recreation.

---

## Phase 1 — Legal, identity, and CI foundation

Goal: otto is legally distributable and every push runs through automation. No publishing, no version churn.

### Changes

- ✅ **Created** [LICENSE](../LICENSE) — verbatim MIT text, `Copyright (c) 2026 Chris Collins`.
- ✅ **Modified** [pyproject.toml](../pyproject.toml):
  - ✅ `name` changed from `otto` → `otto-sh` (done ahead of Phase 4 to avoid churn; see "Name decision" below). Required a `[tool.uv.build-backend] module-name = "otto"` override so the build backend still finds [src/otto/](../src/otto/).
  - ✅ Replaced the placeholder `description`.
  - ✅ Added `license = "MIT"` (PEP 639 SPDX form).
  - ✅ Added `license-files = ["LICENSE"]`.
  - ✅ Added `authors`, `keywords`, `classifiers` (`License :: OSI Approved :: MIT License`, `Programming Language :: Python :: 3.10`, `Development Status :: 3 - Alpha`, `Framework :: AsyncIO`, plus `Environment :: Console`, `Intended Audience :: Developers`, `Operating System :: POSIX :: Linux`, `Topic :: Software Development :: Testing`, `Topic :: System :: Systems Administration`).
  - ✅ Added `[project.urls]` (Homepage, Source, Issues, Changelog). **Documentation URL deferred** until RTD project is actually published — add once `https://otto.readthedocs.io` resolves.
- ✅ **Created** [.github/workflows/ci.yml](../.github/workflows/ci.yml) — on push/PR: checkout, `astral-sh/setup-uv@v6` (has built-in cache), `uv sync --all-extras --dev`, **`make ci`** (not `make all`). Single job on 3.10 for now; matrix lands in Phase 3.
- ✅ **Modified** [Makefile](../Makefile) — split the pipeline along the VM-availability axis: `make all` (dev VM, runs everything including `integration`/`hops` markers, 85% threshold) vs `make ci` (GitHub Actions, unit-only via `tests/unit -m "not integration and not hops"`, separate `CI_COVERAGE_THRESHOLD := 80` since unit-only coverage floor measured at 81% with the current test suite). New helper target `coverage-unit` carries the marker filter and threshold; `coverage` is unchanged. **Why two thresholds:** integration tests cover real session/transport/transfer paths the unit suite can't reach without VMs, so dropping them mechanically reduces achievable coverage. Two thresholds make the dev-VM and CI contracts independently meaningful instead of dragging the dev-VM bar down to match CI.
- ✅ **Created** [.github/dependabot.yml](../.github/dependabot.yml) — Dependabot is a GitHub bot that opens PRs bumping dependencies whenever a newer version is published (and flags known CVEs). The `weekly` cadence means it checks once a week and opens at most one PR per stale dep that week (daily is noisier, monthly risks missing security patches). Two ecosystems configured: **`uv`** (Dependabot gained native uv support GA March 2025 — uses `uv.lock` directly; the older `pip` ecosystem still works for uv projects but `uv` is now preferred) and `github-actions` (so `setup-uv@v6`, `checkout@v4`, etc. stay current). Each PR triggers CI, so upgrades that break something surface as red CI before you merge.
- ✅ **Created** [CHANGELOG.md](../CHANGELOG.md) — seeded with an "Unreleased" section using Keep a Changelog format.
- ✅ **Created** [CONTRIBUTING.md](../CONTRIBUTING.md) at repo root — a stub pointing contributors at [docs/contributing.md](../docs/contributing.md) (the detailed guide that's part of the Sphinx build) and at the `make all` Makefile contract. Root-level placement is what GitHub auto-discovers for PR/issue UI surfacing; the docs/ version stays put because it's referenced from [docs/index.rst](../docs/index.rst).

### Rationale

- **GitHub Actions** via `astral-sh/setup-uv@v6` is the uv-native "forward-looking" default — avoids hand-rolled pip/venv wiring.
- **PEP 639 `license = "MIT"`** is the modern SPDX form; the deprecated `{file=...}` table should be avoided.

### Documentation hosting

✅ RTD project imported into the GitHub repo. ✅ Created [.readthedocs.yaml](../.readthedocs.yaml) using the Astral-recommended `asdf`-installs-uv recipe; `fail_on_warning: true` mirrors `make docs-html`'s `-W`. ✅ `Documentation = "https://otto-sh.readthedocs.io"` added to `[project.urls]` and to [README.md](../README.md) — slug confirmed against the RTD dashboard. ⏳ First build goes live once a commit is pushed to `main` (or the user clicks **Build version** in the RTD dashboard).

**Recommendation: Read the Docs (RTD) at `otto.readthedocs.io`.** Otto's stack (Sphinx + myst-parser + furo) is RTD's canonical configuration — no migration friction. RTD gives you, out of the box: free hosting for OSS projects, a native version selector flyout menu (latest / stable / v0.1.0 / v0.1.1 / …), PR preview builds, full-text search, and a webhook-driven build on every tag push.

- ✅ **Created** [.readthedocs.yaml](../.readthedocs.yaml) at repo root — pins build OS (`ubuntu-24.04`), Python 3.10, installs uv via `asdf`, runs `uv sync --all-extras --dev`, builds with `uv run sphinx-build -W` to mirror `make docs-html`, points `sphinx.configuration` at `docs/conf.py` with `fail_on_warning: true`. Version 2 schema.
- ✅ **Signed up** at readthedocs.org, import the GitHub repo, enable the webhook. Activate the `latest` version (tracks `main`) and turn on "Build pull requests for this project".
- ⏳ **Configure** in RTD: set the default version to `stable` (which RTD auto-tracks to the highest semver tag once you have tags), and enable "Privacy Level: Public" + "Ad-free project" (request once under the community plan if ads bother you).
- ✅ **Modified** [README.md](../README.md) and [pyproject.toml](../pyproject.toml) `[project.urls]` — point Documentation URL at `https://otto-sh.readthedocs.io` (working assumption; may need fix if RTD assigned a different slug).

How the version selector appears automatically: RTD detects git tags matching PEP 440 (so `v0.1.0`, `v0.1.1rc1`, etc.) and builds each one as a separate version. The furo theme's flyout menu (bottom-right on every doc page) lists every built version so readers can jump between `latest`, `stable`, and any tagged release. Nothing extra needs to be wired in Sphinx.

**Alternative if you ever want self-hosting**: GitHub Pages + `sphinx-polyversion` (the maintained successor to `sphinx-multiversion`). A workflow checks out each tag, builds its docs, and publishes a combined tree to the `gh-pages` branch. More control, more moving parts. Skip unless RTD specifically doesn't work for you.

#### MkDocs vs Sphinx on RTD

**Short answer: yes, MkDocs is fully supported on RTD — it's one of the two first-class tools** (alongside Sphinx). The `.readthedocs.yaml` has a dedicated `mkdocs:` section in place of `sphinx:`. RTD's version-selector flyout is theme-agnostic and works identically for both. Material for MkDocs (the theme you're probably eyeing for aesthetics) is what most new Python projects reach for.

That said, the migration cost is real and worth weighing honestly:

| Concern | Sphinx (current) | MkDocs + Material |
|---------|------------------|-------------------|
| Aesthetics out of the box | Furo is good, not great | Material for MkDocs is the benchmark |
| Markdown support | Via `myst-parser` (already configured) | Native; no translation layer |
| API autodoc | `sphinx-autodoc-typehints` (already in dev deps) | `mkdocstrings[python]` (different API, needs rewrite) |
| `.. doctest::` directives | First-class Sphinx support via `make doctest` | Not supported by MkDocs natively; doctests that live inside `.py` docstrings still work via pytest `--doctest-modules`, but any doctest directives in standalone `.rst`/`.md` docs would need porting |
| Makefile coupling | `make docs` runs `sphinx-build` + `doctest`, enforces warnings-as-errors | New script needed (`mkdocs build --strict`) |
| Existing content | `.rst` + `.md` mixed | Would need `.rst` → `.md` pass |

**Middle-ground option worth considering: `sphinx-immaterial` theme.** It's a Material-for-MkDocs-style theme for Sphinx — gets you most of the aesthetic win without migrating the toolchain. Drop-in replacement for furo in `conf.py`; everything else stays. Good way to check whether aesthetics is the real itch before committing to a full migration.

Recommendation sequence:

1. **Before committing to MkDocs**, try `sphinx-immaterial` for an afternoon. If it closes the aesthetic gap, you're done — no migration.
2. If it doesn't, and MkDocs+Material is clearly what you want, plan the migration as its own project (not bundled into release-management work). Main pain points: rewriting autodoc config, porting any Sphinx-specific directives, reworking the doctest story.
3. Either way, RTD hosts it, and the version-selector experience is identical.

### Resources

- PEP 639 (license expression): https://peps.python.org/pep-0639/
- MIT License text: https://choosealicense.com/licenses/mit/
- `setup-uv` action: https://github.com/astral-sh/setup-uv
- Keep a Changelog: https://keepachangelog.com/en/1.1.0/
- Read the Docs tutorial: https://docs.readthedocs.io/en/stable/tutorial/
- `.readthedocs.yaml` reference: https://docs.readthedocs.io/en/stable/config-file/v2.html
- Versioned docs on RTD: https://docs.readthedocs.io/en/stable/versions.html
- sphinx-polyversion (alternative): https://github.com/real-yfprojects/sphinx-polyversion
- MkDocs on RTD: https://docs.readthedocs.io/en/stable/intro/mkdocs.html
- Material for MkDocs: https://squidfunk.github.io/mkdocs-material/
- sphinx-immaterial (middle ground): https://jbms.github.io/sphinx-immaterial/

### Verification

- ✅ Push a branch → CI green.
- ✅ `uv build` produces a wheel whose `METADATA` contains `License-Expression: MIT`. *(Verified locally with `pkginfo`.)*
- ✅ `uv run twine check dist/*` reports PASSED for both sdist and wheel.
- ✅ RTD builds `latest` successfully on the next push to `main`; docs load at `https://otto-sh.readthedocs.io/en/latest/`.
- ✅ Opening a PR triggers an RTD preview build and posts a link as a commit status.

---

## Phase 2 — Version management + TestPyPI dry-run

Goal: be able to cut releases to TestPyPI repeatedly, rehearse the tag → build → publish flow privately.

### Changes

- **Modify** [pyproject.toml](../pyproject.toml):
  - Keep version static in `[project]`.
  - Add `[tool.bumpversion]` config with a `[[tool.bumpversion.files]]` entry for `pyproject.toml`.
- **Create** [.github/workflows/release-testpypi.yml](../.github/workflows/release-testpypi.yml) — `workflow_dispatch` trigger: checkout at tag, `uv build`, upload via `pypa/gh-action-pypi-publish@release/v1` with `repository-url: https://test.pypi.org/legacy/`, authenticated via **trusted publishing (OIDC)**.
- **Create** [.github/workflows/release.yml](../.github/workflows/release.yml) — triggers on `v*` tag push, publishes to production PyPI. Gate behind a `pypi` GitHub Environment with required reviewers so a misplaced tag cannot accidentally ship.
- **Create** [docs/release_process.md](../docs/release_process.md) — runbook: commit → `bump-my-version bump patch` → push tag → watch workflow.

### Tooling decisions

**Versioning tool: `bump-my-version`.**

✅ Installed

- `uv version` exists but only rewrites `pyproject.toml` — no tagging, no commit, no multi-file updates. Useful for one-liners, doesn't replace a bumper.
- `bump-my-version` (successor to `bumpversion`) handles multi-file updates, commit, and tag in one shot. Zero runtime dependency. Fits a static-version workflow perfectly.
- `commitizen` bundles bump + changelog + Conventional Commits — useful, but heavier and locks you into CC. Skip until you want automated changelog generation.
- **Not recommended**: `hatch-vcs` / `setuptools-scm` / `uv-dynamic-versioning`. Otto's `uv_build` backend doesn't support dynamic version plugins the way `hatchling` does, and switching backends is a bigger yak-shave than the benefit.

The "static version in pyproject.toml + bumper + `importlib.metadata` at runtime" pattern is the norm for Typer CLIs (matches `pip`, `ruff`, `poetry`, `pipx`).

**Publishing: trusted publishing (OIDC).** No long-lived tokens in repo settings; scoped per workflow + environment + repo; PyPI's official recommendation since 2023.

### Resources

- bump-my-version: https://callowayproject.github.io/bump-my-version/
- PyPI trusted publishing: https://docs.pypi.org/trusted-publishers/
- `pypa/gh-action-pypi-publish`: https://github.com/pypa/gh-action-pypi-publish
- Packaging guide: https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/

### Verification

- `bump-my-version bump patch --dry-run` shows the expected diff and nothing else.
- Dispatch the TestPyPI workflow on a `v0.1.1rc1` tag → artifact appears on TestPyPI.
- `pipx install --index-url https://test.pypi.org/simple/ --pip-args='--extra-index-url https://pypi.org/simple' <pkg>==0.1.1rc1` installs cleanly; `otto --version` prints `0.1.1rc1`.

Note: if the name decision (Phase 4) slips, you can still rehearse TestPyPI under any reserved placeholder name; mechanics are identical.

---

## Phase 3 — Multi-version support + targeted concurrency confidence

Goal: prove otto works on more than one Python and build real insurance against the session.py race hotspots.

### Multi-version changes

- **Modify** [pyproject.toml](../pyproject.toml) — add classifiers for 3.11, 3.12, 3.13.
- **Create** [noxfile.py](../noxfile.py) — sessions `tests(python=['3.10','3.11','3.12','3.13'])`, `lint`, `typecheck`, `docs`. Uses `nox-uv` so each session resolves via uv.
- **Modify** [.github/workflows/ci.yml](../.github/workflows/ci.yml) — convert to a Python-version matrix, each cell runs `nox -s tests-${{ matrix.python }}`.

**Tooling decision: `nox` + `nox-uv`, not `tox`.**

- `nox` configures in Python (not INI); better for conditional logic (skip FTP-needing tests on certain runners, crank stability iterations in CI, etc.).
- `nox-uv` reuses uv's resolver and cache — fast, consistent with local dev.
- `tox-uv` works but INI config is awkward for conditional behavior.
- Matrix-only (no nox/tox) loses the ability to reproduce a specific Python version *locally* with one command — worth the small upfront cost.

### Stability changes

- **Create** [tests/unit/host/test_session_concurrency.py](../tests/unit/host/test_session_concurrency.py) — dedicated module targeting the three hotspots, built on `asyncio.gather`/`asyncio.TaskGroup` (no threads):
  - `test_oneshot_pool_high_fanout` — N=200 simultaneous `oneshot` acquisitions; assert no double-checkout and free-list size is sane after drain.
  - `test_named_session_alive_check_race` — concurrent `get_or_create` on the same name while a background task kills the transport; assert exactly one replacement session is created.
  - `test_ensure_default_session_recreation_race` — M concurrent tasks trigger default-session recreation; assert one-and-only-one recreation wins.
  - `test_session_manager_property` — `hypothesis.stateful.RuleBasedStateMachine` modelling acquire/release/kill/oneshot; invariant: no session appears in both `_oneshot_pool` and `_named_sessions`.
- **Modify** [pyproject.toml](../pyproject.toml) dev group — add `pytest-repeat`, `hypothesis`.
- **Modify** [Makefile](../Makefile) — new `stability` target: `uv run pytest tests/unit/host/test_session_concurrency.py tests/unit/host/test_remoteHost.py::test_oneshot_telnet_concurrent_does_not_deadlock --count=50 -p no:cacheprovider`. (Only otto-internal concurrency tests — the existing `tests/unit/suite/test_stability*.py` tests cover otto's ability to stability-test *other* products and do not belong here.)
- **Create** [.github/workflows/soak.yml](../.github/workflows/soak.yml) — nightly cron, `make stability` with `--count=500`; failures open an issue via `peter-evans/create-issue-from-file`.

### Stability strategy

The priority order (targeted tests > broad soak):

1. **Targeted concurrency module** — the real defense; each test is shaped by the specific race it hunts.
2. **`pytest-repeat --count=N`** on that module — cheap insurance against timing drift as code evolves. Lives in `make stability`.
3. **Hypothesis stateful test** — finds interleavings you wouldn't write by hand.
4. **Whole-suite repeat** — optional, run occasionally before releases locally; don't wire into CI (poor signal-to-noise).

Per CLAUDE.md, no threads mixed with asyncio; fan-out uses `asyncio.gather` / `asyncio.TaskGroup`.

### Resources

- nox: https://nox.thea.codes/
- nox-uv: https://github.com/FollowTheProcess/nox-uv
- pytest-repeat: https://github.com/pytest-dev/pytest-repeat
- hypothesis stateful: https://hypothesis.readthedocs.io/en/latest/stateful.html

### Verification

- `nox -s tests-3.12` passes locally.
- CI matrix green on 3.10–3.13.
- `make stability` runs 50 iterations without flakes across a week of normal dev.
- Nightly soak has a green run before Phase 4.

---

## Phase 4 — Public release

Goal: ship `0.1.0` to production PyPI under a name you can actually claim.

### Name decision — DONE

Dist name is **`otto-sh`** (PyPI), CLI command stays `otto`. Changed in Phase 1 rather than deferred to here, so CI/release workflows built in Phase 1–2 reference the final name from day one. Build-backend config required `[tool.uv.build-backend] module-name = "otto"` so `uv_build` still finds [src/otto/](../src/otto/) despite the hyphenated dist name.

PEP 541 to reclaim bare `otto` can still be filed in parallel as a free option (typically 3–12 months, often denied), but is not blocking.

### Changes

- ✅ `pyproject.toml` `name = "otto-sh"` (done in Phase 1); `[project.scripts] otto = "otto:app"` kept so users still type `otto`.
- **Modify** [README.md](../README.md) — add install section using `pip install otto-sh`; usage examples continue to say `otto`.
- **Modify** [docs/release_process.md](../docs/release_process.md) — finalize the runbook.
- **Register** `otto-sh` + `release.yml` workflow + `pypi` environment as a PyPI trusted publisher.
- **Tag** `v0.1.0`; release workflow ships to PyPI.
- **Create** a GitHub Release with notes from `CHANGELOG.md`.

### Resources

- PEP 541 (name reclaim): https://peps.python.org/pep-0541/
- Reclaim process: https://github.com/pypi/support (pick "Package name request")
- Semantic Versioning: https://semver.org/

### Verification

- `pipx install otto-sh` on a clean machine; `otto --version` prints `0.1.0`.
- PyPI project page: MIT license, correct classifiers, correct repo + docs links.
- `pipx run otto-sh run --help` works end-to-end.
- `https://otto.readthedocs.io/en/v0.1.0/` resolves and the version selector lists both `latest` and `v0.1.0`; `stable` points at `v0.1.0`.

---

## Suggested pacing

| Phase | Est. effort | Minimum value if you stop here |
|-------|-------------|--------------------------------|
| 1 | 0.5–1 day | Legally distributable; CI on every push |
| 2 | 1–2 days | Private TestPyPI releases at will; muscle memory for bump + tag flow |
| 3 | 2–3 days | Multi-version support; targeted race coverage; nightly soak |
| 4 | 0.5 day + any PyPI wait | Live on PyPI under a durable name |

Phases 1 and 2 together are the biggest leap — once TestPyPI dry-runs work, the only remaining unknown for Phase 4 is the name.

---

## Critical files

- [pyproject.toml](../pyproject.toml) — touched in every phase
- [LICENSE](../LICENSE) — Phase 1 new
- [.github/workflows/ci.yml](../.github/workflows/ci.yml) — Phase 1 new, Phase 3 matrix
- [.github/workflows/release-testpypi.yml](../.github/workflows/release-testpypi.yml) — Phase 2 new
- [.github/workflows/release.yml](../.github/workflows/release.yml) — Phase 2 new
- [.github/workflows/soak.yml](../.github/workflows/soak.yml) — Phase 3 new
- [noxfile.py](../noxfile.py) — Phase 3 new
- [tests/unit/host/test_session_concurrency.py](../tests/unit/host/test_session_concurrency.py) — Phase 3 new
- [.readthedocs.yaml](../.readthedocs.yaml) — Phase 1 new (docs hosting)
- [CHANGELOG.md](../CHANGELOG.md), [CONTRIBUTING.md](../CONTRIBUTING.md), [docs/release_process.md](../docs/release_process.md) — docs
- [Makefile](../Makefile) — Phase 3 `stability` target
