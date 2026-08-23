# Contributing

Before changing code, start at {doc}`architecture/index` — it has one design
page per functional area, mirroring the User Guide's sections, and each page
ends with a "Where the code lives" section pointing at the modules that
implement it.

## Development environment

Vagrant can be used to develop and test changes to `otto`. After
[installing Vagrant](https://developer.hashicorp.com/vagrant/install), run
`vagrant up` from the repository root.

### The VMs

The `Vagrantfile` defines five machines on a private `10.10.200.0/24`
network:

| VM       | IP              | `autostart` | Purpose                                                   |
|----------|-----------------|-------------|-----------------------------------------------------------|
| `dev`    | `10.10.200.100` | yes         | Development VM - develop and run the test suite here      |
| `test1`  | `10.10.200.11`  | no          | SSH + SCP test host                                       |
| `test2`  | `10.10.200.12`  | no          | Telnet + netcat test host                                 |
| `test3`  | `10.10.200.13`  | no          | Docker-capable test host                                  |
| `zephyr` | `10.10.200.14`  | no          | Zephyr RTOS test bed (3 QEMU instances) + SSH hop to them |

The `zephyr` VM hosts **three** Zephyr QEMU instances concurrently, one per
filesystem config. They share the SSH hop (`10.10.200.14`) but each lives on
its own QEMU-internal `/30` so the host VM's routing table holds a distinct
route per TAP (a shared `/24` overlaps and the kernel picks one TAP for
all of them, making the other two unreachable):

| Zephyr instance | IP          | /30 subnet     | Filesystem                      | systemd unit                         |
|-----------------|-------------|----------------|---------------------------------|--------------------------------------|
| `zephyr37_fat`  | `192.0.2.1` | `192.0.2.0/30` | FAT on a RAM disk               | `zephyr-qemu-v3_7_fat_ram.service`   |
| `zephyr37_lfs`  | `192.0.2.5` | `192.0.2.4/30` | LittleFS on the flash simulator | `zephyr-qemu-v3_7_lfs.service`       |
| `zephyr37_nofs` | `192.0.2.9` | `192.0.2.8/30` | (none — no `fs` shell)          | `zephyr-qemu-v3_7_no_fs.service`     |

See `tests/firmware/zephyr/README.md` in the repo for the per-config
overlay layout.

Only `dev` starts on a bare `vagrant up` (the rest are `autostart: false`).
Bring the others up explicitly when you need them:

```bash
vagrant up                              # dev VM only
vagrant up test1 test2 test3 zephyr     # the test hosts
vagrant ssh                             # connect to dev (the default)
```

Develop and test from the `dev` VM — integration and end-to-end tests
assume connectivity to the test hosts.

### Files required at provision time

Most provisioning is self-contained (inline shell in the `Vagrantfile`),
but the `zephyr` VM's build step reads files from the repository checkout
through the `/vagrant` synced folder. These **must be present in your local
checkout before `vagrant up zephyr`** (a fresh `git clone` has them all —
this matters mainly if you iterate on overlays from outside the host
checkout; see the next subsection):

| File                                                                | Used for                                                          |
|---------------------------------------------------------------------|-------------------------------------------------------------------|
| `Vagrantfile`                                                       | The provisioning definition itself                                |
| `tests/firmware/zephyr/common/otto-overlay.conf`                    | Shared Kconfig overlay (shell, networking, runtime stats)         |
| `tests/firmware/zephyr/configs/v3_7_fat_ram/overlay.conf`           | FAT-on-RAM-disk Kconfig delta                                     |
| `tests/firmware/zephyr/configs/v3_7_fat_ram/app.overlay`            | FAT-on-RAM-disk devicetree (RAM disk node)                        |
| `tests/firmware/zephyr/configs/v3_7_lfs/overlay.conf`               | LittleFS Kconfig delta                                            |
| `tests/firmware/zephyr/configs/v3_7_lfs/app.overlay`                | LittleFS devicetree (flash simulator + fstab automount)           |
| `tests/firmware/zephyr/configs/v3_7_no_fs/overlay.conf`             | no-filesystem Kconfig delta (graceful-degradation target)         |

The `zephyr` VM builds an **unmodified** Zephyr shell sample
(`samples/subsys/shell/shell_module`) three times — once per filesystem
config — layering `common/otto-overlay.conf` plus the per-config
`overlay.conf` via `-DEXTRA_CONF_FILE="a;b"`, with the matching
`app.overlay` via `-DEXTRA_DTC_OVERLAY_FILE=` (the `no_fs` config omits
the DT overlay). otto ships no firmware code — the overlays only flip
standard Zephyr Kconfig options (telnet shell backend, networking,
runtime stats, filesystem), the same way a Unix host needs an
`sshd_config`. If any overlay is missing, the `west build` provisioning
step fails with a missing-file error.

The lab definition `tests/_fixtures/lab_data/tech1/lab.json` is read by otto at
**runtime** (not provision time); it must be present to target the test
hosts but is not needed for `vagrant up` itself.

### Iterating on overlays from outside the host checkout

`vagrant up zephyr` (and `vagrant provision zephyr`) run on your **host**
machine, and the `zephyr` VM's `/vagrant` synced folder maps to the
**host's** otto-sh checkout. If you edit firmware overlays from anywhere
other than the host checkout — for example, from inside the `dev` VM —
those edits do **not** reach the `zephyr` VM until they land in the host
checkout.

The sync mechanism is whatever your workflow already uses for source
control. With a shared remote: commit + push from where you edited and
pull on the host. Without one: any file-copy mechanism (`scp`, `rsync`,
manual copy) that puts the changed `tests/firmware/zephyr/...` and
`Vagrantfile` files into the host checkout will do. Either way:

```bash
# on the host, in the otto-sh checkout
vagrant provision zephyr                                # rebuild all 3 Zephyr images
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_fat_ram.service'
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_lfs.service'
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_no_fs.service'
```

`west build` is incremental within each per-config build dir, so
re-provision after an overlay edit is fast on the second run.

For a tighter iteration loop on a single config from the host:

```bash
vagrant ssh zephyr
source ~/zephyr-venv/bin/activate
source ~/zephyrproject/zephyr/zephyr-env.sh
west build -d ~/build/v3_7_lfs     # incremental rebuild of just that config
sudo systemctl restart zephyr-qemu-v3_7_lfs.service
```

A fresh `git clone` on the host has all the files above by default — this
workflow only matters when you are iterating on overlays from a different
checkout.

## Development setup

Otto uses [uv](https://docs.astral.sh/uv/) for dependency management. Once
the repo is cloned in the dev VM:

```bash
make dev              # uv sync, git hooks, hyperfine, browsers, and web/ deps
source project_env    # optional: sets up usage with test repos
uv run pytest         # run the test suite
```

`make dev` places `otto` at `otto-sh/.venv/bin/otto`.

### Node (the monitor dashboard's web lane)

The monitor dashboard's frontend (`web/`) is a separate React + Vite +
TypeScript project built with Node, pinned via `.nvmrc`
([nvm](https://github.com/nvm-sh/nvm) users: `nvm use`). The dev VM
provisions Node 24, and `make dev` runs `make web-install` (`npm ci`) so
`web/node_modules` is ready. Node backs both the dashboard build (`make
web`, `web-dev`) and the TypeScript quality gates below. Everything else —
the Python test suite and every other non-`web`/non-quality `make` target —
works from a checkout with the dashboard already built and never needs Node.
## Monitor frontend development

The dashboard's frontend is a React + Vite + TypeScript single-page app in
`web/`. Vite builds it into `src/otto/_webassets/monitor/dist/`, the *only*
frontend {class}`~otto.monitor.server.MonitorServer` serves — there is no
legacy fallback, so a checkout without a build fails loudly with a
`make web` pointer rather than silently serving something stale.

```bash
make web-install   # npm ci, from web/package-lock.json
make web-dev       # Vite dev server with hot reload; proxies /api to a
                    # running `otto monitor` (default http://127.0.0.1:8080,
                    # override with VITE_OTTO_TARGET=http://host:port)
make web           # production build: regenerates + diffs the generated
                    # wire types against the live pydantic models, builds,
                    # then gates the output against absolute http(s) URLs
                    # (labs are air-gapped)
make test-ts       # vitest — store reducers, SSE handling, chart-series
                    # grouping, per-chart series capping, etc.
```

`make web-dev`'s proxy target is a running server process — an `otto
monitor --live` collector or an `otto monitor <source>` review server both
serve `/api/*` — useful for developing against real backend responses,
live or historical. `make web` is what actually ships in the wheel.

**Behavior-spec contract.** `tests/e2e/monitor/dashboard/` is a Playwright
suite that pins the dashboard's observable surface through `data-testid`
attributes only — styling and DOM structure are free to change underneath
them. Those pins adjudicate, not this page or the source: if a doc
description and a pin ever disagree, fix the doc. Run them locally with
`make dashboard` (Chromium only — the fast
per-task check; needs `make browsers` once) or `make dashboard-all` for the
full cross-engine matrix: Chromium (Blink), Firefox (Gecko), and WebKit
(Safari). The one Safari-specific test runs on WebKit only via
`@only_browser("webkit")`. `make release` runs all three; CI runs them as a
parallel per-engine matrix.

### Web quality gates

`web/` carries the same lint / format / type-check / coverage discipline as
the Python side. For which tool performs each kind of check on each side,
and where each gate binds (`make` target, `nox` session, CI job), see
{doc}`architecture/quality-gates` — that page is the single inventory, so
this section stays about invocation. Every quality aspect follows the same
language-parity shape: a `-python` sub-target, a `-ts` sub-target, and a
bare umbrella that runs both:

| Aspect       | Python                    | TS                          | Both              |
| ------------ | ------------------------- | ---------------------------- | ----------------- |
| Lint         | `make lint-python`        | `make lint-ts` (Biome check + knip) | `make lint`       |
| Type-check   | `make typecheck-python`   | `make typecheck-ts`         | `make typecheck`  |
| All static   | `make check-python`       | `make check-ts`             | `make check`      |
| Autofix      | `make format-python`      | `make format-ts`            | `make format`     |
| Fast tests   | `make coverage-unit`      | `make test-ts`              | —                 |
| Coverage gate| `make coverage-python`    | `make coverage-ts` (merged vitest+e2e; unit floor: `coverage-ts-unit`) | `make coverage` |
| Everything   | `make validate-python`    | `make validate-ts`          | `make validate`   |

The umbrella targets `make validate`, `lint`, `format`, `typecheck`, and
`check` each run **both** languages via their `-python` / `-ts` sub-targets
(so `make validate` == `make validate-python` + `make validate-ts`). Bare
`make coverage` is the same shape: `coverage-python` (full pytest, 95 floor)
+ `coverage-ts` (merged vitest+e2e, its own floor). CI's browserless
`check-ts` job runs the reduced slice `check-ts coverage-ts-unit` — the
vitest-only floor, since it has no browsers to run the merged e2e leg.
Biome config lives in `web/biome.json`; the vitest coverage floor lives in
`web/vite.config.ts` (raise it as component test coverage grows). Install
the recommended "Biome" and "Vitest" VS Code extensions (see
`.vscode/extensions.json`) for format-on-save and an inline test runner.

One asymmetry inside that shape, because it is the kind that bites: `make
lint-python` also runs the architecture gates (`lint-arch` — tach and
ast-grep), so it matches CI's `lint-python` job, which is `nox -s lint` and
has always run them. It ran ruff only until 2026-08-10, and the difference
was invisible: a file could pass `make lint`, `make format` and every
coverage lane while still violating an architecture rule. Those rules mostly
police *test* code — deadline polls, `parents[N]` path arithmetic,
module-scope env writes — so a change that is "only a test" is the one most
exposed to them, not the least.

## Branching and commits

All work branches off `main`. `main` is protected — direct pushes are
rejected, so every change lands via a pull request.

```bash
git checkout main
git pull --rebase
git checkout -b <type>/<short-description>
```

Use one of these branch prefixes:

| Prefix     | Use for                              |
|------------|--------------------------------------|
| `feature/` | New functionality                    |
| `fix/`     | Bug fixes                            |
| `chore/`   | Tooling, deps, CI, refactors         |
| `docs/`    | Documentation only                   |

Examples: `feature/add-ssh-retry-logic`, `fix/gcda-parse-error-on-empty-file`.

Keep commits focused — one logical concern per commit. Use
[Conventional Commit](https://www.conventionalcommits.org/) prefixes in
the message subject:

| Prefix      | Meaning                                     |
|-------------|---------------------------------------------|
| `feat:`     | New feature                                 |
| `fix:`      | Bug fix                                     |
| `chore:`    | No production code change                   |
| `docs:`     | Documentation only                          |
| `test:`     | Tests only                                  |
| `refactor:` | Code restructuring, no behavior change      |
| `ci:`       | CI/CD configuration                         |

Before pushing, run `make all` locally — it mirrors CI
(`clean-dist → typecheck → coverage → docs → build`).

### Gating a branch before it lands

Run `make gate-fresh` before handing a branch back or squashing it onto `main`.

It runs CI's assets-absent Python lanes — `lint-python`, `lint-arch`,
`typecheck-python`, `coverage-hostless` — against your **committed** tree
inside a throwaway pristine worktree, then removes it (or keeps it, if the
gate went red, so you have somewhere to debug).

The reason it uses a separate worktree is that your checkout is a *superset* of
CI's environment. It accumulates gitignored build outputs — above all
`src/otto/_webassets/*/`, which only `make web` produces and which `pytest`
never builds — and a superset certifies nothing about a subset. A run that is
green only because an artifact happens to be lying around is not evidence about
CI, which starts from a clean checkout. That is how issue #196 reached `main`.

A worktree is free of every gitignored artifact by construction, so there is no
allowlist to write and none to keep in sync. It also catches two things nothing
else does: an unsynced `uv.lock`, and a test that only passes because of a file
that was never `git add`ed.

The general rule, of which this is one instance: **each gate should reproduce
its own CI twin's environment — matched, not maximal.** The hostless lanes are
the ones that run *without* a frontend build, so gating them with the frontend
present would be just as wrong as gating the browser lanes without it.

`git push` to `main` runs this automatically via `.githooks/pre-push`; use
`git push --no-verify` to skip it deliberately.

## Keeping your branch up to date

Always rebase, never merge, so history stays linear:

```bash
git checkout main
git pull --rebase

git checkout <your-branch>
git rebase main
```

Resolve conflicts commit by commit during the rebase
(`git add <file>` then `git rebase --continue`, or `git rebase --abort`
to start over). Push with `--force-with-lease` — it refuses to clobber
upstream commits you haven't seen:

```bash
git push origin <your-branch> --force-with-lease
```

## Pull requests

PRs target `main`. Link the related issue in the body using a closing
keyword so it auto-closes on merge:

```text
Closes #42
```

Open as a draft while work is in progress, then mark Ready for review
once `make all` is green:

```bash
gh pr create --draft --base main --title "feat: add SSH retry logic"
```

A maintainer will **squash and merge** once approved — you do not need
to squash yourself. After merge, delete the branch and pull `main`:

```bash
git checkout main
git pull --rebase
git branch -d <your-branch>
```

### PR checklist

- [ ] Commits follow the conventional commit format
- [ ] `make all` passes locally
- [ ] Branch is rebased on the latest `main`
- [ ] Related issue linked (`Closes #N`)
- [ ] Commit subject reads as the changelog entry it will become (see below)
- [ ] No manual edits to `CHANGELOG.md` or the version string

## Version management

Versioning is owned by maintainers and driven by
[`bump-my-version`](https://github.com/callowayproject/bump-my-version).
Do not hand-edit the `version` field in `pyproject.toml` — your PR will
be asked to revert the change.

`CHANGELOG.md` is **generated, not written**: `make changelog` regenerates
the whole file from Conventional Commit history via `cliff.toml`, so a hand
edit is erased at the next release. Your commit subject IS your changelog
entry — write it that way:

- the type picks the section, and **an unmapped type is dropped, not
  defaulted** — the config ends in a catch-all skip, so a commit that lands
  in no rule simply never appears:

  | type | section |
  | --- | --- |
  | `feat` | Added |
  | `fix` | Fixed |
  | `perf`, `refactor`, `revert` | Changed |
  | `docs` | Documentation |
  | `chore(deps…)`, `build(deps…)` | Dependencies |
  | `chore(…)` | Maintenance |
  | `ci`, `test`, `style`, `chore(release)` | *dropped* |
  | anything else — including `build(…)` with a non-`deps` scope | *dropped* |

- the scope is printed in bold before the subject, so `fix(cli): …` renders
  as **cli**: …, and a missing scope leaves the reader guessing;
- a breaking change — `type(scope)!:` or a `BREAKING CHANGE:` footer — earns
  a **BREAKING** badge. The badge is bare: the footer's TEXT is not copied
  into the bullet, because git-cliff's `breaking_description` truncates at
  the first `Token: value` continuation line and ignores every footer after
  the first, which mangled real entries. Write the footer for someone
  upgrading anyway — it is what a reader finds when the badge sends them to
  the commit.

The rendering is pinned by `tests/unit/test_changelog_rendering.py`, which
runs the real renderer over a synthetic repo. The committed file is
regenerated when a release is cut (`make release` runs `make changelog` at
the new version), so between releases its `## [Unreleased]` section lags
whatever has landed since — that is expected, and a reason not to hand-patch
it.

## Running tests

For the *why* behind this taxonomy — what each kind of test is for, and a
decision guide for picking one — see {doc}`architecture/testing`; this
section stays focused on the mechanics of running them.

```bash
make coverage                 # run the full suite and enforce the coverage gate
uv run pytest -k test_host    # run a subset by keyword
```

### Regression-test categories

Tests live in two orthogonal axes. **Level** is the directory a test lives in
(`tests/unit/` ⊆ `tests/integration/` ⊆ `tests/e2e/`) and is selected by *path*;
**resource** is what infrastructure a test needs (`integration` = Vagrant VMs,
`embedded` = Zephyr) and is selected by *marker*. Pick the target that matches
what you want to exercise:

| Category | How to run | VMs needed |
|----------|------------|------------|
| Unit tier only (level) | `make coverage-unit` (pinned) / `make nox-unit` (all Pythons) | none |
| Unit + integration tiers (level) | `make coverage-integration` / `make nox-integration` | full lab |
| No-testbed CI gate (tests/unit + no-VM e2e) | `make coverage-hostless` (pinned) / `make nox-hostless` (all Pythons) | none |
| Full coverage gate (all tiers, excludes `stability`) | `make coverage` | lab VMs |
| Unix VMs, incl. multi-hop (resource) | `make coverage-unix` / `make nox-unix` | test1/test2/test3 |
| Embedded / Zephyr (resource) | `make coverage-embedded` / `make nox-embedded` | zephyr VM |
| Multi-hop only | `uv run pytest -m "hops and not stability"` | three VMs |
| Stability / soak | `make stability` (or `stability-unit` / `stability-unix` / `stability-tunnel` / `stability-embedded`) | lab VMs (`-unit` needs none) |
| Chaos lane (tier 3, opt-in — interrupt/SIGKILL/reboot scenarios + BedHygiene, incl. docker kill/pause/restart/daemon-restart and privilege `as_user` interrupt) | `make chaos` / `make chaos-embedded` | leased unix host, incl. test3 for docker (+ zephyr board for the embedded leg) |
| Chaos lane, docker slice only (GitHub nightly, no lab needed) | automatic — `nightly.yml`'s `chaos-docker` job (`OTTO_CHAOS_DOCKER=loopback`) | none — loopback sshd wrapping the runner's own docker daemon |
| Everything (the dev-VM contract) | `make all` | lab VMs |
| Cross-Python matrix | `make nox-unit` (quick, no VMs) / `make nox` (full on 3.10 + 3.14, hostless on the middle versions) / `make nox-full` (full, all Pythons) | `nox`/`nox-full` need VMs |

`make stability-tunnel` soaks the tunnel machinery against the live bed:
add/remove churn (2- and 3-hop), concurrent populations, racing adds,
discovery-under-churn, a traffic soak, port-scoped-impairment churn,
degrade/recover cycling, and host-down health (phantom ip + SIGSTOP — no VM
is ever powered off). `COUNT=N` repeats the whole suite (default 1);
`CYCLES=N` sets each test's internal loop depth (default 5; `CYCLES=2` is the
smoke setting). These tests carry `stability + integration + hops`;
`stability-unix` excludes them via `not hops`. The no-VM collector tick soak
(`tests/unit/monitor/test_collector_tunnel_soak.py`) is marked `concurrency`
instead — it rides `make stability-unit` and stays in coverage. The suite also
proves the recovery contract: a degraded or uncertain tunnel is plainly visible
in `otto tunnel list` output, and misbehaving tunnels remove cleanly whenever
their hosts are reachable — including completing a partial reap after a host
returns.

`uv run pytest -k <kw>` filters any run by keyword. Recover a wedged embedded bed
with `make qemu-restart`; probe the whole lab with `make vm-health`.

#### Adding a harness environment knob

`tests/conftest.py` strips every `OTTO_*` variable from the environment at
import time so ambient otto *product* configuration can never leak into a run.
Harness knobs like `OTTO_CHAOS_DOCKER`, `OTTO_CHAOS_SEED`, `OTTO_CHAOS_BED_HOST`
and `OTTO_TUNNEL_SOAK_CYCLES` are exempt — but only because they are declared in
`tests/_ambient_env.py`, which is the single source of truth for that allowlist.

Declare a new knob there and read it with `ambient("OTTO_...")`. Reading one
straight from `os.environ` without declaring it does not raise; the variable is
simply gone by the time the reader runs, so the knob silently does nothing and
the run stays green while doing the wrong thing. That is issue #192: nightly's
`OTTO_CHAOS_DOCKER=loopback` job spent months of runtime targeting the bed host
instead, and the same bug had quietly disabled the chaos seed's reproduce path
and `make stability-tunnel CYCLES=N`.

### Embedded coverage bed

`zephyr37_llext` is the embedded coverage instance: an ARM `mps2_an385` Zephyr in the
`embedded` lab, reached over a QEMU `-serial telnet:` bridge via the `test4` SSH
hop (`zephyr-qemu-cov.service` on the zephyr VM, provisioned by the Vagrantfile
like the other Zephyr instances). The **dev VM runs no QEMU** — it only builds
the instrumented `.llext` extension and runs the cross-gcov report; the coverage
instance itself runs on the zephyr VM. Which host(s) coverage is collected from
is repo-declared by the `[coverage].hosts` regex in `.otto/settings.toml`
(default: every host in the lab), so the `test4` hop and the plain embedded test
hosts are excluded by the pattern rather than by inference.

> **Manual-gate convention.** If a test depends on infrastructure that
> `vagrant up` does not yet reproduce (a hand-built "gate"), label it clearly as
> a manual gate and record the plan to fold it into the provisioning. A manual
> gate must not masquerade as part of the reproducible regression suite.

### Cross-version testing with nox

`make ci` runs the no-testbed CI gate under one Python (whichever uv resolves by
default). To exercise the full matrix the way CI does — Python 3.10
through 3.14 — use `nox`:

```bash
make nox-unit                      # unit level tier across all Pythons (no VMs)
make nox-integration               # unit + integration tiers across all Pythons (full lab)
make nox                           # tiered matrix: full suite on 3.10 (floor) + 3.14 (warning canary), hostless on 3.11-3.13 (needs VMs)
make nox-full                      # complete full-suite matrix, all Pythons (needs VMs; ~2.5x nox)
uv run nox -s tests_hostless-3.10  # the no-testbed CI gate under one Python
uv run nox -s tests_unit-3.14 -- -k test_session   # forward args to pytest
uv run nox --list                  # show every available session
```

Nox sessions are defined in `noxfile.py` and use uv as the venv backend
via `nox-uv`, so each session reuses the same lockfile-resolved deps as
local development. The toolchains themselves come from
`uv python install 3.10 3.11 3.12 3.13 3.14` (run once per machine).
`make all` is unchanged and remains the dev-VM contract — `nox` covers
the cross-Python axis that `make all` doesn't.

## Documentation

### Building docs

```bash
make docs          # HTML output + doctests
make doctest       # run doctests only (from .md/.rst files)
make docs-html     # HTML only (warnings are errors)
```

pytest also runs doctests from Python source files automatically via
`--doctest-modules`.

The HTML build needs two system tools the dev environment provides:
`graphviz` (renders the architecture diagrams; provisioned on the dev VM
by the Vagrantfile) and headless Chromium (installed by `make dev` via
`make browsers`).

### Build-time GUI media and terminal blocks

Screenshots, video clips, and the animated terminal blocks are **products
of the build**, never committed. On every build, `docs/conf.py` runs two
capture scripts into `docs/_static/generated/` (gitignored):

- `scripts/capture_docs_media.py` serves the real monitor dashboard —
  through the same `DashboardHarness`/`FakeCollector` fixtures the browser
  e2e suite uses — seeds it with deterministic dummy data, and captures a
  screenshot plus a live webm clip with headless Chromium.
- `scripts/capture_docs_termynal.py` scaffolds a demo repo with
  `otto init --all` and captures every command's real `--help` output and
  the real tab-completion candidates (via typer's completion protocol),
  emitting one HTML snippet per capture. Pages pull them in with
  `{raw} html :file:` and the vendored `termynal.js` animates them.

The artifacts therefore always match the current code; a CLI or frontend
change shows up in the docs on the next build with zero manual work.

- Regeneration is stamp-cached: each script reruns only when its inputs
  change (the dashboard capture watches `src/otto/monitor` + the harness
  fixtures; the terminal capture watches all of `src/otto`).
  `make docs-media` forces a fresh capture of everything.
- Pages reference the generated files like any other asset; a missing
  screenshot or snippet fails the `-W` build loudly.
- `OTTO_DOCS_MEDIA=placeholder` writes tiny placeholder assets without
  running a browser or the CLI — an emergency escape hatch (e.g. a broken
  Chromium install on a docs host), not a developer convenience.

### Documentation layout

```text
docs/
├── overview.md          # Project overview
├── getting-started.md   # Installation and first steps
├── installation.md      # Install flows: air-gapped, teams, offline docs
├── guide/
│   ├── cli/             # One page per command, mirroring otto's command tree
│   └── configuration/   # settings.toml, lab.json, host sources and options
├── library/             # Using otto as a library + recipes (Markdown)
├── architecture/        # How otto is built and why (Markdown)
├── contributing.md      # This page
└── api/                 # API reference (reStructuredText, auto-generated)
```

CLI usage goes in `guide/cli/`, on the page for the command it serves —
that tree mirrors `otto`'s own command tree, so a new subcommand gets a page
under its verb's directory and an entry in that verb's toctree.  Anything
that serves a *Python* author rather than a CLI user goes in `library/`
instead.  API reference pages live in `api/` and use `.. automodule::`
directives to pull documentation from docstrings.  Design rationale and subsystem
internals belong in `architecture/` — when a change alters how a
subsystem works (not just what it does), update the matching
architecture page in the same PR.

### Docstring rules of thumb

- **New public function or class?** Add a Google-style docstring with a
  one-line summary, Args/Returns sections, and a `>>>` example if the
  function is pure and deterministic.
- **Changed a function's signature or behavior?** Update its docstring and
  any `>>>` examples to match.  Stale doctests will fail in CI.
- **Async, I/O, or nondeterministic code?** Write a docstring with `>>>`
  examples.  Also test these in `tests/unit/`.
- **Keep doctests minimal.** 2-4 lines showing the happy path is enough.
  Edge cases belong in unit tests.

### Doctest quick reference

In Python source files (collected by pytest):

<!-- doctest-lint: ignore -->
```python
def add(a: int, b: int) -> int:
    """Add two numbers.

    >>> add(1, 2)
    3
    """
    return a + b
```

In Markdown documentation files (collected by Sphinx):

````markdown
```{doctest}
>>> from otto.utils import Status
>>> Status.Success
<Status.Success: 0>
```
````

Common imports (`Status`, `Result`, `CommandResult`, `Results`, `LocalHost`)
are pre-loaded in doc-file doctests via `doctest_global_setup` in
`docs/conf.py`.

## Coverage reports

### From pytest

```bash
make coverage
```

### Manually

```bash
uv run coverage run --source=otto --context=manual -m otto <subcommand> [args]
uv run coverage html  # writes to reports/coverage/html (see .coveragerc)
```

## Type checking

`ty` (from astral) is being trialled as a replacement for pyright. The
project keeps a `[tool.pyright]` block for Pylance/VS Code, while
`[tool.ty]` drives the CLI checker and the optional ty language server.

```bash
make typecheck     # run ty check against src/ with all rules at error
```

Config lives under `[tool.ty.*]` in `pyproject.toml`. ty is pinned to an
exact version (see the `ty==` pin in `pyproject.toml`) because its 0.0.x
releases allow breaking diagnostic changes between any two versions —
floating the pin would cause unannounced CI churn.

Work the count down with per-line `# ty: ignore[rule-name]` suppressions
(justified in the surrounding context) or by fixing the underlying type.
Do not silence rules globally in `[tool.ty.rules]` — an individual
demotion there needs to be defensible in review.

Use `uv run ty explain rule <name>` for the full rationale and examples
behind any diagnostic.

For VS Code: install the "Astral ty" extension (Ctrl+Shift+P → Extensions
→ search "ty"). It reads `[tool.ty]` from `pyproject.toml`, so LSP
diagnostics and `make typecheck` stay in sync.

## Performance reports

```bash
uv run pyinstrument -o profile.txt -m otto <subcommand> [args]
```

## AI-Assisted Contributions

AI coding tools (e.g., GitHub Copilot, Claude, Cursor) are permitted for
contributions to otto. If your PR contains AI-assisted code, please note it
in the PR description. Regardless of how code was generated, contributors
are responsible for understanding, testing, and owning what they submit.

`make dev` installs a `prepare-commit-msg` hook (from `.githooks/`) that
prompts for the AI model used and records it as an `Assisted-by:` commit
trailer. On a non-interactive commit (no terminal — e.g. an agent or CI
job), the hook can't prompt, so it stamps a sentinel `Assisted-by: Claude
Opus 4.8 (unverified)` — grep for `(unverified)` and confirm or correct the
model when you squash/merge. If the message already carries an `Assisted-by:`
trailer, the hook leaves it as-is.
