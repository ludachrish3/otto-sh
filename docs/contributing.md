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
| `zephyr` | `10.10.200.14`  | no          | Zephyr RTOS test bed (7 QEMU instances) + the chaos reboot guest + SSH hop to them |

The `zephyr` VM — otto knows it as the host `test4` — runs **seven** Zephyr
QEMU instances concurrently, spanning three Zephyr versions and two
transports. All of them are reached through its SSH hop (`10.10.200.14`).

It also hosts the Ubuntu chaos reboot guest, an eighth QEMU instance that the
reboot chaos reboots so no lab VM has to be: see
{doc}`architecture/subsystems/chaos-guest`.

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
coverage. Every other guest runs `shell_module`. Building that extension
needs the embedded-gcov submodule initialized first; `tests/project3/product/README.md`
has the command and the rest of its setup.

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
usable UART to the arm64 guests; the lab is built the same way on an x86_64
host. `test2` runs a getty on a pseudo-terminal
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

Each VM gets 900 s to come up before `vagrant up` gives up on it, more than
Vagrant's own 300 s, because several VMs booting at once (especially on a host
that is itself a VM) can take longer than that to reach SSH. Set `OTTO_LAB_BOOT_TIMEOUT` (seconds) to change
it, for example `OTTO_LAB_BOOT_TIMEOUT=1800 vagrant up test1 test2`.

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

Some of the suite needs system packages that `make dev` does not install.
Provisioning the dev VM (the `dev-root` step) installs them; on a dev VM
provisioned before they were added, `vagrant provision dev` installs them. On a
machine set up another way, install them yourself:

- `strace` (≥ 5.3): the import-budget tests count file operations with it.
- `gcc-11`, `clang` and `llvm`: the kmodcov reader tests
  (`tests/unit/cov/test_kmodcov_reader.py`) build `otto_kmodcov`'s own C with
  a gcc before 12, the default gcc and clang, and read what it writes with
  each compiler's gcov.
- `libgtest-dev`: the GoogleTest product the hierarchical coverage e2e builds.
- `sshpass`: lab SSH for `make vm-health` and the integration tests' wedge
  diagnostics.
- `linux-headers-$(uname -r)`: the kmod coverage e2e builds its module
  against the running kernel. If the archive no longer carries your kernel's
  headers, `dev-root` fetches them from Launchpad.

```bash
sudo apt-get install strace gcc-11 clang llvm libgtest-dev sshpass
sudo apt-get install "linux-headers-$(uname -r)"   # on its own: it may have left the archive
```

If the archive no longer has them, do what `dev-root` does: from
<https://launchpad.net/ubuntu/+archive/primary/+files>, fetch
`linux-headers-$(uname -r)_<build>_<arch>.deb` (the build is the second field
of `/proc/version_signature`, up to the flavour, e.g. `6.8.0-86.87`), then the
common headers deb its `Depends` names, and install both with `apt-get install
./<file>.deb`.

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
dashboard already built and never needs Node. Building a *wheel* from source
does need it, to build the embedded web frontends first:
`make web-install && make web && uv build --wheel`. Installing a released
version never does; the published wheels already embed the frontends.

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
parallel per-engine matrix. A failed engine job uploads the failed tests'
Playwright traces and screenshots as the `playwright-traces-<browser>`
artifact; open a trace with `npx playwright show-trace <trace.zip>` (or at
trace.playwright.dev) to step through its clicks, DOM, console and network.
Locally, `make dashboard` leaves failure screenshots in `reports/playwright/`.

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
`.ast-grep/rule-tests/`, the [duplicate-code ratchet](#duplicated-code), and
the four parse-only tests behind
[Patching lazily exported names](#patching-lazily-exported-names)), so it
matches CI's `lint-python` job, which is `nox -s lint` and has always run
them; CI runs the four import-rule tests in its `tests` job. It ran ruff only until 2026-08-10, and the difference
was invisible: a file could pass `make lint`, `make format` and every
coverage lane while still violating an architecture rule. Those rules mostly
police *test* code — deadline polls, `parents[N]` path arithmetic,
module-scope env writes — so a change that is "only a test" is the one most
exposed to them, not the least.

## Branching and commits

All work branches off `main` and lands through a pull request; the landing
steps are in [Gating a branch before it lands](#gating-a-branch-before-it-lands).

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
internal budget or timeout. Nor is a visual or aesthetic change (layout,
colour, styling, wrapping of otto's own output, where focus starts) that
leaves every function in place: it is a `feat:` or a `fix:`. It becomes
breaking only when functionality changes or is taken away. Neither is any
change to maintainer tooling
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

Before pushing, run what `make gate-scope` names for the diff (see
[Gating a branch before it lands](#gating-a-branch-before-it-lands)); `make all`
(`clean-dist → typecheck → coverage → docs → build`) is the release's whole
build, not the gate.

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

### Measuring interface churn

The number of marked commits in a release says how often a break was marked,
and the marking rule has changed over time, so two eras' counts do not
compare. `scripts/interface_churn.py` measures the interface itself. It is a
report and never a gate:

```bash
uv run python scripts/interface_churn.py                        # every release, then HEAD
uv run python scripts/interface_churn.py v0.17.0 v0.18.0 HEAD   # these windows only
uv run python scripts/interface_churn.py v0.18.0 HEAD --detail --unrecorded
```

Each consecutive pair of revisions is a window, and each window is one row.
With no revisions, the windows run between the release tags that `HEAD`
contains (a tag cut on a branch that never merged is left out), then to `HEAD`.

| Column | Meaning |
|---|---|
| `commits`, `marked` | The window's commits, and those that count as breaking by today's `cliff.toml`, whatever the rule was when they landed. |
| `surface` | The sites the older golden records: every public binding, member, input and versioned format. |
| `broken`, `churn` | The older golden's sites that broke, and their share of `surface`. A site is broken when a breaking finding names it, when it gained a required input, or when its binding is gone. The findings come from the comparison `make check-breaking` runs on each commit, applied here to the window's two ends. |
| `findings`, `distinct` | Every breaking finding, and the findings left once those that differ only in namespace are merged. A gap between the two is one change seen under several namespaces: one function exported twice, or one decision restated. |
| `added` | The sites the newer golden records and the older does not. |

The recorded columns are the net difference between a window's two ends, not
an audit of marks. A name removed and restored inside one window does not
show. Neither does a change to `api/public.toml` alone, nor any change of
behaviour.

A window shows `-` in every recorded column when it is not comparable: either
revision has no golden, or the two goldens have different schemas. The cutover
to the API dump is commit `14caec5b6`, inside 0.17.0; pass it as a revision to
measure the rest of that release. Two goldens of the earlier line format are
compared by line: `surface` is the older golden's lines, `broken` the lines
gone or reworded, and `findings` and `distinct` show `-`.

`--detail` lists each window's broken sites by namespace and its distinct
findings. `--unrecorded` adds the two surfaces no golden records yet, the
command-line flags and otto's environment variables, approximated by the
distinct spellings in `src/otto` at each revision. A flag that moved between
commands, or left one of two, does not show, and a constant that only looks
like a variable name is counted.

### Gating a branch before it lands

`make gate-scope` tells you which gates a commit needs. It reads the diff and
prints two command lists: one for **this commit** (`HEAD^1..HEAD`, the first
parent, so a merge counts only what it brought in) and one for the **tip**
(the merge base with `BASE`, default `origin/main`, to `HEAD`: what lands as a
whole). Only the **tip** of a landing is gated: whatever reaches `main` in one
landing, whether one commit or several that persist one by one, runs the tip
list once, on its final tip. Every commit before the tip runs only the
tests in its likely breakage areas plus ruff on the files it changed, never a
gate. The commit list is a quick check you may run while working on a commit;
it is not a landing gate. It refuses a dirty tracked tree (commit first) and a `HEAD` with no
parent (exit 2), and exits 1 when git cannot answer (a missing base, no
repository). `BASE` and `make quick`'s `PATHS` are read from the command line
only, never from the environment.

A landing is a pull request, and its checks are split by who can run them.
**The PR's CI runs everything that needs no lab**: every validation job in
`.github/workflows/ci.yml`, the hostless matrix (the `tests_hostless-3.10` …
`3.14` contexts: `tests/unit` and the no-VM e2e, one nox session per tier,
each on the Pythons [the session table](architecture/testing.md#other-axes-typescript-doctests-cross-python)
gives it; the two bookend contexts are certificates over
the primary's and the canary's sessions, and the 3.10 one also needs the
coverage fold, see [the quality-gates table](architecture/quality-gates.md#what-binds-each-gate)), the
docs build and the TypeScript gates among them; the CI column of [the
quality-gates table](architecture/quality-gates.md#what-binds-each-gate) names
them, and only the issue-filing job waits for a failed run on `main`, as
nightly's does: a failed pull request never files an issue. **The
local tip adds what CI cannot run, and the cheap static lanes beside it**:
`make check-breaking` over the landing range; `make check-python` and `make
collect-check` for any diff that touches Python; and the bed lanes the diff
reaches, `make coverage-bed` (the bed half of `make coverage`'s selection) and
the diff's own bed lanes ([the class table](#gate-class-table) below has every
column). The hostless matrix, `make docs` and `make check-ts` are not local
tip lanes: CI runs them before anything lands, and `make nox-hostless` stays
for running the matrix by hand. A tip that reaches no bed lane costs about
50 s, most of it `make check-python` (36 s on the dev VM with a warm cache,
about 70 s cold).

The flow, from a branch whose commits are ready:

1. `make gate-scope` prints what the tip will run for your diff (commit
   first: it refuses a dirty tree).
2. `make gate-tip` runs the tip over `merge-base(main, HEAD)..HEAD`, `main`
   being `origin`'s, fetched (`BASE=` names another): the static lanes for a
   Python diff and the bed lanes it reaches ([the lease only while a bed
   lane runs](#the-landings-gate-is-stamped)). Each green lane is
   [stamped](#the-landings-gate-is-stamped) for the committed tree. On green it
   posts the `local-gate` status on `HEAD` when the branch is already pushed;
   on a branch's first gate GitHub does not have the commit yet, so the run
   says `local-gate not posted: push the branch, then make gate-status` and
   still exits 0: the gate passed.
3. `git push -u origin <branch>`. The pre-push hook (`.githooks/pre-push`)
   checks every branch push: it refuses a tip whose lanes are not stamped,
   runs `make check-breaking` over `merge-base..HEAD` for the checked-out
   branch, and refuses any push to `main`. Before its live check it refuses a
   dirty tracked tree or an untracked source, because that check reads the
   tree on disk, not the commit: commit or stash the edit, then push again.
   On a stamped tree it takes about 20 s: the fetch of `main` and `make
   check-breaking` over the landing range (18 s for a 13-commit landing).
4. `make gate-status` posts the `local-gate` status on `HEAD` after
   re-checking that every stamped lane is green for its tree. When the pull
   request's CI run already exists it waits for that run's `gate-admission`
   job and re-runs it if it failed, because a late status starts no run by
   itself. Posting twice is harmless: GitHub keeps the latest state.
5. `gh pr create` (see [Pull requests](#pull-requests)), then wait for CI.
   Its first job, `gate-admission`, verifies [the `local-gate`
   status](#the-landings-gate-is-stamped) against the head, and every
   validation job waits for it. A missing or mismatched status fails it
   with the remedy: `make gate-tip` on this head, then `make gate-status`.
   A pull request Dependabot opened is admitted without the wait; see
   [Dependency updates](#dependency-updates).
   A branch forked before this flow landed does not carry the admission
   script at its head, so `gate-admission` fails with a Python error until
   the branch is rebased onto `main`.
6. The maintainer reviews the pull request and presses **Rebase and merge**
   once CI and the `local-gate` status are green. `main`'s ruleset requires
   both, keeps the history linear and allows rebase-merge alone (squash and
   merge commits are off), so the landing commits keep their messages and
   the breaking marks the changelog reads.

`make land` runs steps 2 to 5 in one command from a feature branch: it gates,
pushes, opens the pull request as a draft (an existing one is left alone) and
runs `make gate-status`.

(landing-a-batch)=

#### Landing a batch

Several fixes can land as one pull request. A batch branch is one landing:
one commit per fix (each with its own conventional subject), one tip gate over
the final tip, one pull request and one rebase-merge. Every commit before the
tip gets only its scoped working check (the tests in its likely breakage areas
plus ruff on the files it changed), never a gate of its own. The integrator
owns the branch.

- **A red fix leaves the batch.** A fix whose own check is red is dropped
  before the tip gate and re-queued together with anything that depended on
  it. It is never carried along and never fixed in place by someone else. A
  drop after the tip gate is a new tip and needs a new gate.
- **A breaking (`!`) commit may ride a batch** when its subject is the
  changelog line, even a batch whose tip plan has a bed lane. A rebase-merge
  keeps every commit's message, and the changelog and `check-breaking-marks`
  read each one. The pull request's title and body list every `!` subject.
- **The ordering rule is a cost rule.** In a wave of landings, put those whose
  tip plan has a bed lane first and those whose plan has none after them: a
  landing whose plan has none should not merge while a landing with a bed lane
  sits between its gate and its merge, because the rebase that follows would
  re-run the bed. [The carry rule](#the-landings-gate-is-stamped) relaxes the
  rule without removing it: when every intervening landing was a tip plan
  without a bed lane, the bed stamp carries when the carry conditions hold,
  so the bed does not re-run; the static lanes still do.
- **One lease per tip gate.** Bed fixes for the same lab hosts may be grouped
  into one batch, whose single tip gate takes one [bed lease](#bed-lease)
  across its lanes. Separate landings take separate leases. No lease is held
  while waiting for CI or for the merge, and grouping a batch never narrows
  the selection the tip gate runs. The arithmetic — *n* scoped checks plus one
  tip gate instead of *n* tip gates — is priced in
  [what a gate costs](#what-a-gate-costs).

**The behind-`main` gap.** The ruleset does not require a branch to be up to
date with `main`, so a rebase-merge of a branch behind `main` lands a tree no
gate ran on: the head's commits replayed onto changes they never saw. CI on
`main` runs after the landing and catches it there (a red run files a "CI
failed on main" issue); the gap is documented rather than closed by a merge
queue because landings are rare, and the dedicated bed runner (#801) is the
structural fix. `gate-admission` compares the status's base with the merge
base it computes, never with `main`'s tip, so a branch behind `main` is still
admitted. When `main` has moved in an area your change touches, rebase onto
it and gate again before asking for the merge; a bed lane's stamp survives
that rebase only under [the carry rule](#the-landings-gate-is-stamped).

**The status is an attestation, not a proof.** Anyone with push access can
post a `local-gate` status with `gh api`, as `git push --no-verify` skips the
hook. The trust model is author honesty plus CI: the status makes the honest
path checkable, it does not prove a bed run. Posting needs write access to
the repository. The author posts it from the dev VM, where every contributor
works today under the maintainer's `gh` login; for a pull request from a
fork, the maintainer fetches the head, runs `make gate-tip` and `make
gate-status` on it, and the status lands on the head's sha whoever posts it.
CI's `gate-admission` runs the admission script from the pull request's own
head, so a fork can edit that script; what it cannot do is post the status,
which the ruleset requires on its own, so the edit buys a fork nothing but a
CI run.

`main` takes no direct push, from anyone: the pre-push hook refuses it, and
`main`'s ruleset refuses it too through its `pull_request` rule, which no
actor may bypass for a push. Nothing reaches `main` but a merged pull
request, [the release](release_process.md)'s included. The one escape is
[the hotfix](#hotfix), which is still a pull request.

(dependency-updates)=

#### Dependency updates

Dependabot cannot run the local gate, so `gate-admission` admits a pull
request whose author is `dependabot[bot]` without waiting for the
`local-gate` status, and CI runs every hostless lane on the bump. That
verdict tells the maintainer whether the bump is worth a bed run. The
ruleset still requires the `local-gate` status, so nothing Dependabot opens
merges ungated. To land one whose CI is green:

1. `gh pr checkout <n>`, then `make gate-scope` to see the lanes; expect
   bed lanes under [the lease](#bed-lease).
2. `make gate-tip`. Dependabot's head is already pushed, so its green end
   posts the `local-gate` status itself; `make gate-status` re-checks it.
3. **Rebase and merge**.

Dependabot may rebase its pull request (on a conflict, a newer release, or
an `@dependabot rebase` comment). That makes a new head the status does not
cover, so gate just before the merge.

`.github/dependabot.yml` sorts the bumps into pull requests by what landing
them costs. Its comments name the members of each kind and cite the
history behind each choice:

- **A tool whose new rules fail unchanged code** comes alone on every bump,
  so it lands with the fixes it asks for.
- **A framework otto's own code is written against** comes alone. Its break
  is a product change, so check whether the fix is
  [breaking](#branching-and-commits).
- **A lockstep set** moves as one pull request, because no member can pass
  alone.
- **`routine`** takes every other patch and minor bump, one pull request a
  week for Python and one for the dashboard's npm packages. A major outside
  a lockstep set normally comes alone, though it may wait while a
  `routine` pull request is open.

When a bump needs code fixes, put them on the Dependabot branch ahead of
its commit, or land them first in a pull request of your own. Dependabot
stops rebasing a branch someone else has pushed to.

(gate-class-table)=

Each changed path has a class; a diff takes the union of its paths' classes:

| Class | Paths | This commit | Tip |
| --- | --- | --- | --- |
| `none` | `docs/superpowers/specs/`, `todo/`, `AGENTS.md` | `make check-breaking` only | `make check-breaking` |
| `docs` | `README.md`, `docs/` (not `conf.py`, `_ext/`, `examples/`) | `make docs` | `make check-breaking` (plus `make docs-captures-check` for a capture input) |
| `web` | `web/src/` (not generated `*.gen.ts`) | `make check-ts`, `make test-ts` | `make check-breaking` |
| `tests-leaf` | a test module whose tests and marks are unchanged (an added test is a leaf change unless it carries a mark), and the two measured matrices, over their reader modules | `make quick` over its package, ruff, `make lint-arch`; a module whose every test carries `interpreter_agnostic` runs `make quick-guards` instead of `make quick` | `make check-breaking`, `make check-python`, `make collect-check`, and `make coverage-bed` when a changed module is in the bed half (`BED_REACH`) |
| `tests-structural` | a test module that removes or renames a test, adds a marked one, or changes a mark (its arguments too) | the leaf lines plus `make quick-guards` | as `tests-leaf` |
| `src-leaf` | one or two `src/otto/` packages | `make quick` over the package's unit and bed trees plus the tree-wide guards, ty, ruff, `make lint-arch`, `make quick-guards` | `make check-breaking` and the static lanes, `make coverage-bed`, the package's bed lane |
| `scripts` | `scripts/` | `make quick-guards`, ty, ruff | `make check-breaking` and the static lanes, and `make coverage-bed` when a changed script is one the bed half runs on (`BED_SCRIPTS`) |
| `workflow` | every `.github/workflows/` file except `ci.yml`, each a row in `WORKFLOW_ROWS` (a new workflow needs one) | `make quick` over the test modules that read the workflows, `make quick-guards` for those in the `interpreter_agnostic` family | `make check-breaking` |
| `escalate` | a conftest, `tests/_fixtures/`, the registry, a package `__init__`, a build file (`Makefile`, `noxfile.py`, `pyproject.toml`, CI's `ci.yml`, which decides what a pull request runs), three or more packages | the full gate, the bed lanes, plus `make quick-guards` (a build file or a `workflow` path in the diff) | `make check-breaking` and the static lanes, `make coverage-bed` and the bed lanes |
| `full` | any path with no row | the full gate | as `escalate` |

The static lanes are `make check-python` and `make collect-check`: every
diff that touches Python (a Python class above, or any `.py` path, a Python
file under `docs/examples/` included) runs them after `make check-breaking`. `make check-python` is `make lock-check` (the lock
matches `pyproject.toml`), `make lint-python` (ruff and the architecture
gates of `make lint-arch`) and `make typecheck-python`, as recipe lines in that
order; `make collect-check` imports every module under the test roots without
running one. `check-breaking` is the one live goal: it reads the landing
range, so the hook runs it at the push and CI's `lint-python` runs it over the
pull request's range. Every other tip lane is a function of the tree and is
stamped. A tip whose diff touches `src/` or a docs capture input (the
example captures and the two capture scripts) also runs `make
docs-captures-check`, a bed lane.

**`BED_REACH`** (`bed_reach()` in `scripts/gate_scope.py`) decides whether
the tip runs `make coverage-bed`. It holds
when the diff's classes include `src-leaf`, `escalate` or `full`;
when a changed script is one the bed half runs on (`BED_SCRIPTS` in the same
file: a script that a bed-half test module or a conftest above one imports,
directly or through other `tests` and `scripts` modules, and the lease and
the reap that every bed lane's recipe runs);
when a changed `tests-leaf` or `tests-structural` module is in the bed half,
that is, it lives under `tests/integration` or any of its tests carries an
`integration` or `embedded` mark (module, class, function and parameter marks
all count, so a module mixing hostless and bed tests is in the bed half); and
when the oracle recorded a reason for the diff (an unreadable `pytestmark` or
mark decorator, no surviving root, a package's test trees gone), the
conservative fallback, printed under the tip as `(bed half: <reason>)`. A
hostless unit-test change therefore does not select `make coverage-bed`.
Neither does a change to any other script: what it can break, the pull
request's CI can see. That includes `scripts/gate_tip.py`, the runner around
the lanes, and the scripts that build the web bundle. A script with a row of
its own (the two capture scripts above) still selects that row's bed lane. A
bed test module's tip
runs the whole bed half, not a `make quick` over the module: coarser and
longer, the price of one lane.

The full gate is the commit column of an `escalate` or `full` commit: `make
check-python`, `make check-ts` (together, `make check`), `make docs` and `make
coverage-bed`, in that order, plus the diff's bed lanes. The lint, type and
docs checks run first, so that run by hand a lint, type or docs failure costs minutes rather
than the seven minutes the bed half takes to reach it. The tip never runs the full
gate. Beyond the `escalate`
and `full` classes, the commit column is the full gate when a test module
carries a `stability` or `chaos` mark outside every lane's files, carries a
`pytestmark` (or a mark decorator) the oracle cannot read, or leaves no
surviving root `make quick` could run, and when a `src/otto/` path's test
trees are all gone; each reason is printed above the commands. The roots
`make quick` receives for `src/otto/` packages include integration and e2e
bed trees, which need the lab VMs: run them on the dev VM.

(gate-stability-lanes)=

**Stability lanes and the test-isolation guard (`nox-unit-repeat`).**
A package's bed lane rides every tip whose diff touches the package, beside `make coverage-bed`: `make conformance-bed`
for `src/otto/host/`, and a stability tier for `src/otto/link/`
(`stability-unix`), `src/otto/tunnel/` (`stability-tunnel`) and
`src/otto/console.py` (`stability-embedded`). The stability tiers have two more
areas, the stability test files (the files the `stability-*` Makefile targets
run) and the tunnel-stability tree, which name their lanes in both columns. So a
commit that changes only a src leaf under `src/otto/link/` runs no stability
lane before the tip, and the landing's tip runs it. The probe-line cut sweep
(`make stability-cut-sweep`) names its lane in the commit column only, as a
working check. No tip runs it; it runs in nightly's `probe-cut-sweep` job,
and once by hand before a change to it lands. Whichever area selects it, a
stability lane runs **once**: `make stability-<tier> COUNT=1`, and for the tunnel
tier, whose tests loop internally, `make stability-tunnel COUNT=1 CYCLES=2`, its
smoke depth. It never runs at the Makefile's soak count or depth, and a diff
outside those areas runs no stability lane at all. Outside the gate, nightly CI
soaks `stability-unit` at `COUNT=100` on every Python, and `make release-campaign`
runs `make stability COUNT=1` in each release: a single pass of the unit, unix and
embedded tiers, and the tunnel tier at its full depth (`CYCLES` 5). A
`stability-unix` soak (its default `COUNT=10`) or an embedded one runs only by
hand. So a regression that only a soak shows is found at nightly for unit, at
release for tunnel, and only by a hand-run soak for unix and embedded; never at
the commit. The test-isolation guard is never a local lane, and since #795 not a PR
check either: nightly's `unit-repeat` job runs the same session once a night.
At about an hour it was CI's longest job by a factor of two and set every
PR's wall clock, while the leak classes it was built for are restored per
test by the root conftest or purged by their fixtures; what it still catches
is the class nobody has named yet. The target stays for running it by hand.

Some test trees are a lane's own and are never handed to `quick`. Both `quick`
legs deselect the items of `tests/conformance/` (`make conformance`; the
bed-opener witness runs only under `make conformance-bed`), `tests/busybox/`,
the tunnel-stability tree and the chaos tree by marker (`conformance`,
`busybox`, `stability`), and browser modules, which route to `make dashboard`
by their `browser` mark, by `not browser`. `tests/integration/busybox_bed/` is
different: its items carry only `integration`, which `quick` and `make coverage`
do not exclude, so its row (`make coverage-unix` at the commit) is what keeps
it out of `quick`; at the tip it names nothing, because `make coverage-bed`
runs its `integration`-marked items. A lane row names the lanes in both
columns, even when the column is the full gate, because `make coverage`
excludes those tiers, and at the tip only its bed lanes survive; a conftest or helper inside one of those trees keeps its
own class (a conftest escalates) and adds the tree's lanes as well. A *mixed* file (a stability file that also holds
unmarked tests) runs its lane for the marked tests and `quick` over its
package for the rest, when there is a rest. A module carrying `kmodcov`,
`browser` or `busybox` runs that lane wherever it lives; a hostless module
inside the dashboard tree is a plain leaf and runs under `quick` like any other.

`tests/unit/scripts/test_gate_scope.py` keeps this honest: every tracked path has
a row, and an oracle census proves that every item pytest collects in every
test module, changed alone, is selected by a lane the oracle emits.

The bed lanes take [the bed lease](#bed-lease), so a second landing's gate on the same cell waits (`BED_LEASE_WAIT`) or fails fast naming the first ([`make gate-tip` only while a bed lane runs](#the-landings-gate-is-stamped)). A gate you interrupt (Ctrl-C, or a SIGTERM to `scripts/gate_tip.py` itself) waits for the lane it was running to stop, then, when it holds the lease, reaps the bed and releases the lease itself; a second Ctrl-C kills the lane outright, and the gate reaps and releases the same way once the lane's processes are gone, or, when some outlive the kill, keeps the lease, names them and tells you to run `make bed-reap` once they are ([the bed reap](#bed-reap)).

A stamp is keyed by the committed tree, so a rebase or an amend that keeps the
tree (a reworded message, commits squashed on the branch) keeps the stamps:
`make gate-tip` on the new head finds every lane stamped and re-posts the
`local-gate` status for the new sha in seconds. A rebase onto a `main` that
moved changes the tree: the static lanes run again (about 50 s), and a
**bed** lane's stamp is **carried** to the new tree when all four hold:

1. `main` advanced from the base the stamp was written on.
2. Replaying the landing's patch onto the new base, as a rebase does, gives
   exactly the new tree (an amended patch, a whitespace-only amendment, or
   the same edit moved elsewhere gives another tree and refuses).
3. No red marker for the lane exists on the same patch, on any base behind
   the new one; a marker whose base cannot be placed or whose patch cannot be
   replayed refuses the carry, and a marker written before bases were
   recorded blocks only its own tree. A marker keeps the patch it went red on
   beside it (`<marker>.patch`), so it still replays once its tree is pruned;
   one without that file replays from its tree while git holds it.
4. `main`'s delta, the old base to the new one, has no bed lane by `make
   gate-scope`'s judgement, as if that delta were a landing; a plan that
   cannot be computed counts as having one.

Static lane stamps are never carried. Only the runner (`make gate-tip`)
carries: it writes the carried stamp, with the tree it came from recorded as
`promoted_from`, and prints `already stamped (promoted from <tree>: the
patch replays onto <new base>, main's delta <old base>..<new base> has no
bed lane)`. When no candidate carries, the runner prints `no carry: <reason>`,
the first condition the newest candidate tried failed, and the lane runs.
`--dry-run` previews a carry as `would carry (promoted from <tree>)`. The pre-push hook and `make
gate-status` never carry; they see the stamp the runner wrote, exactly.
`--timings` lists it as `promoted` ([What a gate costs](#what-a-gate-costs)).

The carry has one known false miss: a `main` edit inside the three context
lines of one of the landing's hunks, which the rebase merged cleanly, replays
to another tree and the bed lane runs again. That errs on the safe side. The
trust argument is the behind-`main` gap above, accepted one step earlier: a
rebase-merge already lands a tree no gate ran on and CI on `main` catches it,
and a carry reuses a bed run only when `main`'s delta would itself have
landed without one. CI on `main` covers its own selections; a bed-only
interaction between the landing and a bed-free delta is an accepted,
unvalidated gap until #801 gates the merge result. Either way, run `make
gate-status` after the push: the status belongs to a sha, and the old head's
does not carry over.

### Hotfix

A hotfix is a pull request like any other; only its checks are skipped. It is
for extreme cases: `main` is broken for users or a release is wedged, and the
gate or CI cannot be made green in the time the fix can wait.

1. Push the branch with `git push --no-verify` (the hook's escape hatch) and
   open the pull request with `hotfix:` at the start of its title and the
   `hotfix` label.
2. The maintainer merges it with the button's "merge without waiting for
   requirements" option, which `main`'s ruleset grants the repository's
   admin role for a pull request and for nothing else (the repository's
   merge settings still make it a rebase-merge). GitHub records every bypass
   in the ruleset's rule insights, which is where the skips are counted, and
   `gh pr list --label hotfix --state merged` lists them. The push to `main`
   then runs CI as any landing does, and a red run files the "CI failed on
   main" issue.
3. Afterwards, run the ordinary gate on `main`'s tip (`make gate-tip
   BASE=<the hotfix's base>` on a branch at that tip) to learn whether the bed
   lanes pass on the hotfixed tree; the status it would post is
   informational, since the ruleset judges pull-request heads. The follow-up
   that makes the checks green lands through the normal flow. The release
   workflow's guard requires green CI on the commit it tags, so a hotfix that
   leaves `main` red blocks releases until that follow-up lands.

### The landing's gate is stamped

`make gate-tip` is how a branch earns its `local-gate` status. It fetches
`origin`'s `main` (`BASE=` names another base), asks the oracle for the tip
list over `merge-base(main, HEAD)..HEAD` (what `make gate-scope` prints), runs
every **stamped** lane on it in order, and on green writes a stamp for that
lane, keyed by the committed tree. A lane that already has a stamp for this
tree is skipped, so a re-run after a red lane repeats only what is missing. A
lane that goes red writes a red marker where its stamp would be. Until the
lane passes on that tree, nothing counts for it there, not even a stamp
promoted from another tree. `make gate-tip` deletes only evidence that cannot
still be true: before a lane runs, a corrupt or misnamed file at that lane's
path for the gated tree, that lane's stamps for trees git no longer holds, and
its red markers without a base on such trees. A red marker with a base stays
when its tree is pruned: it is evidence against the patch, which its
`.patch` file still replays. Every other stamp stays, whichever worktree wrote it, so two
worktrees can gate at once and you can gate a second landing before you push
the first. A lock on `otto-gate-stamps/.lock` keeps two runs from
interleaving their writes, and the pre-push check holds it shared while it
reads, so it judges one state of the store and never sees a write half done.
The static lanes (`make check-python`, `make collect-check`) and the bed lanes
are stamped. It skips the one **live** lane, `make check-breaking`: it depends
on the pushed range and the push runs it (`.githooks/pre-push`), so a stamp
for it would certify the wrong thing. `make coverage-bed` is the suite lane of
every `BED_REACH` tip, a pass/fail lane that leaves no coverage stamp; `make
coverage` is never a tip lane. `uv run python scripts/gate_tip.py --dry-run`
lists the lanes without running them and `--force` re-runs lanes that are
already stamped. Every run ends with a timing table, and each stamp records
what its lane cost: see [What a gate costs](#what-a-gate-costs).

**Suspended lanes.** The owner can hold a lane back without touching the
oracle: `SUSPENDED_LANES` in `scripts/gate_scope.py` maps a lane key to its
reason, and that table is the one place that decides. The oracle still names
the lane (so `make gate-scope` and the census keep saying what a change
reaches, with the reason beside it), but the plan lists it under `suspended`
rather than `stamped`: `make gate-tip` prints it and runs nothing, writes no
stamp, the pre-push check demands none, and the `local-gate` digest omits it.
Today the table is empty. It held `make chaos` and `make chaos-embedded` from
2026-11-09, while a warm reboot stranded a lab VM
([#978](https://github.com/ludachrish3/otto-sh/issues/978));
[#985](https://github.com/ludachrish3/otto-sh/issues/985) moved the reboot scenarios
onto [the chaos reboot guest](architecture/subsystems/chaos-guest.md), whose reboot
strands nothing, and emptied it.

`make gate-tip` takes [the bed lease](#bed-lease) itself, immediately before
the first bed lane that will actually run, keeps it through the last, and
releases it before anything is posted. A tip with no bed lane, or one whose
bed lanes are all stamped already, takes no lease and goes green while
another landing holds the bed; `--dry-run` takes none either.

**The `local-gate` status.** On green, `make gate-tip` posts a commit status
named `local-gate` on `HEAD`; `make gate-status` posts it for a tree gated
earlier (`scripts/local_gate_status.py` is its one writer and reader). Its
description, `v1 tree=<40 hex> base=<40 hex> lanes=<12 hex>`, is 113
characters (GitHub caps a description at 140) and binds the committed tree
that was gated, the merge base the tip was planned over, and a digest of the
plan's stamped lane keys. CI's `gate-admission` checks out the pull
request's head, plans the tip over `merge-base(base, head)..head` with the
head's own oracle, and waits up to five minutes for a `local-gate` success
whose tree, base and lane digest match; a missing or mismatched status
fails it. A post is re-checked under the stamp store's
exclusive lock, refused unless every stamped lane is green for the tree, so
a status never outruns its evidence. A tip with no stamped lane (a `none`,
`docs` or `web` diff) earns its status too. `make gate-tip` exits 0 on a
green gate whose post did not land (the gate passed, and the run says
`local-gate not posted: <why>`); `make gate-status` exits 1, since posting was
its job. When a lane goes red on a tree whose `HEAD` already carries a
`local-gate` success, `make gate-tip` posts a failure over it, so the remote
evidence follows the red marker. The failure path looks at `HEAD`'s sha
only: a success posted on another sha of the same tree (a message amended
after `make gate-status`) is not flipped, though the red marker still
refuses the hook and `make gate-status` for that tree.

Stamps live under `<git common dir>/otto-gate-stamps/<tree>/`, so every
worktree of the clone shares them. They are keyed by the tree hash and not the
commit because the tree is what an amend, a squash on the branch or a rebase
that changes nothing inherits, so the gate you ran stays valid for it. The
PR's CI and the `local-gate` status are per sha, though: push the final head,
then `make gate-status`, and the head that lands is the one they are green on. Like the
coverage stamp, a stamp is a committed-tree contract: a dirty
tracked tree, or an untracked file the lanes would import, is refused, and so
is any command-line variable other than `BASE=` and the release's own knobs,
`BUMP=`, `NEW_VERSION=` and `BUDGET_APPROVE=` (the Makefile's
`COVERAGE_STAMP_HARMLESS`, which the release's nested `make gate-tip`
inherits); `make gate-tip X=...` is refused. `BASE=` is the one control a
landing passes. The runner shares the coverage stamp's
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

Such a change is classed `none` and adds no lane, so a release branch made of
nothing else needs only `make check-breaking` (`CHANGELOG.md` is on the docs
row, which stamps nothing either). Any other edit to these files, such as a
dependency, a tool setting or a changed verdict, is classified as before: a
matrix whose verdicts improved is a `tests-leaf` change, and the release's own
`make gate-tip` runs its static lanes.

The exemption covers only those files. The release starts from `main`'s tip
exactly (`make release-preflight` refuses anything else), so no other commit
rides its range.

When the pre-push hook finds a stamped lane without a satisfying stamp it
refuses and says which, as in:

```text
gate-tip: refusing refs/heads/ssh-retry (3f9a1c2d4e5b) — its gate is not stamped on tree 7b0c91ae2d44 (range 9e1d4b7a0c33..3f9a1c2d4e5b):
  make coverage-bed: no stamp
run: make gate-tip   (on commit 3f9a1c2d4e5b), then push again.
git push --no-verify skips this check; the local-gate status then stays absent and gate-admission holds the pull request's CI.
```

Each lane's reason is one of `no stamp`, `the lane went red on this tree (…)`, `unreadable stamp`, `command mismatch (…)`, `stamped for tree …, which differs from the tip's beyond the measured outputs (…)` or `stamped for a different path list (…)`; the last is the moved-`main` case below. `make gate-status` refuses with the same lines, `gate-status: refusing HEAD …`.

The check fetches the remote's `main` and recomputes the tip list over the
range the push really carries, so a `main` that moved after your gate
(someone else landed) can make the list longer than the one you stamped.
`make gate-tip` fetches the same `main`, so the remedy is to run it again.

There are two ways out of a refusal: stamp the tip, or `git push --no-verify`.
The second is honest about what it skips: `make gate-status` posts no
`local-gate` status for an unstamped tip, so the pull request's
`gate-admission` fails and holds its CI, and the pull
request cannot land without [the hotfix](#hotfix)'s bypass. (The
test-isolation guard is not on that list: nightly's `unit-repeat` job runs it.)

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

The stamp is a committed-tree contract, like a lane stamp: a run over
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
find the stamp. A landing never writes a coverage stamp: its suite lane is
`make coverage-bed` ([gating a branch](#gating-a-branch-before-it-lands)), and
`make coverage` is the release's certificate (and the by-hand working check
before `make release`). So a release tip usually has no coverage stamp: run
`make coverage` on it before `make release` when you want the suite's twenty
minutes to be yours and not the release's; either way the floor is enforced
on the released tree.

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
once its CI is green:

```bash
gh pr create --draft --base main --title "feat: add SSH retry logic"
```

The PR follows the steps in [Gating a branch before it
lands](#gating-a-branch-before-it-lands) and lands by the maintainer's
**Rebase and merge** once CI and the `local-gate` status are green. It then
closes as merged; delete the branch and pull `main`:

```bash
git checkout main
git pull --rebase
git branch -d <your-branch>
```

### PR checklist

- [ ] Commits follow the conventional commit format
- [ ] `make gate-tip` has stamped the PR's head, and its CI is green on that sha
- [ ] `make gate-status` posted the `local-gate` status for this head
- [ ] Branch is rebased on the latest `main`
- [ ] Related issue linked (`Closes #N`)
- [ ] Commit subject reads as the changelog entry it will become (see below)
- [ ] No manual edits to `CHANGELOG.md` or the version string

### Public API review checklist

A change to a public name (one a declared namespace's `__all__` holds,
{doc}`api/stability`) is reviewed against the calling conventions in
[Calling conventions](api/stability.md#calling-conventions):

- [ ] A new public callable is keyword-only, unless its `[symbols]` entry in
  `api/public.toml` lists positional parameters and says why.
- [ ] A new public record is `@dataclass(kw_only=True)`, and so is every
  dataclass base in its chain: `kw_only` is not inherited.
- [ ] A class otto constructs for the user is declared `construct = false`
  in its `[symbols]` entry.
- [ ] An implementer-facing member (one you write when you subclass) never
  gains a parameter without a breaking mark.

`make check-api-keywords` holds the first item mechanically
([the quality-gates table](architecture/quality-gates.md#what-binds-each-gate));
review holds the rest.

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

Most make lanes stop each pytest leg after 40 minutes, so a hang fails the
lane instead of stalling it (`conformance-bed` and `kmodcov` have their own
caps, `CONFORMANCE_BED_TIMEOUT` and `KMODCOV_TIMEOUT`). A slower machine
raises it from the environment with `LANE_TIMEOUT` ({ref}`slower-machines`).
Set on the make command line instead, like any variable there, it marks that
`make coverage` run as overridden, so the run is not stamped, and
`make gate-tip` refuses it outright. The cap is separate from pytest-timeout's
per-test limit (`timeout` in `pyproject.toml`).

(slower-machines)=

### On a slower machine

The suite's bounds fit the project's own dev VM. On a machine that runs otto
and the lab more slowly (a lab nested inside another VM, a busy host), raise
them from the environment, once, in a shell profile. The tests run inside the
`dev` VM, so three of them go in its `~/.bashrc` (an export on the machine you
`vagrant ssh` from does not follow you in):

```bash
# in the dev VM's ~/.bashrc
export OTTO_INIT_TIMEOUT=30          # each shell's readiness deadline (default 10, embedded 15)
export PYTEST_TIMEOUT=540            # pytest-timeout's per-test limit (default 180)
export LANE_TIMEOUT=10800s           # each make lane's per-leg cap (default 2400s)
```

The fourth goes where you run `vagrant`, on the machine hosting the lab:

```bash
export OTTO_LAB_BOOT_TIMEOUT=1800    # vagrant up's wait per lab VM (default 900)
```

Scale them by how much slower the machine is; three times is a reasonable
start. Neither the Makefile nor the noxfile names them: every lane and nox
session inherits your environment, the suite's `OTTO_*` strip lets
`OTTO_INIT_TIMEOUT` through on purpose, the lab and e2e tests hand it to the
otto they run, and a stamped lane
(`make coverage`, `make gate-tip`) still stamps. `OTTO_INIT_TIMEOUT` is otto's
own setting, described under
[Shell readiness timeout](configuration/host-options.md#shell-readiness-timeout).
`PYTEST_TIMEOUT` is pytest-timeout's, and does not override a test's own
`@pytest.mark.timeout`: a test that trips one of those on your machine is worth
an issue. The unit tests of the readiness default ignore `OTTO_INIT_TIMEOUT`,
so they still check the 10 s otto ships with.

(bed-lease)=

### The bed lease

The lab bed is single-tenant, and the bed lease is what enforces it. Every
`make` target that touches the bed (a suite that needs the lab, the console and
guest probes, and `quick` when its `PATHS` name a tree outside `tests/unit`)
takes the cell's lease as its first step and releases it as its last; the
targets that run bed lanes with `$(MAKE)` (`release`, `stability`, `all` and
the release matrices) hold it across every lane they run. `gate-tip` holds it
only while its bed lanes run: it takes the lease immediately before the first
bed lane that will actually run and releases it after the last, so a landing's
bed lanes are one hold and its static lanes need none. The census in `tests/unit/test_lane_invariants.py`
classifies every Makefile target as leased, holding, or hostless, and refuses
one it does not know. A second run on the same cell fails fast:

```text
bed-lease: tech1 is held by vagrant@otto ('gate-tip', pid 4242, since 2026-11-01T12:00:00+00:00, expires 2026-11-01T16:00:00+00:00): holder running
```

Set `BED_LEASE_WAIT=<seconds>` to queue behind it instead (the lane polls every
5 s and starts the moment the bed frees). `make bed-status` shows the same
record, `free` when nobody holds it.

The lease budgets belong to `make gate-tip`, not its lanes: it spends
`BED_LEASE_WAIT` (above) once when it takes the lease, its `BED_LEASE_TTL`
covers its whole hold, and `BED_LEASE_DRAIN` is spent once when it releases.
It passes none of the three to the lanes it runs, which re-enter the gate's
hold.

The lease belongs to the **run**: the `make` running the lane and every
process group it starts. A lane that dies mid-run (SIGKILL, a lost VM, a
closed terminal) keeps the bed only while something of it still runs; once
its workers are gone the next acquirer takes the lease over, saying whose it
was and that it was dead. A red lane ends the same way: `make` stops at the
failing step, so the release never runs and the next lane on the cell prints
`taken over from ... dead`, which is that red lane's lease being collected,
not a second failure. A green lane's release first waits (`BED_LEASE_DRAIN`,
default 30 s) for the run's own processes to exit, and keeps the lease if any
remain, naming them: a lane cannot free a bed its own workers are still on.
A lane that merely overran its budget
(`BED_LEASE_TTL`, default four hours, in seconds; raise it in the environment
of a longer soak) is reported as overdue but is never taken from under a live
run: the eviction of a run you *know* is dead is `make bed-release`, which
names what it evicted. Launch an unattended lane under `setsid` (for
example `setsid make coverage-unix > lane.log 2>&1 &`) so its process group is
the run's own rather than the launcher's; a lease whose group is a long-lived
launcher's shell is held until that shell exits or you release it. A
`make lane 2>&1 | tee lane.log` is fine: `tee` shares make's group but is not
the run's, and the lease knows a pipeline sibling from a worker a lane left
behind (one started beside make, within two seconds of it, by make's own
parent).

`make bed-lease PURPOSE="..."` holds the bed from an interactive shell for as
long as that shell lives (a hold to keep peers off the bed while you work by
hand); the lanes you then run from it re-enter the hold. `make bed-release`
frees it. The holder is the shell that ran `make`, so from a launcher that
exits at once (an agent's command tool, a one-shot `sh -c`) the hold is dead
the moment it is taken: hold the bed from a shell that stays open.

The lease lives under `/tmp/otto-bed-lease/` on the machine the lanes run
from (`BED_LEASE_ROOT` moves it), as `<cell>.json` and, beside it,
`<cell>.reservations.json` in otto's own [JSON reservation
format](cli/reservation/json-backend.md): the holder is the owner of every
resource the cell's labs declare, so otto can read the lease without knowing
the script. From a project whose settings name the cell's lab directory and
that file (the paths below are the dev VM's; use your checkout's),

```toml
libs = ["/home/vagrant/otto-sh/tests/custom_hosts"]
init = ["custom_hosts"]

[[lab.sources]]
backend = "json"
paths = ["/home/vagrant/otto-sh/tests/_fixtures/lab_data/tech1"]

[reservations]
backend = "json"

[reservations.json]
path = "/tmp/otto-bed-lease/tech1.reservations.json"
```

(`libs` and `init` load the test suite's `custom_hosts` package, which
registers the frame the cell's embedded hosts name; without it the embedded
lab does not load.) `otto --lab unix+busybox+embedded reservation check` reports every bed
resource `held: yes` to the holder and refuses anyone else, naming the holder.
otto only reads the file; the lease script (`scripts/bed_lease.py`) is the
scheduler.

(bed-reap)=

### The bed reap

A run that stops early leaves its state on the bed: the otto tunnel daemons,
impair timers and `tc` qdiscs a test placed, the `nc` listeners and
`/tmp/otto*` staging its hosts carry, orphan compose stacks and e2e images on
the docker parents, a half-built coverage product on this machine, and a
console a test left logged in. The bed reap (`scripts/bed_reap.py`) removes
all of it through otto's own removers, judged by the hygiene oracle's probes
(`tests/_fixtures/bed_hygiene.py`), under the lease. It never names a host:
the cell's lab file says what the bed is. It reaches the chaos guest through
its hop and waits for the guest to boot first
({doc}`architecture/subsystems/chaos-guest`).

Every lane that runs a suite on the bed runs the quick reap right after it
takes the lease and again before it releases it: whatever a predecessor left
is gone before the suite starts, and a green lane leaves none of its own. A
red lane stops at its failing step, and the next lane's first reap collects
what it left.

So you may stop a gate whenever its result no longer matters: a tree that a
rebase has replaced, a lane already known to be red. Interrupt it, or kill it
outright. The gates exist to defend against dirty lab state, not to be
protected from it. A stopped run that cannot release its own lease leaves it
for the next lane, which takes it over once nothing of that run still runs
([the bed lease](#bed-lease)) and reaps before its suite starts. A kill
that leaves workers running keeps the bed until you kill them too, and a
console the stopped run left logged in waits for the hand run, which alone has
the console step.

The hand run, for when you want the bed clean now rather than at the next
lane's start:

```text
make bed-reap
```

It adds the console step (`make console-logout`'s; `RESTART_QEMU=1` restarts
the guests first, as `make qemu-restart` does), prints one `bed-reap: removed
...` line per thing removed, `bed-reap: clean` when the bed is, and exits 1
with a per-host `still carries` report when something stayed: a host that did
not answer, a removal that did not take, or a qdisc tree otto did not place
(reported as foreign and left alone: clear it by hand on that host with
`sudo tc qdisc del dev <netdev> root`, then run the reap again). It refuses
a bed another live run holds, and a run of your own whose processes are still
running: wait for them, or kill them, then run it again. `make gate-tip` runs
the same reap by itself when you interrupt it (above).

The reap is quick when the bed is clean (a probe per host). One step is not:
the embedded coverage product (`tests/project3/product`) is rebuilt when a
declared build's artifact is missing, which a `west build` cut mid-way
leaves behind, and that build takes minutes. It runs at every lane's start,
whether or not that lane loads the product, so a missing artifact costs the
next lane the rebuild once.

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
| No-testbed CI gate (tests/unit + no-VM e2e) | `make coverage-hostless` (pinned; the floor's by-hand certificate) / `make nox-hostless` (the three hostless sessions across their Pythons; no floor — see [the cross-Python matrix](architecture/testing.md#other-axes-typescript-doctests-cross-python)) | none |
| Full coverage gate (all tiers, excludes `stability`) | `make coverage` | lab VMs |
| The bed half of that gate (tests/integration plus the e2e tests marked `integration` or `embedded`), uninstrumented, pass/fail: every `BED_REACH` tip's lane, never a floor | `make coverage-bed` | lab VMs |
| Unix VMs, incl. multi-hop (resource) | `make coverage-unix` / `make nox-unix` | test1/test2/test3 |
| Embedded / Zephyr (resource) | `make coverage-embedded` / `make nox-embedded` | zephyr VM |
| Multi-hop only | `uv run pytest -m "hops and not stability"` | three VMs |
| Stability / soak — every tier runs and the target fails at the end if any did (so `make release-campaign` can rely on it) | `make stability` (or `stability-unit` / `stability-unix` / `stability-tunnel` / `stability-embedded`) | lab VMs (`-unit` needs none) |
| Probe-line cut sweep (#853): every tail of the readiness handshake, the resync probe and the recovery probe, typed into each installed interactive shell on a pty, then the line whole (`stability`-marked; nightly's `probe-cut-sweep` job; never a release stage) | `make stability-cut-sweep` (`CUT_SWEEP_WORKERS=N`, default 4) | none |
| The #386 console-release bed proof (opt-in, dev VM only; `make release-campaign` runs it; what it runs: `tests/integration/host/test_console_release_bed.py`) | `make console-release-bed` | the telnet Zephyr guests and their hop |
| Chaos lane (tier 3, opt-in — interrupt/SIGKILL/reboot scenarios + BedHygiene, incl. docker kill/pause/restart/daemon-restart and privilege `as_user` interrupt) | `make chaos` / `make chaos-embedded`. On demand only — never part of `make release` (owner decision, 2026-10-15; see [quality gates](architecture/quality-gates.md#the-last-two-rows-are-the-point)); its reboot scenarios reboot only [the chaos reboot guest](architecture/subsystems/chaos-guest.md) behind test4, never a lab VM ([#985](https://github.com/ludachrish3/otto-sh/issues/985)). | leased unix host, incl. test3 for docker and the chaos reboot guest on test4 (+ zephyr board for the embedded leg) |
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

`uv run pytest -k <kw>` filters any run by keyword. Clean a bed a killed or
interrupted run left dirty with `make bed-reap` ([the bed reap](#bed-reap)).
Recover a wedged embedded bed
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
One product variable is declared there too, `OTTO_INIT_TIMEOUT`, so a slower
machine's readiness deadline reaches the lab tests ({ref}`slower-machines`).

Declare a new knob there and read it with `ambient("OTTO_...")`. Reading one
straight from `os.environ` without declaring it does not raise; the variable is
simply gone by the time the reader runs, so the knob silently does nothing and
the run stays green while doing the wrong thing. That is issue #192: nightly's
`OTTO_CHAOS_DOCKER=loopback` job spent months of runtime targeting the bed host
instead, and the same bug had quietly disabled the chaos seed's reproduce path
and `make stability-tunnel CYCLES=N`.

#### Starting a pytest session from a test

A pytest session a test starts must not take its temp files from the shared
`/tmp/pytest-of-<user>`: at session finish it can delete the temp tree of a
whole pass that just finished, inside the test's budget. The nightly 3.10 leg
timed out three nights running on that (#814); the root conftest's
`_hermetic_child_temproot` fixture explains the mechanism. That fixture points
`PYTEST_DEBUG_TEMPROOT` at a directory of the test process's own, so start the
child with an env built from `os.environ`, or with `otto_subprocess_env` or
`coverage_subprocess_env` from `tests/e2e/_otto_subprocess.py`, which pass the
variable through. A child given `--basetemp`, as every `pytester` session is,
needs neither.

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

(embedded-coverage-bed)=
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
uv run nox -s tests_hostless_unit-3.10  # one tier of the no-testbed CI gate under one Python (no floor)
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

### Images ship with the docs

Every image a page shows is a file in the tree, never an http(s) URL: the
release's offline docs tarball (see
[Offline documentation](installation.md#offline-documentation)) is read
inside an air gap, where a hotlinked image is a broken one.
`tests/unit/docs/test_docs_images_are_local.py` fails on a remote image in any
docs source. Committed artwork (the index page's hero) lives in
`docs/_static/`; anything derived from the tree is rendered at build time
into `docs/_static/generated/` instead. The overview's supported-Python badge is
one: `scripts/render_python_badge.py` draws it from `pyproject.toml`'s
`Programming Language :: Python :: 3.X` classifiers, so adding or dropping a
Python changes the badge with no docs edit.

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

### User pages cite only what a reader has

A page outside Developing otto must not require otto's maintainer checkout or
infrastructure: its reader has the published site, otto's shipped sources and
the example projects, nothing more. `make docs-lint` (and `nox -s docs`)
enforces it with `scripts/lint_docs_citations.py`, which skips the Developing
otto pages (`developing/`, `architecture/`, this page and
{doc}`release_process`) and generated pages. Its six rules:

- **include:** a `literalinclude` or `include` target lands in `docs/examples/` or `src/otto/`.
- **download:** a `download` target lands in `docs/examples/` or the built example archives.
- **link:** a relative link that resolves inside the checkout stays inside `docs/` or `src/otto/`.
- **path:** a path in code never names a tracked `scripts/` file and never starts in one of otto's other tracked two-level directories (such as `tests/unit/`; `docs/examples/` and `src/otto/` are the reader's), unless an example project has the same path.
- **make:** a `make` command in code names no maintainer target, such as `make kmodcov`.
- **issue:** no `(#NNN` or `issue #NNN` reference, in prose or code.

HTML comments in Markdown and comments in RST are invisible to it, so a
maintainer note can sit in one; Markdown's 4-space indented code blocks are read
as prose, a known gap, so put code in a fence. A `make -C dir` or `make -f file`
that names some other Makefile is not otto's and is never checked; `make -C .`
and `make -f Makefile` still are. A page that a pending docs track will rewrite
carries an exact ceiling of findings in `EXEMPT`: a fix lowers the ceiling, a
new finding fails, and the list only shrinks until each track lands.

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

### Downloadable example projects

A track's example project lives in `docs/examples/quickstart-<track>/`, and is
a complete project a reader can build: sources, `.otto/` settings, tests.
`make docs` (and `nox -s docs`) checks each one without a lab with
`scripts/check_docs_examples.py`: `otto --list-products`, `otto --lab <lab> -n
run install`, and, when it has a `CMakeLists.txt`, a cmake build and `ctest`.
Then, for each `[[docker.use_cases]]` name it declares, it previews `otto --lab
<lab> -n docker compose build <use-case>` and `... up <use-case>`. These come
after the build because an image's context may be a build directory. The lab is
the project's single literal `[project] lab_patterns` entry, or the only lab it
has. At build time `scripts/build_example_archives.py` packs the project's
tracked files into `docs/_static/generated/examples/<track>.tar.gz`, and the
track's page links that archive:

```text
{download}`quickstart-shell.tar.gz </_static/generated/examples/quickstart-shell.tar.gz>`
```

Only tracked files ship, so commit a file before its page relies on it being
in the download.

### Setup and bed-case-study captures

The `docs/setup/` and `docs/developing/bed-case-study/` pages `{literalinclude}` what they show, never
paste it: config and code fragments from the checked-in project
`docs/examples/getting-started/`, and real command output from committed text
files under `docs/examples/captures/`, produced by
`scripts/refresh_docs_captures.py` from the manifest
`docs/examples/captures.toml`. One manifest and one artifact directory serve
every example project. Labless captures
(`labless = true`) are diffed in the docs gate (`nox -s docs`,
`make docs-lint`); bed captures are
diffed by `make docs-captures-check` and refreshed by `make docs-captures`,
both of which need the lab VMs. To refresh one: `uv run --no-sync python
scripts/refresh_docs_captures.py --only <id>`; a capture in an ordered set
(the manifest's `[ordered]` table) refreshes only with its whole set, so
name every id in it. Never edit an artifact by hand
— if a capture drifts, the page's claim drifted with it, and the fix is in
otto, the manifest's redaction rules, or the prose. A rule may change a
line's width: once every rule has run, each log line's `file.py:<n>` column is
re-padded to end at the capture's display width, where Rich put it, so a
replaced value of any width leaves that line the same. A log record that Rich
wraps onto a second row can still drift: the wrap point moves with the width
of the source column and with the raw width of any value a rule replaces in
the message. A failed refresh leaves
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
- **The lab reference is an index plus one page per area.**
  `docs/configuration/lab-config.md` holds a minimal lab, the list of area
  pages under `docs/configuration/lab/`, and the whole-lab example. A new
  lab-data area gets its own page there, listed in the index's toctree and
  its one-line list; `tests/unit/docs/test_lab_config_split.py` checks both.
- **Every lab-data field needs a row.**
  `tests/unit/docs/test_lab_config_field_coverage.py` fails when a field of a
  lab-data spec model has no row on any page under
  `docs/configuration/lab/`, or an inventory record
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
  every code block and every inline code span under `docs/` (except
  `docs/superpowers/`) and in the top-level `README.md`, and the whole of
  `docs/examples/` and `src/otto/examples/`. Among other things, it refuses:
  - an import, a dotted access or a `"module:Name"` string that names an
    undeclared path;
  - a star import;
  - a taught use of an otto underscore member;
  - a call that passes a public callable more positional arguments than
    its signature takes (the `positional-call` finding: a `Result` built with
    its status by position, once `status` is keyword-only), in a code block or
    in an inline code span
    of the prose: a sentence's `` `f(a, b)` `` teaches the shape as much as
    a fence does. Write the call as a reader must write it, or describe the
    old shape in words.

  The full list of finding kinds is in that script's module docstring.

  The docs declare nothing. To teach a new name, add it to its namespace's
  `__all__` first ({doc}`api/stability`).

## Printing to the console

### Literal text

A path, a host id, a link or tunnel id, an argv or a URL is text the reader
may copy, and Rich would change it three ways on its way to the screen: it
reads `[red]` as a style tag and deletes it, turns `:smile:` into an emoji,
and wraps a long line mid-token at the console width. Pick the helper by
what the text is:

- **`otto.console.print_literal(text, *, console=None)`** prints a line
  exactly as written, on one line, whatever the width. Use it for any line
  that carries a user's or a host's text. It prints through Rich's global
  console unless you pass `console=` (the output-directory and monitor URL
  lines pass `CONSOLE`).
- **`otto.cli.invoke.print_error`** prints the message as a `Text` in red, so
  user text in it is literal and a caller styles a part with `Text.assemble`.
  A prose line that interpolates user text is
  `rprint(Text.assemble((own, style), user))`, never `escape()` inside a
  markup string.
- **`otto.logger.relay.escape_relayed`** is for foreign text that must stay a
  markup string, because it travels through a log record or a progress
  description.

To style a prefix, build a `rich.text.Text` with `Text.assemble` and
pass that: its styles survive, and the user's text stays a plain span
(`otto.console` is internal, so the docs never teach its import line;
`check-api-teaching` holds them to `api/public.toml`):

```python
from rich.text import Text

print_literal(Text.assemble(("Wrote schemas to", "green"), " ", str(path)))
```

The four flags that make a print literal (`markup=False, emoji=False,
highlight=False, soft_wrap=True`) live only in `print_literal`. Don't spell
them at a call site: a copy that drops one (`emoji=False` was the one that
went missing) is the bug this helper exists to prevent. A CLI module imports
`otto.console` inside the function that prints, beside its other Rich
imports, so no verb's `--help` loads `rich.console`.

### Identifier columns

Every table column whose cells hold an identifier, a path, an address or a
value echoed from config is added with `otto.console.id_column(table,
header, **kwargs)`, and every cell in it goes in through
`otto.console.literal_cell(value)`. The promise is narrow: the cell is
literal (no markup, no `:emoji:` codes), and a cell too wide for its column
folds onto more lines instead of ending in "…". It does not stay on one
line; for a token that must, print it on its own line with `print_literal`.
Columns of otto's own prose keep `add_column` and their usual cells.

Rich's wrapping has two limits that apply at any width, so a reader can
still lose a character:

- a space that lands exactly on a fold is dropped (`"ab cd"` in a 2-wide
  column renders `ab` / `cd`);
- a double-width character cannot render in a column one cell wide, and is
  dropped.

`tests/unit/test_console_literal.py::test_rich_fold_limits_are_pinned` pins
both, so a Rich upgrade that changes them is noticed.

### The width default

The `otto` command's shim (`otto._shim._apply_columns_default`) sets
`COLUMNS` before the CLI imports anything that builds a Rich console: Rich
reads `COLUMNS` once, in `Console.__init__`. The rule the user sees is the
`COLUMNS` row in [Environment variables](cli/index.md#environment-variables);
the "is this a usable terminal" decision behind it is `otto._terminal`'s, shared
with the `CONSOLE` size pin in `otto.console` and the login's remote terminal
size. Keep both of those in that one module.

- **In-process tests never pass through the shim.** A test that calls the
  Typer app (a `CliRunner`, an in-process `pytest.main`) gets whatever
  `COLUMNS` the worker has, not the shim's default. A test about the default
  width runs otto as a subprocess.
- **The root guard.** A test that does call `_shim.main()` in-process would
  leave `COLUMNS` set in the worker for every later test, so the root
  conftest's autouse `_restore_columns` puts it back to exactly what it was,
  absence included. Restore environment variables through
  `tests/_fixtures/env_guard.py` (`preserved_env_var`, `preserved_timezone`)
  rather than a hand-rolled save-and-restore.
- **Width tests run otto through `run_otto`.** It builds the child's
  environment from scratch, so `TERM` is absent unless a test passes one
  (CI sets `TERM=dumb`, which pins Rich to 80 columns under a forced
  terminal), and it attaches stdin to `/dev/null`, so the child sees no
  terminal on any descriptor. A test that needs the old narrow width passes
  `extra_env={"COLUMNS": "80"}`.

## Coverage reports

### From pytest

```bash
make coverage
```

Any `make coverage-*` target writes the HTML report to `reports/coverage/html`
on its final pytest leg. A bare `pytest` run prints the `term-missing`
console report only. Nox sessions run with coverage off and print none
(the instrumented hostless sessions on the primary Python write a partial
report and enforce no floor; the CI floor is judged on their fold, see the
coverage-floors note at the top of `noxfile.py`). The
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
  one entry per surface and minor). A surface named without buckets, an
  entry whose total differs from the growth measured or whose buckets
  differ by more than two operations, or an entry nothing grew for over
  the whole run each stop again. It is a command-line variable (an
  environment value approves nothing) and the release's own knob, like
  `BUMP=`: the coverage stamp accepts it. Growth or a bucket shuffle is
  measured a second time before it counts, a third when the second
  disagrees, and a minor that never reproduces itself stops as
  inconclusive, naming the surfaces; the tables shown are the deciding
  pair's, or every pair's, labelled, for a minor that stayed
  inconclusive. One operation can land in a different bucket from run to
  run, so two measurements agree when their totals match and no bucket
  moved by more than two, and a move that small is no shuffle; growth
  itself is exact. The base's records are cached under
  `reports/budget/<tag>/` of the checkout that measured them (per
  checkout, like every `reports/`) and reused while their sha, tree,
  interpreter version and instrument (the harness and `tests/_fixtures/`
  at the tip) still match; the tip is measured every
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
  interpreter: `uv run nox -s tests_hostless_unit-3.14 --install-only`, then
  `.nox/tests_hostless_unit-3-14/bin/python scripts/import_budget.py --update`.
  A missing-baseline failure prints the same commands for the interpreter
  it ran on. The next Python has a ceilings file too, read by nightly's
  `python-canary`. It is not in `noxfile.py`'s `PYTHON_VERSIONS`, so its install
  step needs the flag: `uv run nox -s tests_hostless_unit-3.15 --extra-python
  3.15 --install-only`. `--update` moves ceilings only: a `target_ratio` breach
  stays red.
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

The suite's time splits by venue, and the split is what [the landing's
tip](#gating-a-branch-before-it-lands) is built on: the pull request's CI runs
the hostless half, and the tip runs the bed half (`make coverage-bed`). Of
`make coverage`'s 5,721 test-seconds on
2026-11-01 (22,887 tests), `tests/unit` held 3,542 and everything outside it
about 2,180, nearly all of it the bed half (the `tests/integration` tree and
the e2e tests marked `integration` or `embedded`; the hostless e2e items are
the small remainder): docker e2e 381, console e2e 344, integration/host 292,
cov e2e 251, cli e2e 215, tunnel e2e 125, busybox_bed 101, link impair 97,
host e2e 92, the rest under 70 each. `make coverage-bed`'s reports
(`reports/junit/coverage-bed/` and `coverage-bed-serial/`) measure the bed
half alone. An escalating commit therefore runs no hostless half locally either:
its full gate ends with `make coverage-bed`; `make coverage-hostless` is the
by-hand certificate when you want one, and CI owns the hostless sessions and
the fold.

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

The fourth view is the lane as a whole against its tests:

```bash
uv run python scripts/junit_durations.py --lane-wall reports/junit/coverage-python/*.xml
```

prints, per report, the suite's wall (pytest's whole-session clock, the
`testsuite` element's `time`), its summed test-seconds, their ratio, and when
the suite started. The ratio is the average number of tests in flight over
the wall: how much overlap the lane got, not how busy the CPUs were (a test's
time includes its waits, so sleeps and network calls overlap too). A ratio
near the worker count says the workers stayed busy; one at or below 1 says
the lane ran one test at a time, on `xdist_group`s or on `-n0`. How far
below 1 is the prologue, collection and coverage finalisation, which are in
the wall and in no test's time: the serial `-n0` leg of `make coverage`
reads well below 1 for that reason (about 0.3x on the 4-vCPU dev VM, where
the 2026-10-31 run spent 127 s of wall on 40 s of tests). The wall is the
number to put against a stamp's `elapsed_s` below.

### What a gate costs

Every stamp records what its lane cost, so a proposed saving is argued from
numbers and not from memory. A
[lane stamp](#the-landings-gate-is-stamped) carries a `timing` object:
`started` and `finished` (UTC), `elapsed_s` (whole seconds of elapsed time,
monotonic clock), `interpreter` (the Python `gate_tip.py` itself ran under, the project venv;
a lane's own interpreters are the lane's business), `nproc` (the CPUs the
lane may run on, what `nproc(1)` prints and what `COVERAGE_WORKERS` reads)
and `junit` (the reports under `reports/junit/` the lane wrote: those new or
rewritten between the lane's start and its end). A red marker carries the
same object. A promoted stamp carries none: the lane did not run on that
tree. Timing is a report and
never evidence: the pre-push check does not read it, a stamp without it (or
with a malformed one) is evidence all the same, and a failure while
measuring leaves the lane's verdict and stamp untouched.

The [coverage stamp](#the-coverage-stamp) itself records the run per leg:
its `timing` has `started`, `finished`, `elapsed_s`, `nproc`, `workers` (the
`-n` count the Makefile passed, `COVERAGE_WORKERS`), `legs` (a list, in run
order: `dashboard`, `coverage-python`, `coverage-python-serial` — which
includes the unit-outliers report printed after it — and `coverage-ts`, each
with its `seconds`) and `junit`. No verb prints it: read the stamp file,
`<git common dir>/otto-coverage-stamps/<tree>.json`. The legs are marked by
`coverage_stamp.py mark <leg>` lines after the Makefile's legs. A mark
records only onto the record a stamped run's `start` wrote; with none (`make
coverage-python` alone, `make all`) it is a silent no-op, a failed run's
leftover record takes marks until the next `start` rewrites it, and a mark
is never a leg's verdict.

`make gate-tip` ends with the table for the run — elapsed, outcome
(`stamped`, `already stamped`, `red`, `not stamped`, `live at push`, or
`missing` in a dry run) and lane — then says what publishing did: `posted
local-gate on <sha>`, `local-gate not posted: <why>`, or, when a red run
flipped a success `HEAD` carried, `posted local-gate failure on <sha>`. On
the dev VM a tip that reaches no bed lane costs about 50 s, one that
reaches the bed half about 8 minutes (`make coverage-bed` is 405 s of it),
and the pre-push hook about 20 s on a stamped tree. A rebase onto a moved
`main` costs the static lanes again (about 50 s) plus, for a tip with a bed
lane, about 8 minutes, unless
[the carry rule](#the-landings-gate-is-stamped) holds and the bed lane is
carried: seconds.

A batch ([Landing a batch](#landing-a-batch)) is priced by those numbers: a
batch of *n* fixes costs *n* scoped working checks plus one tip gate, where *n*
separate landings cost *n* tip gates. When the batch's tip plan has a bed lane
that is one bed run (about 8 minutes) and one CI run instead of *n* of each.

The stored table for any tree is one command away:

```bash
uv run python scripts/gate_tip.py --timings          # HEAD's tree
uv run python scripts/gate_tip.py --timings <rev>    # a rev, a commit id or a tree id
```

It lists every stamp and red marker the store holds for the tree, oldest
first: elapsed, outcome (`stamped`, `promoted` — the stamp came from another
tree, so no elapsed — or `red`), when it was written, and the lane.

### Register built-ins by reference

A registry's built-in entries are registered in the module that defines the
registry, as a {class}`~otto.registry.Ref` naming the implementation
(`Ref(target="otto.host.transfer.nc:NcFileTransfer")`), never by the implementation
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

### Duplicated code

Two kinds, caught two ways.

A block copied between modules is the textual kind, and
`scripts/check_duplicate_code.py` (`make lint-arch`, `nox -s lint`) catches
it: pylint's similarities checker reports every pair of `src/otto` modules
sharing six or more consecutive lines, comments, docstrings and imports left
out, and the gate compares the pairs against
`scripts/duplicate_code_baseline.txt`. The contract is `tach.toml`'s ratchet:
a pair the baseline does not list fails, and so does a listed pair that no
longer reports — remove its line in the same change, so the file only
shrinks. Pairs are keyed on module names, not line numbers, so an edit above
a clone does not move it out of the baseline. The remedy for a new pair is
one function the copies call; `--write` regenerates the file, and an added
line in its diff is a new clone to fold, not a baseline to accept.

The same decision computed at two entry points in different words — a
constant restated where another site calls a resolver, an open sequence
re-spelled with a subset of its steps, a command string rebuilt by hand, a
retry loop beside `otto.utils.wait_for` — is the semantic kind, and no
detector sees it. AGENTS.md's "one decision, one place" rule is the gate:
find the site that already decides it before writing, and a review names
every restatement with the existing site's path. The deadline-poll rule
(`.ast-grep/rules/no-handrolled-deadline-poll.yml`) covers the one shape of
it that an AST pattern can express: a poll whose deadline is spelled as a
`range()` count times a sleep is the same poll as a clock-spelled one, and
goes through `wait_for` / `wait_for_async` like the rest.

## AI-Assisted Contributions

AI coding tools (e.g., GitHub Copilot, Claude, Cursor) are permitted for
contributions to otto. If your PR contains AI-assisted code, please note it
in the PR description. Regardless of how code was generated, contributors
are responsible for understanding, testing, and owning what they submit.

AI-assisted commits carry an `Assisted-by:` trailer naming the model that
actually produced the work — the trailer is a per-commit provenance claim, so a
stale or approximate name defeats its purpose. An AI agent writes the trailer
into the commit message itself; nothing adds it for you.

### Review tiers

AI-assisted work is reviewed in tiers, and the tier is a property of the whole
landing:

- **Full tier.** Every landing gets a design review by two independent models,
  a plan check, an implementation review per task and a final branch review by
  two models.
- **Batch tier.** A batch of XS/S changes gets one design check per batch
  plan, the implementation review per fix and one final branch review of the
  batch. A change is XS/S when it is under about two hundred changed lines in
  one package, has no public-API or breaking change, and has no bed lane in
  its own plan.

The tier is decided from the whole landing, not per fix, and is independent of
the gate class: a landing's gate says what must run, its tier says how it is
reviewed.
