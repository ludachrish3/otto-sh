# Contributing

Before changing code, start at {doc}`architecture/index` — it has one design
page per functional area, mirroring the CLI Reference's sections, and each page
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
| `test1`  | `10.10.200.11`  | no          | SSH + SCP test host; console server for `test2` and `bb1350`; hop to the BusyBox guests |
| `test2`  | `10.10.200.12`  | no          | Telnet + netcat test host, with a serial console served by `test1` |
| `test3`  | `10.10.200.13`  | no          | Docker-capable test host                                  |
| `zephyr` | `10.10.200.14`  | no          | Zephyr RTOS test bed (7 QEMU instances) + SSH hop to them |

The `zephyr` VM — otto knows it as the host `test4` — runs **seven** Zephyr
QEMU instances concurrently, spanning three Zephyr versions and two
transports. All of them are reached through its SSH hop (`10.10.200.14`).

**Four x86 instances are networked.** Each lives on its own QEMU-internal
`/30` so the host VM's routing table holds a distinct route per TAP (a shared
`/24` overlaps and the kernel picks one TAP for all of them, making the rest
unreachable):

| Zephyr instance | IP           | /30 subnet      | Zephyr | Filesystem                      | systemd unit                        |
|-----------------|--------------|-----------------|--------|---------------------------------|-------------------------------------|
| `zephyr37_fat`  | `192.0.2.1`  | `192.0.2.0/30`  | 3.7    | FAT on a RAM disk               | `zephyr-qemu-v3_7_fat_ram.service`  |
| `zephyr37_lfs`  | `192.0.2.5`  | `192.0.2.4/30`  | 3.7    | LittleFS on the flash simulator | `zephyr-qemu-v3_7_lfs.service`      |
| `zephyr27_fat`  | `192.0.2.13` | `192.0.2.12/30` | 2.7    | FAT on a RAM disk               | `zephyr-qemu-v2_7_fat_ram.service`  |
| `zephyr44_lfs`  | `192.0.2.29` | `192.0.2.28/30` | 4.4    | LittleFS on the flash simulator | `zephyr-qemu-v4_4_lfs.service`      |

**Three ARM `mps2_an385` instances are serial.** QEMU bridges each console to
a telnet port on the hop's loopback (`127.0.0.1`), so they need no TAP and no
`/30`; the address below is identity, not a route. otto reaches them over the
`console` term, which tunnels into `test4` and dials that port there. A
checkout from before the console term dials the guest's own address through
the hop instead, and cannot reach these three:

| Zephyr instance  | Address      | Console port | Zephyr | Sample                 | systemd unit                     |
|------------------|--------------|--------|--------|------------------------------|----------------------------------|
| `zephyr37_nofs`  | `192.0.2.37` | `2325` | 3.7    | `shell_module` (no `fs`)     | `zephyr-qemu-no_fs_arm.service`  |
| `zephyr37_llext` | `192.0.2.33` | `2323` | 3.7    | `shell_loader` (LLEXT)       | `zephyr-qemu-cov.service`        |
| `zephyr44_llext` | `192.0.2.34` | `2324` | 4.4    | `shell_loader` (LLEXT)       | `zephyr-qemu-cov44.service`      |

The two `_llext` guests run the stock LLEXT `shell_loader` sample, into which
an instrumented `.llext` is loaded at runtime; that is what backs embedded
coverage. Every other guest runs `shell_module`.

See `tests/firmware/zephyr/README.md` in the repository for the per-config
overlay layout.

### Serial consoles

Five bed hosts have a serial console that otto reaches over the `console`
term ({ref}`console-term`):

| Host | Console server : port | What answers Enter |
|------|-----------------------|--------------------|
| `test2` | `test1:4001` | agetty: `test2 login:` |
| `bb1350` | `test1:2450` (loopback) | BusyBox getty on the guest's second serial port, `ttyS1`: `bb1350 login:` |
| the three ARM Zephyr guests | `test4`, on the ports in the table above (loopback) | the Zephyr shell; no login |

`test2`'s console is a virtual null-modem, because VirtualBox offers no
usable UART to the arm64 guests. `test2` runs a getty on a pseudo-terminal
and listens for its far end on `10.10.200.12:4102` (socat); `test1` dials
that into a pseudo-terminal of its own, which `ser2net` serves on port 4001,
one client at a time, on every address. Start the two VMs in either order:
`test1` re-dials until `test2` answers. A session left logged in on the line
ends if the link drops. `bb1350`'s console is described with the BusyBox bed
({doc}`architecture/subsystems/busybox-bed`).

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
| `tests/firmware/zephyr/common/otto-overlay.conf`                    | Shared Kconfig overlay, applied to every build                    |
| `tests/firmware/zephyr/common/otto-overlay-v3_7.conf`               | Zephyr 3.7 supplement (applied when present)                      |
| `tests/firmware/zephyr/common/otto-overlay-v4_4.conf`               | Zephyr 4.4 supplement                                             |
| `tests/firmware/zephyr/configs/v3_7_fat_ram/{overlay.conf,app.overlay}` | 3.7 FAT-on-RAM-disk Kconfig delta + devicetree (RAM disk node) |
| `tests/firmware/zephyr/configs/v3_7_lfs/{overlay.conf,app.overlay}` | 3.7 LittleFS delta + devicetree (flash simulator + fstab)         |
| `tests/firmware/zephyr/configs/v2_7_fat_ram/{overlay.conf,app.overlay}` | 2.7 FAT-on-RAM-disk delta + devicetree                        |
| `tests/firmware/zephyr/configs/v4_4_lfs/{overlay.conf,app.overlay}` | 4.4 LittleFS delta + devicetree                                   |
| `tests/firmware/zephyr/configs/cov_an385/overlay.conf`              | ARM LLEXT base delta (both `_llext` guests share it)              |
| `tests/firmware/zephyr/configs/v3_7_no_fs_arm/overlay.conf`         | ARM no-filesystem delta (graceful-degradation target)             |
| `tests/firmware/zephyr/snmp_agent/`                                 | Out-of-tree SNMP-agent module, registered on the x86 builds       |
| `tests/firmware/zephyr/ext_svc/`                                    | Out-of-tree module registered on the ARM builds                   |

The `zephyr` VM builds **unmodified** Zephyr samples seven times, once per
guest. The four x86 guests build `samples/subsys/shell/shell_module`, layering
`common/otto-overlay.conf`, the per-version supplement when one exists, and the
per-config `overlay.conf` via `-DEXTRA_CONF_FILE="a;b;c"`, with the matching
`app.overlay` via `-DEXTRA_DTC_OVERLAY_FILE=` where the config ships one. The
three ARM guests build from a single `overlay.conf` and no devicetree overlay:
the two `_llext` bases build `samples/subsys/llext/shell_loader`, and
`no_fs_arm` builds `shell_module`. otto ships no firmware code — the overlays
only flip standard Zephyr Kconfig options (shell backend, networking, runtime
stats, filesystem), the same way a Unix host needs an `sshd_config`. If any
overlay is missing, the `west build` provisioning step fails with a
missing-file error.

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
vagrant provision zephyr                            # rebuild all 7 Zephyr images
# Restart by glob rather than by name: the unit set changes whenever the bed
# gains or loses a guest, and an enumerated list here goes stale silently.
vagrant ssh zephyr -c "sudo systemctl restart 'zephyr-qemu-*.service'"
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
the repository is cloned in the dev VM:

```bash
make dev              # uv sync, git hooks, browsers, and web/ deps
source project_env    # optional: sets up usage with test projects
uv run pytest         # run the test suite
```

`make dev` places `otto` at `otto-sh/.venv/bin/otto`.

`strace` (≥ 5.3) is a separate dev prerequisite `make dev` does not install,
used by the import-budget tests to count file operations: `sudo apt-get install
strace`. So are `gcc-11`, `clang` and `llvm-cov`, used by the kmodcov reader
tests (`tests/unit/cov/test_kmodcov_reader.py`), which build `otto_kmodcov`'s
own C with a gcc before 12, the default gcc and clang and read what it writes
with each compiler's gcov: `sudo apt-get install gcc-11 clang llvm`.

The test suite caches its bytecode under `$XDG_CACHE_HOME/otto/pytest-pycache`
(falling back to `~/.cache`, then the system temp dir), never in `src/otto` —
`tests/conftest.py` exports `PYTHONPYCACHEPREFIX` for you, so there is nothing
to set, and a session that does write a `__pycache__` under `src/otto` fails
with the directory named. Bytecode written into the editable tree bumps a
package directory's mtime and moves another process's file-operation counts
(#321/#343/#360/#361). That cache also lives outside the checkout, which is why
`make clean` does not empty it.

### Node (the monitor dashboard's web lane)

The monitor dashboard's frontend (`web/`) is a separate React + Vite +
TypeScript project built with Node, pinned to an exact version in `.nvmrc`
([nvm](https://github.com/nvm-sh/nvm) users: `nvm use`). CI, the dev VM and
the Read the Docs build all install exactly that version. `make dev` runs
`make web-install` (`npm ci`) so `web/node_modules` is ready. Node backs both
the dashboard build (`make web`, `web-dev`) and the TypeScript quality gates
below. Everything else — the Python test suite and every other
non-`web`/non-quality `make` target — works from a checkout with the
dashboard already built and never needs Node.

Moving Node is a reviewed bump, never a drift: the web gates fail on any
stderr output, and a new Node release can add a deprecation warning. To bump
it, edit `.nvmrc` in a change of its own, re-provision the dev VM
(`vagrant provision dev --provision-with dev-node`), and confirm that
`make web`, `make check-ts` and `make test-ts` all pass with nothing on
stderr.

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
`make coverage` is the same shape: `coverage-python` (full pytest, 95.5 floor over lines and branches)
+ `coverage-ts` (merged vitest+e2e, its own floor). CI's browserless
`check-ts` job runs the reduced slice `check-ts coverage-ts-unit` — the
vitest-only floor, since it has no browsers to run the merged e2e leg.
Biome config lives in `web/biome.json`; the vitest coverage floor lives in
`web/vite.config.ts` (raise it as component test coverage grows). Install
the recommended "Biome" and "Vitest" VS Code extensions (see
`.vscode/extensions.json`) for format-on-save and an inline test runner.

One asymmetry inside that shape, because it is the kind that bites: `make
lint-python` also runs the architecture gates (`lint-arch` — tach, ast-grep,
`ast-grep test`, which runs each rule's own valid/invalid snippets from
`.ast-grep/rule-tests/`, and the four parse-only tests behind
[Patching lazily exported names](#patching-lazily-exported-names)), so it
matches CI's `lint-python` job, which is `nox -s lint` and has always run
them; CI runs the four import-rule tests in its `tests` job. It ran ruff only until 2026-08-10, and the difference
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

A commit that removes, renames or moves a public name, or changes otto's
user-facing behavior so that an existing user has to change something (a
caller, a subclass, a script, a lab file), is a breaking change. Behavior a
user does not have to act on is not breaking, even when it shows: a
verdict that becomes more reliable, a run that takes a little longer, an
internal budget or timeout. Neither is any change to maintainer tooling
(`make` targets, `scripts/`, the gates, the hooks, CI), even one a
maintainer must adapt to; say what changed in the commit body instead.
A public name is any name in the `__all__` of a namespace declared in
`api/public.toml`; {doc}`api/stability` says which those are. Mark it with
`type(scope)!:` in the subject or a `BREAKING CHANGE:` footer (either is
accepted), however small the deletion looks. A rename is a deletion and an
addition together, so it is marked too.

A settings or lab-file key change is breaking as well, though not yet
enforced (a later schema-diff gate). `make check-breaking`
(`scripts/check_breaking_marks.py`, run in CI) enforces the rest against the
committed golden, `tests/unit/api_snapshot/public_api.txt`. That golden is
the **API dump**: one record per public binding and its shape.

The checker regenerates each commit's dump from that commit alone and
compares it with its parent's. A dump you forgot to refresh is refused,
marked or not: run `make api-snapshot` and commit the result with the
change. Removing a namespace from `api/public.toml`, or moving one from
`stable` back to `provisional`, needs a mark too. The range you pass must end
at `HEAD`. The machinery is described in
[the public-surface declaration](architecture/quality-gates.md#the-public-surface-declaration).

Before pushing, run `make all` locally — it mirrors CI
(`clean-dist → typecheck → coverage → docs → build`).

### Repairing a missing mark

A breaking commit can reach `main` without its mark, if it was pushed with
`--no-verify` or merged over a red check. `make check-breaking` flags it once,
on that push, and the next push's range starts after it. Published history is
never rewritten. Re-marking the commit would not help anyway: if it already
shipped, that release missed the bump. The repair is a **correction**: a new,
ordinary marked commit that names the commit it repairs. The release that
contains the correction takes the bump, and its notes quote the original
subject under the correction's entry.

```text
fix(api)!: record that dropping Beta broke callers

BREAKING CHANGE: `otto.Beta` is gone; import `otto.Alpha` instead.
Corrects: 3f9c2a1e5b7d4c8f0a6e2b9d1c4f7a3e5b8d0c2f feat(api): drop Beta
```

- **The correction carries the mark itself**: `type(scope)!:`, or a
  `BREAKING CHANGE:` footer. A mark counts only where git-cliff's census
  counts it. A footer belongs in the message's last paragraph, never inside
  prose, and the type must be one the changelog keeps (see the table under
  [Version management](#version-management)). A `test!:` or `ci!:` mark bumps
  nothing. Nor does any mark in a message git-cliff cannot parse: a header
  continued on its second line, or a last footer with no value.
- **`Corrects:` is a footer in that same last paragraph.** It holds the
  repaired commit's full 40-character sha, a space, and that commit's subject
  exactly as written. Repairing several commits takes one `Corrects:` line
  each.
- **The repaired commit must be an ancestor of the correction.** A correction
  cherry-picked onto a branch that never had that commit is refused.
- **A marked commit may still be corrected**, for a second break it did not
  mark.
- **Each commit is corrected once.** If two corrections of the same commit
  meet in one history, on one branch or two, both are refused: neither counts
  as first. Check `git log --grep '^Corrects: <sha>'` before you correct.
- **Only a published commit is corrected.** A commit still on your branch is
  amended instead. `make check-breaking` keeps reporting it unmarked even when
  a correction follows it in the same range.
- **A correction repairs a missing mark, never a missing notice period.** Once
  otto has a deprecation lifecycle, removing a protected name without its
  window is reported as a breach, not corrected.

`make check-breaking` validates every correction in its range. The rules live
in `scripts/release_events.py`, which `make release` also reads (see
[the release process](release_process.md)).

### Gating a branch before it lands

`make gate-scope` tells you which gates a commit needs. It reads the diff and
prints two command lists: one for **this commit** (`HEAD^1..HEAD`, the first
parent, so a merge counts only what it brought in) and one for the **tip**
(the merge base with `BASE`, default `origin/main`, to `HEAD`: what lands as a
whole). The lists gate the commits that will **persist** on `main`: a branch
that lands as one squash runs the tip list once, on its final tip, and its
intermediate commits run only the tests in their likely breakage areas plus
ruff on the files they changed; a landing whose commits persist one by one
runs the commit list for each. It refuses a dirty tracked tree (commit first) and a `HEAD` with no
parent (exit 2), and exits 1 when git cannot answer (a missing base, no
repository). `BASE` and `make quick`'s `PATHS` are read from the command line
only, never from the environment.

Each changed path has a class; a diff takes the union of its paths' classes:

| Class | Paths | This commit | Tip |
| --- | --- | --- | --- |
| `none` | `docs/superpowers/specs/`, `todo/`, `AGENTS.md` | `make check-breaking` only | the same |
| `docs` | `README.md`, `docs/` (not `conf.py`, `_ext/`, `examples/`) | `make docs` | `make gate-fresh` (it runs `docs`) |
| `web` | `web/src/` (not generated `*.gen.ts`) | `make check-ts test-ts` | `make check-ts coverage-ts`, `make gate-fresh` |
| `tests-leaf` | a test module whose tests and marks are unchanged (an added test is a leaf change unless it carries a mark), and the two measured matrices, over their reader modules | `make quick` over its package, ruff, `make lint-arch`; a module whose every test carries `interpreter_agnostic` runs `make quick-guards` instead of `make quick` | those, `make quick-guards`, `make gate-fresh` |
| `tests-structural` | a test module that removes or renames a test, adds a marked one, or changes a mark (its arguments too) | the leaf lines plus `make quick-guards` | as `tests-leaf` |
| `src-leaf` | one or two `src/otto/` packages | `make quick` over the package's unit and bed trees plus the tree-wide guards, ty, ruff, `make lint-arch`, `make quick-guards` | the full gate and the package's bed lane |
| `scripts` | `scripts/` | `make quick-guards`, ty, ruff | the full gate |
| `escalate` | a conftest, `tests/_fixtures/`, the registry, a package `__init__`, a build file (`Makefile`, `noxfile.py`, `pyproject.toml`, CI), three or more packages | the full gate, the bed lanes, plus `make quick-guards` (build file) and/or `make nox-unit-repeat` (conftest, fixture, registry) | the full gate, the bed lanes and `make nox-unit-repeat` (conftest, fixture, registry) |
| `full` | any path with no row | the full gate | the full gate |

The full gate is `make coverage`, `nox -s tests_hostless-3.14`,
`make check-python`, `make docs` and `make gate-fresh`. Beyond the `escalate`
and `full` classes, the commit column is the full gate when a test module
carries a `stability` or `chaos` mark outside every lane's files, carries a
`pytestmark` (or a mark decorator) the oracle cannot read, or leaves no
surviving root `make quick` could run, and when a `src/otto/` path's test
trees are all gone; each reason is printed above the commands. The roots
`make quick` receives for `src/otto/` packages include integration and e2e
bed trees, which need the lab VMs: run them on the dev VM.

Some test trees are a lane's own and are never handed to `quick`. Both `quick`
legs deselect the items of `tests/conformance/` (`make conformance`; the
bed-opener witness runs only under `make conformance-bed`), `tests/busybox/`,
the tunnel-stability tree and the chaos tree by marker (`conformance`,
`busybox`, `stability`), and browser modules, which route to `make dashboard`
by their `browser` mark, by `not browser`. `tests/integration/busybox_bed/` is
different: its items carry only `integration`, which `quick` and `make coverage`
do not exclude, so its row (`make coverage-unix` at the commit, the full gate at
the tip) is what keeps it out of `quick`. A lane row names the lanes in both
columns, even when the column is the full gate, because `make coverage`
excludes those tiers; a conftest or helper inside one of those trees keeps its
own class (a conftest escalates) and adds the tree's lanes as well. A *mixed* file (a stability file that also holds
unmarked tests) runs its lane for the marked tests and `quick` over its
package for the rest, when there is a rest. A module carrying `kmodcov`,
`browser` or `busybox` runs that lane wherever it lives; a hostless module
inside the dashboard tree is a plain leaf and runs under `quick` like any other.

`tests/unit/scripts/test_gate_scope.py` keeps this honest: every tracked path has
a row, and an oracle census proves that every item pytest collects in every
test module, changed alone, is selected by a lane the oracle emits.

`make gate-fresh` is the full gate's last command (a full tip column appends the bed and lane commands after it, and a docs, web or test-only tip ends with it). Run it before handing a branch back or squashing it onto `main`.

It runs the lanes that catch what a clean checkout sees and your dev tree
hides: `lint-python`, `lint-arch`, `check-api-snapshot` (the public-API
golden), `check-api-teaching` (the otto names the docs teach),
`typecheck-python`, `collect-check` and `docs`. It runs them against
your **committed** tree inside a throwaway pristine worktree, then removes it
(or keeps it, if the gate went red, so you have somewhere to debug). It does
not run the test suites: they take longer than a push can wait, and CI runs
them. `scripts/gate_fresh.py` says why each lane is in the list.

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

### The landing's gate is stamped

`make gate-tip` is how a branch earns its push to `main`. It asks the oracle
for the tip list (`make gate-scope`, over `BASE..HEAD`), runs every **stamped**
lane on it in order, and on green writes a stamp for that lane, keyed by the
committed tree. A lane that already has a stamp for this tree is skipped, so a
re-run after a red lane repeats only what is missing. It skips the **live**
lanes: the history checks, `lint-arch`, `check-python`, `docs` and
`make gate-fresh` depend on the pushed range or are run by the push itself
(`.githooks/pre-push`), so a stamp for them would certify the wrong thing.
Coverage is one stamped lane among the others; it runs through
[the coverage stamp](#the-coverage-stamp)'s own check. `uv run python
scripts/gate_tip.py --dry-run` lists the lanes without running them and
`--force` re-runs lanes that are already stamped.

Stamps live under `<git common dir>/otto-gate-stamps/<tree>/`, so every
worktree of the clone shares them. They are keyed by the tree hash and not the
commit because the tree is what a squash inherits: the squash of a gated tip
has the same tree, so the gate you ran on the branch is the gate the landing
shows. Like the coverage stamp, a stamp is a committed-tree contract: a dirty
tracked tree, or an untracked file the lanes would import, is refused, and so
is any command-line variable other than `BASE=` (`make gate-tip X=...` is
refused). `BASE=` is the one control. The runner shares the coverage stamp's
boundary for the environment, too: `PYTEST_ADDOPTS`, `MAKEFLAGS`, `MFLAGS` and an
environment `BASE` are cleared before a lane runs. Everything else in your
environment reaches the lanes undetected (`CONFORMANCE_CELLS=1 make gate-tip`
stamps a sampled bed run as the bed lane): the same boundary the coverage stamp
has, and you are trusted on it as on intermediate commits.

Two lanes **measure**: the conformance bed rewrites `schemas/support_matrix.json`
and `kmodcov` rewrites `schemas/kmodcov_matrix.json`. Commit exactly what the
run produced and the stamps follow: a measured matrix committed as the lane
wrote it is witnessed by blob, so every stamp of that run is promoted to the
new tree. Two consequences. A second measuring lane needs the first one's
commit before it runs, since the first rewrote a tracked file. And a red
measuring lane still rewrites the matrix, so restore it or review the diff
before you go on.

This has one accepted imprecision. Under a promoted stamp, a test that *reads*
a measured matrix ran against the pre-measurement matrix, at the tree that was
stamped; CI runs it again at the committed one. The oracle's row for the
matrices is what a measurement commit owes locally: the reader modules, through
`make quick-guards` for the family modules and `make quick` for the rest.

A release's own commits need no stamp. `make release` re-dates the two
matrices and bumps the version in `pyproject.toml` and `uv.lock` after its
test stages have passed on the tree before them, so those edits cannot change
a test's outcome. The oracle recognises each one by its content, never by path
alone (`RELEASE_OWNED` in `scripts/gate_scope.py`):

- `pyproject.toml` must be the base file with exactly bump-my-version's
  replacement applied (`version = "OLD"` to `version = "NEW"`, which also
  moves `current_version`).
- `uv.lock` may move only otto-sh's own `version` line.
- A matrix may change only its `as_of` dates, each still a date.

Such a change is classed `none` and adds no lane, so a pushed range made of
nothing else needs only the live lanes (`CHANGELOG.md` is on the docs row,
which is live too). Any other edit to these files, such as a dependency, a tool
setting or a changed verdict, is classified as before.

The exemption covers only those files. A commit on `main` that was not pushed
before the release still owes its own lanes, and they must be stamped on the
released tree: a stamp taken before the release names a different tree. So
push your work first, then run `make release`.

When the pre-push hook finds a stamped lane without a satisfying stamp it
refuses and says which, as in:

```text
gate-tip: refusing to push 3f9a1c2d4e5b to main — its gate is not stamped on tree 7b0c91ae2d44 (range 9e1d4b7a0c33..3f9a1c2d4e5b):
  make coverage: no stamp
run: git fetch origin main && make gate-tip BASE=FETCH_HEAD   (on commit 3f9a1c2d4e5b), then push again.
git push --no-verify skips this check; CI then runs only its own lanes — never make coverage, nox-unit-repeat or the beds.
```

Each lane's reason is one of `no stamp`, `unreadable stamp`, `command mismatch (…)`, `stamped for tree …, which differs from the tip's beyond the measured outputs (…)` or `stamped for a different path list (…)`; the last is the stale-fetch case below.

The check recomputes the tip list over the range the push really carries, so
a stale `origin/main` (someone else landed after your last fetch) can make the
list longer than the one you stamped. The remedy in the message is the
fetch-then-restamp: `git fetch <remote> main && make gate-tip BASE=FETCH_HEAD`.

There are two ways out of a refusal: stamp the tip, or `git push --no-verify`.
The second is honest only about what it skips: CI never runs `make coverage`,
`nox-unit-repeat` or the beds, so a push that bypasses the hook lands without
any of them having run.

`scripts/coverage_stamp.py check` stays exact-tree, and `make release` still
uses it that way. A release after a matrix commit therefore re-runs coverage
instead of trusting a promoted stamp. That is the right trade: the release is
the one place the full coverage run is cheap relative to what it certifies, and
promotion is for the landing's gate, not the release's.

### The coverage stamp

`make coverage` leaves a **stamp** when every leg passes: a small JSON file
under the clone's common git dir (`.git/otto-coverage-stamps/<tree>.json`,
shared by every worktree) naming the committed tree it gated
(`git rev-parse HEAD^{tree}`), the primary interpreter it ran under, the
threshold and selection, and a digest of the built web assets. `make release`
checks the stamp first and, when HEAD's tree has no valid one, runs
`make coverage` itself before any test stage, and before any stage that
commits, and checks again; its own matrix
(`make nox-release`) then covers 3.11–3.14 only, because the primary's
session is what the stamp proves.

The stamp is a committed-tree contract, like `make gate-fresh`: a run over
modified or staged tracked files, over an untracked file under `src/` or a
`testpaths` root in `pyproject.toml` (the live checkout would import it),
under a venv on anything but the primary interpreter, or with ANY variable
on the make command line (`BUMP=`, `NEW_VERSION=` and `BUDGET_APPROVE=` excepted — they are
the release's own; an environment `PYTEST_ADDOPTS` is simply cleared) still runs
every leg and is **not stamped** — the first lines of the run say why. An
untracked file anywhere else (`tests/_fixtures/`, `scripts/`) does not
block the stamp; the run lists it, and you should add it before you gate,
because the legs may import it while the committed tree lacks it. Only
`make coverage` stamps: `make all` and `make validate` run the same coverage
legs but leave no stamp. Commit, then `make coverage`, and the release will
find the stamp.

The landing's own gate is stamped lane by lane, which is a different contract:
see [The landing's gate is stamped](#the-landings-gate-is-stamped).

The release reads the same rules. A dirty tree, a non-primary venv or a
command-line variable stops the release at once; only a missing or
mismatched stamp runs `make coverage`. So `make release` with any
command-line variable but `BUMP=`, `NEW_VERSION=` or `BUDGET_APPROVE=` is refused before any
test stage runs, rather than gated at a custom setting and released.

## Keeping your branch up to date

Always rebase, never merge, so history stays linear:

```bash
git checkout main
git pull --rebase

git checkout <your-branch>
git rebase main
```

A merge also costs a mark. The checker judges a merge commit against its
**first parent**, your branch. Merging `main` after a marked removal landed
there removes that name again, relative to your branch, so the merge commit
needs its own `!` or `BREAKING CHANGE:` footer. A rebase replays your commits
on top of `main`, so the question never arises.

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

The bump tool only sees what a commit says, never what it did — which is
why the breaking-change marking rule above (see "Branching and commits")
exists: an unmarked deletion cannot raise the conventional-commit census
git-cliff computes below, and would otherwise ship as a patch release.

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
- a correction (see [Repairing a missing mark](#repairing-a-missing-mark))
  keeps its own bullet and badge, and adds one nested line per repaired
  commit -- `corrects <original subject> (<short sha>), which broke callers
  without a mark for it` -- so the release that takes the late bump says
  which earlier entry broke callers.

The rendering is pinned by `tests/unit/test_changelog_rendering.py`, which
runs the real renderer over a synthetic git repository. The committed file is
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

### The scoped lanes

`make quick PATHS="<dirs or files>"` runs just those paths, uninstrumented, in
two legs: a parallel leg, then the `-n0` twin for `serial_timing` tests. Random
ordering stays on. A leg that selects nothing is tolerated and printed; both
legs empty is refused, since the paths name no test. A red leg stops make with
`Error 1`; the pytest status is in the output above that line, not in make's
exit code. `make quick` without `PATHS` refuses before anything runs.

`make quick-guards` runs the repository guards: the `interpreter_agnostic`
family (build-file and lane census, API golden wiring, the script pins) over
`tests/unit`, uninstrumented and parallel. `make gate-scope` is how you learn
which of the two your diff needs. The policy is in `AGENTS.md`.

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
| No-testbed CI gate (tests/unit + no-VM e2e) | `make coverage-hostless` (pinned) / `make nox-hostless` (full on 3.10, coverage-gated; minus the `interpreter_agnostic` family on every other Python — see [the cross-Python matrix](architecture/testing.md#other-axes-typescript-doctests-cross-python)) | none |
| Full coverage gate (all tiers, excludes `stability`) | `make coverage` | lab VMs |
| Unix VMs, incl. multi-hop (resource) | `make coverage-unix` / `make nox-unix` | test1/test2/test3 |
| Embedded / Zephyr (resource) | `make coverage-embedded` / `make nox-embedded` | zephyr VM |
| Multi-hop only | `uv run pytest -m "hops and not stability"` | three VMs |
| Stability / soak — every tier runs and the target fails at the end if any did (so `make release-campaign` can rely on it) | `make stability` (or `stability-unit` / `stability-unix` / `stability-tunnel` / `stability-embedded`) | lab VMs (`-unit` needs none) |
| Chaos lane (tier 3, opt-in — interrupt/SIGKILL/reboot scenarios + BedHygiene, incl. docker kill/pause/restart/daemon-restart and privilege `as_user` interrupt) | `make chaos` / `make chaos-embedded`. On demand only — never part of `make release` (owner decision, 2026-10-15; see [quality gates](architecture/quality-gates.md#the-last-two-rows-are-the-point)). | leased unix host, incl. test3 for docker (+ zephyr board for the embedded leg) |
| Chaos lane, docker slice only (GitHub nightly, no lab needed) | automatic — `nightly.yml`'s `chaos-docker` job (`OTTO_CHAOS_DOCKER=loopback`) | none — loopback sshd wrapping the runner's own docker daemon |
| Host-contract conformance, hermetic venue (opt-in; also the GitHub nightly job) | `make conformance` (`OTTO_CONFORMANCE_CELLS=N\|all`) | none — loopback sshd + pinned BusyBox artifacts as subprocesses |
| Host-contract conformance, real bed venue (opt-in, dev VM only) | `make conformance-bed` (`CONFORMANCE_CELLS=N` to sample) | full lab: test1–test4, the five BusyBox guests, the seven Zephyr guests |
| otto_kmodcov toolchain proofs (opt-in, dev VM only; `make release` runs it) | `make kmodcov` (`KMODCOV_TOOLCHAINS=gcc-9,…,clang`, `KMODCOV_CROSS_KDIR=<prepared kernel source tree>`, `KMODCOV_KERNELS=x86_64-cross,2.6.32,…,6.17` — the host's own cross build plus the six kernels `scripts/provision_kmodcov_kernels.sh` provisions under `KMODCOV_KERNELS_DIR` (the script itself reads `OTTO_KMODCOV_KERNELS_DIR`), idempotently: re-runs are cheap, and a re-pin rebuilds only what it changes); ends by folding the run into `schemas/kmodcov_matrix.json` (the {ref}`compatibility matrix <kmodcov-matrix>`), which `make release-kmodcov-matrix` re-measures and commits | test1/test2, plus gcc 9–14, clang 18, the 6.8 source tree, docker with the qemu-x86_64 binfmt (`qemu-user-static` with `binfmt-support`) and the provisioned kernel set on the dev VM |
| Everything (the dev-VM contract) | `make all` | lab VMs |
| Cross-Python matrix | `make nox-unit` (quick, no VMs) / `make nox` (full on 3.10 + 3.14, trimmed hostless on the middle versions) / `make nox-full` (full, all Pythons) | `nox`/`nox-full` need VMs |

`make stability-tunnel` soaks the tunnel machinery against the live bed:
add/remove churn (2- and 3-hop), concurrent populations, racing adds,
discovery-under-churn, a traffic soak, port-scoped-impairment churn,
degrade/recover cycling, and host-down health (phantom ip + SIGSTOP — no VM
is ever powered off). `COUNT=N` repeats the whole suite (default 1);
`CYCLES=N` sets each test's internal loop depth (default 5; `CYCLES=2` is the
smoke setting). These tests carry `stability + integration + hops`;
`stability-unix` excludes them via `not hops`. The no-VM collector tick soak
(`tests/unit/monitor/test_collector_tunnel_soak.py`) is marked `concurrency`
instead — it rides `make stability-unit` and stays in coverage. A marker alone
does not enroll a test in a soak: pytest-repeat multiplies everything pytest
collects before `-m` deselects, so each `--count` target names the files it
soaks, and a newly marked test needs its file added there too.
`tests/unit/test_lane_invariants.py` fails until it is. The suite also
proves the recovery contract: a degraded or uncertain tunnel is plainly visible
in `otto tunnel list` output, and misbehaving tunnels remove cleanly whenever
their hosts are reachable — including completing a partial reap after a host
returns.

`uv run pytest -k <kw>` filters any run by keyword. Recover a wedged embedded bed
with `make qemu-restart`; probe the whole lab with `make vm-health` (which also
adds a `console` row per bed serial console: a console that logs in is healthy at
`AT-LOGIN`, a Zephyr console at `LOGGED-IN`, its idle shell prompt; each Zephyr
console row waits out the probe's full 10 s to tell those apart, so expect about
35 s of apparent pause), and put a console that a run left logged in back at its
login prompt with `make console-logout`. The 3.7
guests wedge by design of two upstream Zephyr bugs that are deliberately left in
place — see "Known guest defects" in `tests/firmware/zephyr/README.md` for the
mechanism, the tell, and why silence from a console is not evidence.

#### Adding a harness environment knob

`tests/conftest.py` strips every `OTTO_*` variable and `OTTOPATH` from the
environment at import time so ambient otto *product* configuration can never
leak into a run.
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

### Patching lazily exported names

Every otto package exports its public names lazily (PEP 562):
`otto.lab.get_lab` and `otto.config.load_user_settings` are resolved by the
package's `__getattr__` from its lazy table (`_LAZY_ATTRS` or `_LAZY_EXPORTS`),
and the package `__dict__` never holds them. Four rules follow, each with a
static check; the first also has a run-time backstop.

**Tests patch a name where it is defined, never on its package.**
`monkeypatch.setattr("otto.fleet.get_lab", fake)`, not
`"otto.lab.get_lab"`; `otto.session.lab.build_lab`, not
`otto.session.build_lab`. A package-level `monkeypatch` leaves the real object
cached in the package `__dict__` when it undoes, and from then on, on that xdist
worker, every `from otto.lab import get_lab` binds the cached object and a
later defining-module patch reaches nothing — the failure lands on an unrelated
test. `mock.patch` on the package does not leak, but shadows a defining-module
patch inside its block, so the rule covers it too. The lazy table in the
package's `__init__` names the defining module; a name declared through another
lazy package (`otto.app` → `otto.cli.app`) is defined one hop further
(`otto.cli.main.app`). An EAGER re-export is a different binding too:
`otto.bootstrap` binds `get_projects`, `get_ordered_projects` and `is_bootstrapped`,
which `otto.declared` defines, so a test patches `otto.declared.get_projects`
(`bootstrap` is the root's own). Its lazy `get_env` is discovery's, defined
in `otto.config.project`, so a test patches `otto.config.project.get_env`. It also
binds the error types
`otto.config.scope` defines, so a test that needs to replace
`ProjectScopeError` patches `otto.config.scope.ProjectScopeError`.
Every other module that raises or catches it imports it from there when
called, so that patch reaches every raise and `except` in otto except
`otto.bootstrap`'s own: its providing-project scope check raises through the
binding it took at import. Do not replace the class to test that check; it
raises the owner's object, so
`pytest.raises(otto.config.scope.ProjectScopeError)` catches it.
`tests/unit/test_patch_targets.py` parses every test tree,
integration and e2e included, and names the module to patch instead (its
second scan finds the eager re-exports by identity); the root
conftest's teardown hook (`tests/_fixtures/_lazy_exports.py`) catches a leak at
run time, fails the leaking test by name and evicts the cached object. Names
from a `_LAZY_ATTRS` package that a consumer binds at module level
(`otto.cli.link.find_link`) are patched on the consumer; that is correct and
leaks nothing.

**Source reads process-wide getters at call time.** The process-wide getters —
`otto.bootstrap`'s `bootstrap`, `otto.config.project`'s `discover`, `get_env` and
`discovered_teardown_deadline`, `otto.declared`'s `get_projects`,
`get_ordered_projects`, `is_bootstrapped`, `current_projects`, `current_ordered_projects`
and `compose`, `otto.registry`'s `get_completion_names` (`otto.bootstrap`
re-exports it and the first three of `otto.declared`'s), and the function entries of the
`otto`, `otto.config`, `otto.logger` and `otto.lab` lazy tables (`get_lab`,
`all_hosts`, `do_for_all_hosts`, `get_context`, `try_get_context`, ... and the
registrars, such as `register_options` and `register_cli_command`) — are
imported inside the function that uses them, from the defining module
(`from ..declared import get_projects`, `from ..fleet import get_lab`),
never at module level outside `if TYPE_CHECKING:`. A module-level binding keeps
the real function under a patch every other caller sees, so the module's tests
run the real bootstrap without anyone noticing. Classes (`Project`, `OttoContext`)
are free to import at module level: binding one defeats no patch.
`tests/unit/test_no_import_time_lazy_exports.py` flags a module-level binding
by any spelling; its `ALLOWED` table lists the sites that cannot move (a
decorator applied at import), each with its reason. A module that binds a
getter only to re-export it, listed in its `__all__` and never read by the
module itself, is not a caller and passes; a module-level import of that
re-export (`from otto.bootstrap import get_completion_names`) is flagged like
an import from the getter's owner.

**A moved name has one spelling.** When a public name moves home, its old home
is refused in every spelling — import, attribute, patch-target string,
generated code — across `src/otto` and every test tree, by
`tests/unit/test_moved_name_spellings.py`, which imports nothing it reads. A
stale import inside a broad `except` would otherwise degrade silently. For the
`otto.lab` facade it also holds the broader form of the next rule: otto's own
code may not import it in any spelling or name it in a lazy table.

**Source imports a lazily exported name from its defining module.** Code
under `src/otto` never reaches a name in a package's lazy table through that
package: `from otto.result import CommandResult`, not
`from otto.host import CommandResult`;
`from ..inventory.config import build_inventory`, not
`from ..inventory import build_inventory`, nor `inventory.build_inventory`
after `import otto.inventory as inventory`. tach maps an import to the module
it names, so a facade import declares the facade as the dependency, and the
layers and the cycle check judge an edge that is not the real one.
`tests/unit/test_internal_imports_skip_facades.py` fails on each offender and
names the module to import from, following a binding through further facades
as the first rule describes.
`uv run python scripts/facade_sweep.py --apply` re-points every `from` import,
and `uv run ruff check --select I001 --fix src/otto` and
`uv run ruff format src/otto` then sort and format what it rewrote; an
attribute read is re-pointed by hand. The rule reads lazy tables only: a name a
package's `__init__` imports eagerly and re-exports is not checked. A facade's
own `TYPE_CHECKING` block, which mirrors its lazy table for type checkers, and
the shipped examples, which are taught code, are exempt.

All four static checks run in `make lint-arch` as well as in every pytest
lane, so the per-change gate catches a violation that a targeted test run would
never select.

### Standing in for a Project

A test that needs a {class}`~otto.config.project.Project` without a project root on disk builds
one with `fake_project(name, **overrides)` from `tests/_fixtures/fake_project.py`,
never a hand-built `SimpleNamespace`. The factory returns a real `Project` with
every field at its declared default, so a reader added to `src/` tomorrow finds
the attribute it reads; a namespace carries only what its author's code path
read the day it was written, and breaks the test for an unrelated reason the
first time a patch reaches further. The module docstring records the two
incidents that made this the rule; `tests/unit/test_fake_project_fixture.py` pins
the factory against a parsed `Project`.

### Embedded coverage bed

`zephyr37_llext` is the embedded coverage instance: an ARM `mps2_an385` Zephyr in the
`embedded` lab, whose QEMU `-serial telnet:` bridge the `console` term reaches
by tunnelling into `test4` (`zephyr-qemu-cov.service` on the zephyr VM, provisioned by the Vagrantfile
like the other Zephyr instances). The **dev VM runs no QEMU** — it only builds
the instrumented `.llext` extension and runs the cross-gcov report; the coverage
instance itself runs on the zephyr VM. Which host(s) coverage is collected from
is project-declared by the `[coverage].hosts` regex in `.otto/settings.toml`
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
make nox-unit                      # unit level tier across all Pythons (no VMs; family on 3.10 only)
make nox-integration               # unit + integration tiers across all Pythons (full lab; family on 3.10 only)
make nox                           # tiered matrix: full suite on 3.10 (the oldest supported interpreter; uninstrumented — the floor is make coverage's); every product test, minus the interpreter_agnostic family, on 3.14 (warning canary); trimmed hostless on 3.11-3.13 (needs VMs)
make nox-full                      # every tier on all Pythons; the interpreter_agnostic family on 3.10 only (needs VMs)
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

`>>>` examples in `src/otto` run in `make doctest-src` (and the nox `docs`
session), which pass `--doctest-modules`; the flag is not in pytest's `addopts`.

The HTML build needs two system tools the dev environment provides:
`graphviz` (renders the architecture diagrams; provisioned on the dev VM
by the Vagrantfile) and headless Chromium (installed by `make dev` via
`make browsers`).

### Build-time GUI media and terminal blocks

Screenshots and the animated terminal blocks are **products of the
build**, never committed. On every build, `docs/conf.py` runs two
capture scripts into `docs/_static/generated/` (gitignored):

- `scripts/capture_docs_media.py` serves the real monitor dashboard —
  through the same `DashboardHarness`/`FakeCollector` fixtures the browser
  e2e suite uses — feeds it committed `web/fixtures/` exports through the
  Import front door, and captures screenshots with headless Chromium, each
  clipped to the feature it illustrates rather than the whole page. The
  topology shot uses `web/fixtures/isp-core.json`; the fleet grid, chart,
  element and events shots use `web/fixtures/kitchen-sink.json`. The same
  script renders the fixture coverage report shared with
  `tests/e2e/cov/report_browser/` through the live report renderer and
  photographs each covapp feature — tiers, run drilldowns, context,
  product and ticket pins, the legend, asserted coverage — each still
  clipped to that feature (the `coverage-*.png` images on the `otto cov`
  pages).
- `scripts/capture_docs_termynal.py` scaffolds a demo project with
  `otto init --all` and captures every command's real `--help` output and
  the real tab-completion candidates (via typer's completion protocol),
  emitting one HTML snippet per capture. Pages pull them in with
  `{raw} html :file:` and the vendored `termynal.js` animates them.

The artifacts therefore always match the current code; a CLI or frontend
change shows up in the docs on the next build with zero manual work.

- Regeneration is stamp-cached: each script reruns only when its inputs
  change (the media capture watches `src/otto/monitor`, the coverage
  renderer, the built `src/otto/_webassets` bundles, the harness and report
  fixtures under `tests/_fixtures/`, and the `web/fixtures/` documents; the
  terminal capture watches all of `src/otto`).
  `make docs-media` forces a fresh capture of everything.
- Pages reference the generated files like any other asset; a missing
  screenshot or snippet fails the `-W` build loudly.
- `OTTO_DOCS_MEDIA=placeholder` writes tiny placeholder assets without
  running a browser or the CLI — an emergency escape hatch (e.g. a broken
  Chromium install on a docs host), not a developer convenience.

Not every terminal block is captured: no harness captures
`otto reservation check`, so the table on its CLI page is hand-written and
nothing keeps its box art in step with the code — its columns and values are
the contract.

### Installation page gates

Two tables on {doc}`installation` are gated rather than hand-maintained, both
in `make docs-lint` and `nox -s docs`. `scripts/check_docs_dependency_table.py`
checks the *Direct runtime dependencies* table against `pyproject.toml`.
`scripts/check_docs_wheel_matrix.py` re-derives the *Native-extension
dependencies* table — the package set, each wheel-matrix label, and the
per-version wheel coverage — plus the air-gap download loop's Python list from
the committed `uv.lock` and `[project] classifiers`, and fails on drift, so a
dependency that starts or stops shipping binary wheels breaks the docs build
rather than the next air-gapped install.

### Documentation layout

```text
docs/
├── overview.md               # Start here: what otto is
├── installation.md           # Start here: install flows, air-gapped, teams, offline docs
├── setup/                    # Set up a project: scaffold, products, tests, coverage
├── use/                      # Use a project: driving a project someone else set up
├── extend/                   # Extend otto: custom products, hosts, parsers, backends, library use
├── cli/                      # Reference: one page per command, mirroring otto's command tree
├── configuration/            # Reference: settings.toml, lab.json, host sources and options
├── api/                      # Reference: API (reStructuredText, auto-generated)
├── architecture/             # Developing otto: how otto is built and why
├── developing/               # Developing otto: section hub and the bed case study
├── contributing.md           # This page
└── examples/                 # checked-in example projects pages include; not Sphinx sources
```

CLI usage goes in `cli/`, on the page for the command it serves —
that tree mirrors `otto`'s own command tree, so a new subcommand gets a page
under its verb's directory and an entry in that verb's toctree.  Pages are
grouped by reader: setting up a project, including writing its tests and instructions, goes in
`setup/`; driving an existing project goes in `use/`; extending otto or using
it from Python goes in `extend/`. A page has exactly one toctree parent, and
other hubs link to it.  API reference pages live in `api/`. Each namespace declared in
`api/public.toml` has one page, a bare `.. automodule::` that renders its
`__all__` (the *Public API*). Every other module renders under
`api/internals/` with `:ignore-module-all:` (the *Internals*). A new
declared namespace gets a public page and a new internal module an
Internals page; `tests/unit/docs/test_api_reference_split.py` refuses
anything else.  Design rationale and subsystem
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
- **A CLI command's help is user text.** Typer shows a command's docstring as
  its `--help`, so for a command or group callback write plain language for the
  person typing it: single backticks around a literal command or flag, no
  reStructuredText (double backticks, roles, `*emphasis*`), no references to
  specs or internals. Put developer notes in a `# Developer note:` comment
  directly below the docstring. For an `otto host` verb the docstring stays API
  documentation and `@cli_exposed(help_=...)` carries the user text.
  Typer renders help as Rich markup, so a literal `[` must be escaped: write
  `\\[coverage.tickets]` in the Python string, or the settings section name
  vanishes from the rendered help.
  `tests/unit/cli/test_help_text_user_facing.py` fails on markup in any built-in
  command's help, and on any help string that renders with words missing.

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
>>> from otto import Status
>>> Status.Success
<Status.Success: 0>
```
````

`doctest_global_setup` in `docs/conf.py` preloads a few names for the
function doctests autodoc renders from `src/otto`. A docs page still imports
every otto name it uses, in its own code: the docs validator
(`scripts/api_teaching.py`) refuses a page that leans on the setup
(`setup-only-name`). The setup's non-otto helpers (`run`, `GS_EXAMPLE`) need
no import.

### Setup and bed-case-study captures

The `docs/setup/` and `docs/developing/bed-case-study/` pages `{literalinclude}` what they show, never
paste it: config and code fragments from the checked-in project
`docs/examples/getting-started/`, and real command output from committed text
files under `docs/examples/getting-started/captures/`, produced by
`scripts/refresh_docs_captures.py` from the manifest
`docs/examples/getting-started/captures.toml`. Labless captures
(`labless = true`) are diffed in the docs gate (`nox -s docs`,
`make docs-lint`); bed captures are
diffed by `make docs-captures-check` and refreshed by `make docs-captures`,
both of which need the lab VMs. To refresh one: `uv run --no-sync python
scripts/refresh_docs_captures.py --only <id>`. Never edit an artifact by hand
— if a capture drifts, the page's claim drifted with it, and the fix is in
otto, the manifest's redaction rules, or the prose. A failed refresh leaves
the captures it had already written updated and the rest stale (exit 2); fix
the cause and re-run. Every capture runs in the fixed scratch directory
`/tmp/otto-gs` (not `$TMPDIR`), so an artifact's layout is reproducible
regardless of machine or environment — don't refresh two manifests at once on
the same machine, they'd collide on it. The pages show that path in
their examples and tell readers to substitute their own. A capture whose
page's step is "add this table to your settings" can name
`settings_append = "<file>"`, a project-relative TOML fragment appended to the scratch copy's
`.otto/settings.toml` for that capture alone and restored afterwards, so
committing the table doesn't change every other capture of the project.
A doctest on these pages that calls `project.import_init_modules()` imports the
example project's init module, which registers process-global extensions — a
command frame, monitor parsers, a login proxy, a reservation backend — that
every later document in the same `sphinx-build -b doctest` process then sees.

`tests/unit/docs/test_getting_started_example.py` keeps the example project
itself loadable and its claims true: every lab loads through the same code a
user's project does; the inventory twin
(`docs/examples/getting-started-inventory/`) builds the same `unix` lab as the
inline form, compared field by field and cred by cred; and the example
reservation backend passes conformance with `expect_holders=True` and answers
the inverted `holders` query. `tests/unit/docs/test_getting_started_includes.py`
checks that every `{literalinclude}` under `docs/setup/` and `docs/developing/bed-case-study/` resolves.

### Configuration and host pages

Parts of the Configuration tree and the host pages are tied to the code or to
test fixtures, so edit them with the tie in mind:

- **The inventory worked example mirrors a fixture.** The four blocks on
  `docs/configuration/inventory.md` marked
  `<!-- fixture: tech1-inventory/<file> -->` mirror
  `tests/_fixtures/lab_data/tech1-inventory/`
  (`tests/unit/docs/test_inventory_worked_example.py`): the three JSON blocks
  must parse to the fixture files, and the TOML block must match the fixture's
  `backend` and `supplies` (its paths differ on purpose). Exactly those four
  markers must be present; the NetBox block is unguarded.
  `tests/unit/labs/test_inventory_equivalence.py` proves the fixture's
  referenced hosts build identically to the same hosts declared inline.
- **The lab-config example is an excerpt.** The *Example* on
  `docs/configuration/lab-config.md` is taken from
  `tests/_fixtures/lab_data/tech1/lab.json`, trimmed to two elements, with
  fewer resources and host fields. Nothing compares the two, so keep them in
  step by hand.
- **Every lab-data field needs a row.**
  `tests/unit/docs/test_lab_config_field_coverage.py` fails when a field of a
  lab-data spec model has no row on `lab-config.md`, or an inventory record
  field has none in `inventory.md`'s *Record fields* table. It proves presence,
  not placement.
- **`docs/cli/host/families.md` is generated.**
  `scripts/render_support_matrix.py` renders it from the `capabilities`
  declaration on each host class (the meaning rows come from
  `src/otto/host/capability_grid.py`) on every Sphinx build. The page is
  committed, and `tests/unit/test_support_matrix.py` fails when it is not
  byte-for-byte what the tree would generate; edit the renderer or the
  declaration, then run `uv run python -m scripts.render_support_matrix`.

### Extend pages

Much of `docs/extend/` is executable, so edit it with
these ties in mind:

- **The custom-backend example runs.**
  `tests/unit/docs/test_extending_backends_example.py` finds the one
  `python` fence on `docs/extend/extending-backends.md` that defines
  `class XmodemTransfer`, `exec`s it as the page spells it, puts it through
  `assert_transfer_backend_conforms` and one real `put_files` of two files.
  Exactly one fence may define that class.
- **Imports on these pages must be public.** `scripts/api_teaching.py` is
  gated in CI; run it locally with `make check-api-teaching`. It reads
  every code block under `docs/` (except `docs/superpowers/`) and in the
  top-level `README.md`, and the whole of
  `docs/examples/` and `src/otto/examples/`. Among other things, it refuses:
  - an import, a dotted access or a `"module:Name"` string that names an
    undeclared path;
  - a star import;
  - a taught use of an otto underscore member.

  The full list of finding kinds is in that script's module docstring.

  The docs declare nothing. To teach a new name, add it to its namespace's
  `__all__` first ({doc}`api/stability`).

## Coverage reports

### From pytest

```bash
make coverage
```

Any `make coverage-*` target writes the HTML report to `reports/coverage/html`
on its final pytest leg. A bare `pytest` run prints the `term-missing`
console report only. Nox sessions run with coverage off and print none
(the one exception is `tests_hostless` on the primary Python, which keeps
the CI floor — see the coverage-floors note at the top of `noxfile.py`). The
HTML tree is asked for per lane, not by `pyproject.toml`'s `addopts`, so the
release legs (the ones the census in `tests/unit/test_lane_invariants.py`
enumerates) do not each rewrite it.

The HTML report lists the tests that covered each line (click a line's context
marker); that costs about 17 s of render time and roughly 114 MB of HTML per
report.

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

### The import budget

Every command's startup cost is gated in file operations, per surface and per
Python minor. What is counted, how the ceilings and target ratios work, and
the bytecode cache the harness keeps (including how to clear it) are on
[Startup performance on network filesystems](architecture/startup-performance.md#what-holds-these-numbers-in-place).
The workflow:

```bash
make profile                              # enforce the ceilings (scripts/import_budget.py --check)
uv run python scripts/import_budget.py    # print the table, gated and tracked surfaces, without checking
uv run python scripts/import_budget.py --report-json budget.json   # the same, with full breakdowns
make import-snapshot                      # regenerate THIS interpreter's ceilings file
```

- **A release compares against the previous tag, not just the ceilings.**
  `make budget-delta` (a stage of `make release`, right after `check-python`)
  measures the nearest tag and HEAD on every nox interpreter, the primary
  first, each in a pristine worktree with its own venv, with the tip's
  `import_budget.py` as the one instrument, and stops at the first minor on
  which a surface costs more file operations than it did at the tag, once a
  second measurement confirms it — growth the 10 % headroom would have let
  through. A growth stop prints the entry to paste, whenever the moved
  buckets can be written as a word (a bucket name holding `,` or `=`
  cannot: rename the path):
  `make release BUDGET_APPROVE="help@3.14=stdlib+4,otto:*+1 run=otto:*+1"`
  names each grown surface with the exact buckets it moved by (`@minor`
  scopes an entry; without it the entry applies wherever the surface grew;
  one entry per surface and minor). A surface named without buckets,
  buckets that differ from the measurement, or an entry nothing grew for
  over the whole run each stop again. It is a command-line variable (an
  environment value approves nothing) and the release's own knob, like
  `BUMP=`: the coverage stamp accepts it. Growth or a bucket shuffle is
  measured a second time before it counts, a third when the second
  disagrees, and a minor that never reproduces itself stops as inconclusive,
  naming the surfaces; the tables shown are the deciding pair's, or every
  pair's, labelled, for a minor that stayed inconclusive. The
  base's records are cached under `reports/budget/<tag>/` of the checkout
  that measured them (per checkout, like every `reports/`) and reused while
  their sha, tree, interpreter version and instrument (the harness and
  `tests/_fixtures/` at the tip) still match; the tip is measured every
  time. Exit 0 is accepted (nothing grew, or every growth is approved and
  attributed); exit 1 is a policy stop; exit 2 is invalid evidence or an
  operational failure (a failed sync or measurement, a malformed approval
  word, base and tip the same tree, a git or filesystem error), possibly
  after earlier minors were judged and printed. Surfaces the tag cannot run
  (a verb added since, or a run that did not end as the surface expects at
  the tag — its reason is printed) are listed, not judged; when the
  generated project's dispatch surface cannot run at the tag, every project-based
  surface is listed with it. Each step's elapsed seconds are in
  `reports/budget/run.log`. Each side and minor is measured in
  `$TMPDIR/otto-budget-delta/<side>-<minor>/` (two worktrees and a venv,
  about half a gigabyte, removed before and after every measurement); the
  harness's bytecode cache, `~/.cache/otto/import-budget-pycache`, persists
  and holds one mirror per side and minor. The holder is one per box and per
  `<side>-<minor>`, shared by every checkout of the repository, so two
  releases must not run `make budget-delta` at the same time on one machine:
  each would remove the other's venv, and the loser fails loudly with status
  2, never with a wrong verdict.
- **Regenerate per interpreter.** `make import-snapshot` rewrites only the
  running interpreter's `tests/unit/import_budget/ceilings/<major>.<minor>.json`.
  For another minor, install its nox env and run the script with that
  interpreter: `uv run nox -s tests_hostless-3.14 --install-only`, then
  `.nox/tests_hostless-3-14/bin/python scripts/import_budget.py --update`.
  A missing-baseline failure prints the same commands for the interpreter
  it ran on.
- **Never regenerate to absorb a cost increase without saying so.** A
  regeneration that raises a baseline goes in a commit whose message says
  which surface grew, by how much, and why the verb needs it; the failure's
  breakdown names what grew. The same holds for raising a `target_ratio`.
- **An advisory `NOTE` is an invitation, not a failure.** It says a counter
  now sits well under its ceiling; regenerate to pin the saving.

### Where the test suite's time goes

Every `make` test target writes JUnit XML under `reports/junit/<target>/`, so
after a gate run the timings are already on disk. `scripts/junit_durations.py`
reads them:

```bash
make unit-outliers                              # the unit tree's slow tests, last coverage-python leg
make unit-outliers UNIT_OUTLIERS_THRESHOLD=0.5  # the same, at or above half a second
make unit-outliers UNIT_OUTLIERS_XML="reports/junit/coverage-unit/coverage-unit.xml reports/junit/coverage-unit-serial/coverage-unit-serial.xml"  # hostless: after make coverage-unit
uv run python scripts/junit_durations.py --by-file reports/junit/coverage-python/*.xml  # per module
uv run python scripts/junit_durations.py --top 40 reports/junit/coverage-python/*.xml   # slowest cases
```

`make unit-outliers` reads the two reports `make coverage` (or `make
coverage-python`) last wrote, one for the parallel leg and one for the
`serial_timing` leg, and prints how the `tests/unit` time is spread (count, summed time, nearest-rank p50/p90/p95/p99, and the share held
by tests at or above the threshold, 1 s by default), then every such test,
grouped by file, slowest file first.
A file whose source names `subprocess` or in-process pytest is marked so: a
hint at the kind of cost, read from the text, never a measurement. `make
coverage` prints the ten slowest files the same way after its Python leg, and
only when that leg passes. On a machine without the lab VMs, run `make
coverage-unit` and point `UNIT_OUTLIERS_XML` at its two reports, as above.

It is a report, never a gate: it exits 0 whatever it finds, and what it
cannot read is named and left out (a missing or malformed report, one in an
encoding it cannot read, a test time that is not a number) or quietly dropped
(the hint of a source it cannot read); a failure while reporting is logged
with its traceback, never raised. A wall-clock assertion fails on a loaded
machine with no defect behind it, so a slow test is something to read and act
on, not something to fail a build over. A JUnit
time is setup, call and teardown together; for the split, re-run the file
with pytest's `--durations=N`.

### Register built-ins by reference

A registry's built-in entries are registered in the module that defines the
registry, as a {class}`~otto.registry.Ref` naming the implementation
(`Ref("otto.host.transfer.nc:NcFileTransfer")`), never by the implementation
module registering itself on import. Listing the registry then imports
nothing, and a lookup imports only the entry it names. The mechanism and its
guards are on {doc}`architecture/subsystems/registries`.

### Never branch on import state

Startup savings are structural, never conditional.

- **Allowed:**
  - declarative tables (a lazy package's `_LAZY_*` table, a registry's
    references);
  - one configuration line;
  - an override on the class that owns the behaviour;
  - deletion;
  - a function-scope import in the one function that genuinely needs a
    heavy dependency.
- **Not allowed:**
  - branching on import state: `sys.modules` probes, `try: import … except
    ImportError` for speed, or flags recording what is loaded;
  - helpers or guards whose only purpose is dodging an import.
- **A deferral must earn its place.** Each change names its measured saving
  on a gated surface. A change the ceilings cannot see is not made. An
  optimization that cannot be expressed structurally is not made.

`.ast-grep/rules/no-branching-on-import-state.yml` flags a `sys.modules`
probe; its `note:` says what it cannot see, and names the two sites it
allows by exact shape. Neither is a precedent. A new one needs a
maintainer's agreement and its own exemption in the rule.

### Import bans

The ceilings measure cost; they do not say which import caused it. Two
layers name an edge that regrows:

- **Direct edges** are ast-grep rules, run by `make lint-arch`, and name the
  offending line itself:
  `.ast-grep/rules/lazy-package-init-stays-lazy.yml` keeps every package's
  `__init__` free of eager imports, and
  `.ast-grep/rules/cli-command-no-module-scope-heavy-import.yml` keeps a short
  denylist of heavy modules off the top of `src/otto/cli/` modules. A new
  denylist entry needs a measured cut behind it; the rule's `note:` lists the
  current ones. `tests/unit/test_import_ban_rules.py` keeps that denylist in
  step with the lazy tables: a lazy name that loads a denylisted module is
  banned too.
- **Transitive edges** are rows in `IMPORT_CONTRACTS` in
  `tests/unit/test_import_contracts.py`: an import statement mapped to the
  modules it must never load. A failing row prints the chain of imports that
  loaded the module, ending at the line that imported it; the last otto frame
  in that chain is the edge to cut. `IMPORT_MUST_LOAD` in the same file is
  the positive control: what an entry must load, so that a row cannot pass
  by loading nothing at all. `IMPORT_ONLY_DECLARED` is the allowlist form,
  for a module every layer reads: importing it may load, of otto's own
  modules, only itself and what `tach.toml` declares it depends on, so a new
  import fails without anyone having thought to forbid it. tach's list says
  what a module may import anywhere, so a row names the dependencies its
  module imports only inside a function; those must not load at import, and
  every other declared dependency must.

## AI-Assisted Contributions

AI coding tools (e.g., GitHub Copilot, Claude, Cursor) are permitted for
contributions to otto. If your PR contains AI-assisted code, please note it
in the PR description. Regardless of how code was generated, contributors
are responsible for understanding, testing, and owning what they submit.

AI-assisted commits carry an `Assisted-by:` trailer naming the model that
actually produced the work — the trailer is a per-commit provenance claim, so a
stale or approximate name defeats its purpose. An AI agent writes the trailer
into the commit message itself; nothing adds it for you.
