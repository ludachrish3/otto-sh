# Installation docs rework — design

**Date:** 2026-08-18
**Status:** Approved (brainstorming session with Chris)

## Problem

The installation story lives entirely in `docs/getting-started.md` and has rotted:

- Line 225 claims the monitor dashboard bundles "Plotly.js" — the dashboard moved
  to ECharts in the Untitled UI redesign.
- Three hardcoded otto versions, all stale and mutually inconsistent
  (`otto-sh==0.5.4`, `VERSION=0.3.0` twice); the latest tag is v0.8.4. Nothing
  stops this from recurring.
- The air-gapped flow forces a from-source wheel build (Node + `make web`) even
  though the published PyPI wheel already embeds the web frontends —
  `pip download otto-sh` from PyPI is sufficient and never documented.
- Only one transfer method paragraph; no internal-index-server guidance; no
  virtual-environment guidance; no recommendation for how a team should manage
  otto plus its own Python dependencies.
- No way to read otto's docs (or its dependencies' docs, which otto's pages
  link into) inside an air-gapped network. RTD publishes no downloadable
  format, and users cannot realistically build docs themselves (Node, Chromium,
  graphviz).

## Goals

1. Accurate installation docs, with build-time machinery so the two failure
   modes found (stale library name, stale versions) cannot silently recur.
2. Otto version numbers in docs substituted at build time from package
   metadata — never hand-written.
3. First-class air-gapped story: download (pip and uv flavors), transfer,
   serve (wheel dir → own index server → org artifact manager), install.
4. A clear environment-management recommendation for C/C++-focused teams,
   answering "should pyproject.toml be used at all?".
5. Offline documentation: otto's docs and dependency docs readable in the air
   gap, with working links. Primary: mirror at true URLs. Fallback: a
   relocatable bundle.

## Non-goals

- No redistribution of third-party documentation in otto's release artifacts.
- No auto-generation of the dependency table (a drift *gate* gives the
  accuracy without the machinery).
- No restructuring of docs beyond the installation content.

## Design

### 1. Page structure

New top-level `docs/installation.md`, added to the `index.rst` toctree between
`getting-started` and `guide/index`. Sections, in order: requirements → quick
install → recommended project setup → air-gapped installation (download →
transfer → serve → install) → offline documentation → dependencies reference →
air-gapped considerations (the existing SSH known_hosts / log retention /
Python availability notes move here).

`getting-started.md`'s Installation section shrinks to a ~15-line quick
install: venv one-liner, `pip install otto-sh`, verify with `otto --version`,
and a link to the new page for teams / air-gap / offline docs. Tab completion
stays in getting-started (first-run UX, referenced by `otto init` next-steps).
The dependency tables, air-gap content, and from-source/GitHub-release install
sections move to `installation.md`.

### 2. Accuracy fixes

- The dashboard-assets note stops naming a chart library: "all dashboard
  assets are bundled in the wheel; no CDN or external network access is
  needed." Naming the library is how it rotted; the air-gap grep gate in CI
  already enforces the actual invariant.
- All otto version literals in docs become build-time substitutions (§3).
- The uv download path is documented honestly: uv has no `pip download`
  equivalent (verified on uv 0.12.5, the version this work targets), so the
  uv-flavored flow is
  `uv export --no-dev --no-hashes > requirements.txt` followed by
  `pip download -r requirements.txt`, presented as the lockfile-faithful
  variant — not fake pip/uv symmetry.

### 3. Build-time version substitution

A small `source-read` event hook in `docs/conf.py` replaces a literal token
(exact spelling chosen at plan time, e.g. `%OTTO_VERSION%`) in Markdown/RST
sources with `release` — already computed from `importlib.metadata` at
`conf.py:12`, and kept identical to the latest tag by bump-my-version. A hook
rather than MyST's `substitution` extension because the token must substitute
**inside code fences** (`pip install otto-sh==X`, `gh release download vX`),
where MyST substitution support is unreliable; a `source-read` hook works
unconditionally in every context. Guarded by the lint gate in §7 so a
hand-written version literal fails `docs-lint`.

### 4. Environment management recommendation

Primary recommendation: **declare otto in a `pyproject.toml`, managed with
uv** — because otto imports the team's `pylib`/test code into its own process,
so any extra Python packages that code needs (common among otto users) must
live in otto's environment; a per-project environment declared in one file is
the only model that survives that. The C/C++ framing the page uses:
pyproject.toml is the test rig's parts list — a CMakeLists for the test
tooling — and uv is a single static binary (`uv sync && uv run otto …`), no
Python packaging knowledge required. Key mechanism: uv supports non-package
("virtual") projects, so a C/C++ repo declares
`dependencies = ["otto-sh", "pyserial"]` with no build backend and `uv sync`
just works, producing `uv.lock` for reproducibility.

Documented asymmetry: pip cannot install a pyproject's dependencies without
packaging the repo (no `--only-deps`), so the pip/venv alternative uses
`requirements.txt` (conanfile.txt-shaped, familiar). The page says plainly:
uv path → pyproject.toml; pip path → requirements.txt; do not contort a C/C++
repo into a fake Python package.

Python interpreter guidance, in order: (1) use the system Python — uv
discovers and uses system interpreters by default, and air-gapped orgs already
mirror OS packages; (2) a two-line note that if the OS Python is too old, uv's
managed Pythons work offline via `UV_PYTHON_INSTALL_MIRROR`, which accepts
`file://` URLs pointing at a directory of python-build-standalone archives —
static files, no server to stand up.

### 5. Air-gapped install flows

**Download (connected side).** Primary: straight from PyPI, no repo clone —
`pip download otto-sh --dest wheels --python-version … --platform …
--only-binary :all:`, keeping today's platform-tag note (cryptography, cffi,
pydantic-core need matching tags). Variants: the uv-flavored
`uv export` → `pip download -r` flow (§2); starting from a GitHub-release
wheel instead of PyPI; building from source demoted to a short
"development / unreleased versions" pointer.

**uv itself.** OS package repos don't carry uv (or carry versions too old to
matter), so the page covers bootstrapping it into the gap explicitly, two
ways: (a) the standalone static binary per platform from uv's GitHub
releases — download + published sha256 on the connected side, transfer like
the wheels, unpack onto `PATH` (the curl-pipe-sh installer is just a fetcher
for these same artifacts, so nothing is lost offline); (b) uv is also
published on PyPI as wheels, so `pip download uv` alongside otto puts it in
the same `wheels/` directory / internal index, installable with the system
pip. Pin the transferred version — `uv self update` cannot work offline. For
the current artifact list and platforms, link to uv's installation docs
(<https://docs.astral.sh/uv/getting-started/installation/>) rather than
restating them.

**Transfer.** Shown as a set: tar archive + sha256 manifest with verification
on the far side; scp/rsync; removable media; two-hop scp through a bastion.

**Serve inside the gap, three tiers.**

1. Bare `wheels/` directory + `pip install --no-index --find-links` (and the
   `uv pip install` equivalent) — today's flow, kept as the no-infrastructure
   tier.
2. Internal index server: `pypiserver` pointed at the wheels directory
   (itself pure-Python, installable from those same wheels). Client config for
   both tools: pip via `PIP_INDEX_URL` / `pip config set global.index-url`;
   uv via `[[tool.uv.index]]` with `default = true` **in the project's
   pyproject.toml — the internal index URL is committed once and every
   teammate's `uv sync` just works**. Plain-HTTP notes: pip
   `--trusted-host`, uv `--allow-insecure-host`.
3. A note that Artifactory / Nexus / GitLab package registries speak the same
   protocol — if the org already runs one, use it and skip tier 2.

### 6. Offline documentation

Two release artifacts, both produced from **one** docs build in the release
workflow, attached to the GitHub release next to the wheels:

- `otto-docs-<version>.tar.gz` — the stock HTML build, live URLs untouched.
- `otto-docs-offline-<version>.tar.gz` — derived from the stock output by a
  small repo-owned script that rewrites known absolute URL prefixes
  (docs.python.org, docs.pytest.org, the RTD subdomains, …) into
  depth-correct relative paths onto sibling directories (`../…/python/…`,
  computed per file from its depth in the tree). Sphinx's internal links are
  already relative; only intersphinx/external targets are absolute, so the
  rewrite surface is small and the script is unit-testable as a pure
  function (file path + href → href).

**Primary recommendation — true-URL mirroring** (matches what Chris's network
already does for docs.python.org): serve each doc site at its real hostname
via split-horizon DNS pointing at an internal mirror, with TLS from the org's
internal CA (doc links are https; a wildcard `*.readthedocs.io` zone covers
asyncssh/rich/telnetlib3 and otto-sh.readthedocs.io). Use the **stock**
tarball for otto's own docs. The page ships a table of the eight hostnames:
what to mirror, and where the content comes from — python.org's HTML archive;
RTD htmlzip downloads for the RTD-hosted projects; typer.tiangolo.com and
docs.pydantic.dev publish no offline archives (mirror with wget or accept
dead links — stated plainly). All external links to mirrored sites work, not
just intersphinx ones.

**Fallback — relocatable bundle** for environments that cannot override DNS /
issue certs: unpack `otto-docs-offline-…` as `otto/` next to sibling dirs
(`python/`, `pytest/`, `asyncssh/`, `rich/`, `telnetlib3/`) populated from the
same sources; every rewritten link is depth-correct relative, so the layout
works from bare `file://` or any static server at any sub-path. Links to
unfetched or unmirrorable sites remain live URLs; the page lists which.

### 7. Anti-regression gates

Both wired into the existing `docs-lint` Make target:

- **Version-literal lint:** regex forbidding hardcoded otto versions in docs
  source (`otto-sh==\d`, `otto_sh-\d…whl`, `VERSION=\d`, release-URL
  patterns), forcing the §3 token. Scoped to exclude `superpowers/**` and
  legitimate historical mentions (changelog-style prose like "shipped in
  v0.8.1" in coverage.md — exact scoping at plan time).
- **Dependency-table drift check:** script comparing the installation page's
  dependency table (names + min versions) against `pyproject.toml`
  `[project] dependencies`; fails on any mismatch in either direction.

### 8. Verification

- `make docs` (`-W`) proves substitution, toctree wiring, and links.
- Unit tests for the link-relativization script and the two lint gates'
  matching logic.
- Release workflow exercises the two-artifact docs job.
- Air-gap smoke check where feasible: `pip download` into a scratch dir +
  `--no-index --find-links` install into a scratch venv (network-dependent;
  exact placement — CI vs. optional local target — decided at plan time,
  mindful of dev-VM load limits).

## Plan-time verifications

Claims to verify empirically before/while writing the implementation plan:

1. uv 0.12.5 (the targeted version, per Chris) handles a build-backend-less
   ("virtual") pyproject with `uv sync` as described.
2. RTD htmlzip downloads exist (or can be enabled) for pytest, asyncssh,
   rich, telnetlib3 — adjust the hostname/source table to what's real.
3. The release workflow can absorb the docs build (Node + Chromium +
   graphviz already provisioned in CI docs jobs — confirm the release job can
   reuse that wiring).
4. Exact substitution token spelling that survives doc8/markdown-doctest
   linting inside code fences.
5. `UV_PYTHON_INSTALL_MIRROR` `file://` behavior on the uv version we
   document.
