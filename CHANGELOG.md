# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-08-31

### Added

- **BREAKING** **logging**: root-logger capture — zero-registration funnel, [logging.levels] floor, one embedder pair
- **reservations**: reaching the runner never needs a slot
- **hosts**: hw_version and sw_version on every host family
- **reservations**: reservable resources at lab, element and host
- **inventory**: host facts from a tool-agnostic layer beneath lab.json v2
- **labs**: lab.json v2 -- a labs table, elements and metadata replace the hosts array


### Changed

- **BREAKING** **suite**: suites go pytest-native — fixtures for everything, an ensure marker, one loop per suite


### Dependencies

- **deps**: bump packaging from 25.0 to 26.3
- **deps**: bump pysnmp from 7.1.28 to 7.1.29
- **deps-dev**: bump @testing-library/user-event in /web
- **deps-dev**: bump @types/react-dom from 19.2.4 to 19.2.5 in /web
- **deps-dev**: bump @types/node from 26.2.0 to 26.3.0 in /web
- **deps**: bump actions/cache from 4 to 6
- **deps-dev**: bump ruff from 0.16.3 to 0.16.4
- **deps-dev**: bump ast-grep-cli from 0.45.1 to 0.45.2
- **deps-dev**: bump vite from 8.2.1 to 8.2.2 in /web
- **deps-dev**: bump @biomejs/biome from 2.5.9 to 2.5.10 in /web
- **deps-dev**: bump monocart-coverage-reports in /web
- **deps-dev**: bump @vitejs/plugin-react from 6.0.5 to 6.1.0 in /web
- **deps**: bump @xyflow/react from 12.11.3 to 12.11.5 in /web


### Documentation

- **readme**: oneshot() was renamed to exec() — update Hosts section
- **spec**: logging root capture — the standard funnel, a noise floor, one embedder call
- **spec**: suites go pytest-native — fixtures, an ensure marker, one loop per suite
- **getting-started**: a worked example built to survive interface change
- **spec**: getting started overhaul -- inventory and reservations pages, follow-ups, findability
- **spec**: getting started overhaul -- a worked example built to survive interface change
- **spec**: the built-in local host is never in play
- **spec**: hw_version and sw_version widen to the base HostSpec
- **spec**: three reservable levels -- lab, element and host
- **spec**: host inventory layer design
- **spec**: lab definition v2 design


### Fixed

- **suite**: class-scoped plugin fixtures are staticmethods — pytest 10 refuses the instance form
- **lab**: an element without an id renders as its name, not ('name', None)
- **init**: the printed next steps name the lab they need
- **suite**: count a test once -- setup and teardown passes are not "passed"
- **env**: parse the installed version before judging it
- **busybox**: the drift detector checks every pin, and says which failure it saw


### Maintenance

- **matrix**: re-measure the bed support matrix


## [0.8.8] - 2026-08-27

### Added

- **transfer**: every backend declares what it promises the progress bar
- **nc**: one universal netcat spelling, size-terminated GETs, nc_dash_n removed
- **release**: re-measure the support matrix during a release, refusing a downgrade


### Documentation

- **spec**: the transfer progress contract
- **todo**: record what the must-fix series closed in the 2026-08-25 review
- **spec**: nc universal spelling — one netcat spelling, size-terminated GETs
- **todo**: periodic review 2026-08-25 — public API, churn, flakiness
- **todo**: two release-time gaps — a stale matrix, and unexecuted air-gap steps


### Fixed

- **busybox**: fetch the artifacts from a release mirror first, busybox.net behind it
- **monitor**: an overflowing SSE subscriber lapses and is told to resync, instead of losing frames
- **busybox**: prove the artifact source at the fetch, not at collection
- **busybox**: prove the artifact source is reachable before a lane needs it


### Maintenance

- **conformance**: updated to reflect progress bar support


## [0.8.7] - 2026-08-25

### Added

- **matrix**: publish what otto can do where, and what proved it
- **env**: the bootstrap dependency preflight — refuse before the run, not during it
- **env**: the otto env command group — create, sync and show the orchestration venv
- **config**: ~/.otto, the workspace home, and the caches that move into it
- **BREAKING** **cov**: exclusion rules that remove lines and branches from the data
- **project**: per-invocation project activation — the labs decide, -I/-E override


### Changed

- **bed**: the labs are named for what they are, not for produce


### Documentation

- **install**: download wheels per interpreter, and gate the claim
- **env**: the dependency preflight, and what it does not cover
- **cli**: the otto env verb tree and the multi-project model
- **spec**: coverage exclusion rules that move the numbers
- **todo**: the integration conftest reaps docker for every test under it
- **spec**: the project switches say what they switch
- **spec**: the workspace home — ~/.otto keyed by workspace at the top
- **spec**: envs live under ~/.otto, otto's user-level home
- **spec**: multi-project activation and environments — two approved designs
- **spec**: the axis dataclass is HostAxes, derived from the built host
- **spec**: a support-matrix cell must name the observable it measured
- **zephyr**: the bed documentation describes the bed that exists


### Fixed

- **hooks**: non-interactive commits get no Assisted-by trailer
- **tests**: sample the history file only after its writer has exited


## [0.8.6] - 2026-08-22

### Added

- **bed**: the BusyBox guests answer on real NICs
- **BREAKING** **bed**: busybox guests become first-party; retire the contrived tier
- **bed**: five per-version busybox qemu guests on test1, behind carrot
- **BREAKING** **labs**: combine lab data from an ordered list of host sources


### Changed

- **cli**: order top-level help by the documented command order


### Dependencies

- **deps-dev**: bump ty from 0.0.72 to 0.0.73
- **deps-dev**: bump ruff from 0.16.2 to 0.16.3
- **deps-dev**: bump the vitest group in /web with 2 updates
- **deps-dev**: bump @biomejs/biome from 2.5.8 to 2.5.9 in /web
- **deps-dev**: bump hypothesis from 6.165.3 to 6.165.10
- **deps-dev**: bump @testing-library/user-event in /web
- **deps**: bump zustand from 5.0.14 to 5.0.15 in /web
- **deps**: bump telnetlib3 from 4.0.5 to 5.0.0
- **deps**: bump input-otp from 1.4.2 to 1.5.0 in /web
- **deps-dev**: bump nox from 2026.8.10 to 2026.8.17
- **deps**: bump uvicorn from 0.52.1 to 0.52.4


### Documentation

- **todo**: file the busybox and chaos-lane items the bed work left open
- **spec**: CI confirms the support matrix, not just the code
- **spec**: test-strategy upgrades and the unix-lab rename
- retarget two stale busybox doc paths left by the page move
- **guide**: the User Guide's CLI section mirrors otto's command tree
- **spec**: the User Guide's CLI section mirrors the command tree


### Fixed

- **tests**: pin the outage banner to the clock, not to navigation time
- **tests,docs**: name carrot's interface now that the bed multi-homed it
- **link**: otto clears everything otto can place
- **tests**: close the fd of a transport built after the reap's last scan
- **typing**: keep decorated verbs' signatures instead of erasing them to Any
- **transfer**: an interrupted transfer cleans up after itself


## [0.8.5] - 2026-08-20

### Added

- **BREAKING** **project**: per-project lab/host scoping — fleet-of-interest universes
- **project**: status --full shows the lab's other axis, and is_uninstalled says it plainly
- **project**: cleanup takes the lab's own leftovers off, and is_clean says so
- **project**: first-party project actions, default instructions, and ensure_* fixtures


### Dependencies

- **deps-dev**: bump @biomejs/biome from 2.5.7 to 2.5.8 in /web
- **deps-dev**: bump nox from 2026.7.11 to 2026.8.10
- **deps-dev**: demote unsound-return-statement, which this ty bump adds
- **deps-dev**: bump ty from 0.0.66 to 0.0.72
- **deps-dev**: bump vite from 8.2.0 to 8.2.1 in /web
- **deps-dev**: bump ast-grep-cli from 0.45.0 to 0.45.1
- **deps-dev**: bump @testing-library/user-event in /web
- **deps**: bump sse-starlette from 3.4.6 to 3.4.8
- **deps-dev**: bump bump-my-version from 1.5.0 to 1.5.1
- **deps**: bump @xyflow/react from 12.11.2 to 12.11.3 in /web
- **deps**: bump pydantic-settings from 2.14.2 to 2.15.0
- **deps**: bump shiki from 4.3.1 to 4.4.3 in /web
- **deps-dev**: bump knip from 6.31.0 to 6.32.2 in /web
- **deps-dev**: bump @types/node from 26.1.2 to 26.2.0 in /web
- **deps-dev**: bump hypothesis from 6.165.1 to 6.165.3
- **deps**: bump starlette from 1.4.0 to 1.6.0
- **deps-dev**: bump pytest-playwright from 0.8.0 to 0.9.0
- **deps-dev**: bump ruff from 0.16.1 to 0.16.2
- **deps**: take setup-uv 10.0.1, not 10.0.0 — the manifest retry is the point
- **deps**: bump astral-sh/setup-uv from 9.0.0 to 10.0.0


### Documentation

- **install**: a dedicated installation page, and the gates that keep it true
- **cov**: the store schema page is a snapshot, and it says v6
- **spec**: first-party project actions and default instructions


### Fixed

- close the four follow-ups the scoping review parked
- **project**: close the review debts from the first-party actions branch


## [0.8.4] - 2026-08-16

### Added

- **BREAKING** **dry-run**: a dry run contacts no device, and says what it would do
- **BREAKING** a dry run contacts no device, and says what it did not check
- **host**: sftp cannot be pre-checked, so its failure is named instead
- **BREAKING** **host**: the nc GET refuses a device whose own nc rejects `-N`
- **BREAKING** **host**: the scp backend refuses a device measured to have no scp
- **host**: shutdown picks the spelling the device has, and reports the answer
- **host**: the shell backend transfers over uu where the device has no base64
- **host**: recon once with `otto host <id> probe`, then pin and pay nothing
- **host**: Userland answers "has applet X", in one round trip for all of them
- **busybox**: Tier 3 over real ssh, and the userland gaps declared once
- **host**: a shell-only transfer backend, so BusyBox devices can move files
- **host**: BusyBox host support phase 3 — the ash frame and the busybox profile
- **host**: ask the device what its userland can do, instead of assuming
- **quality**: a gate now reproduces its own CI twin's environment, in a pristine worktree
- **quality**: library failures carry their domain, and unreachable is not failed
- **quality**: readiness is an event, and startup failure reaches the waiter
- **quality**: poll-until-deadline has one spelling, and expiry is never silent
- **quality**: the subprocess env dance and the path anchors each live once
- **quality**: a raises-check on ValidationError must name the field it means
- **quality**: a raises-check on typer.Exit must name the code it expects
- **quality**: the hermeticity strip has no back door — conftest env writes are gated at the import/runtime boundary
- **quality**: a dead probe now says so — the chaos oracles stop reading error text as a clean bed
- **quality**: retry means retried — one implementation, evidence, and a ban on lidding our own flakes
- **cli**: remote path tab completion for host get/put, reservation-gated
- **quality**: the gates can now see the tests, and the lanes stop lying
- **quality**: the two house rules that were only prose are now gates
- **link**: say why a link cannot be impaired, instead of a bare n/a


### Changed

- **host**: the shell backend's chunk loop is a codec, and base64 moves onto it unchanged
- **host**: the gap registry records PATHS, so a coverage hole cannot hide
- **BREAKING** **bootstrap**: discover() returns a DiscoveryResult, not a 3-tuple


### Dependencies

- **deps**: bump react-aria-components from 1.19.0 to 1.20.0 in /web
- **deps**: bump @internationalized/date from 3.12.2 to 3.12.3 in /web
- **deps-dev**: bump ty from 0.0.64 to 0.0.66
- **deps**: bump fastapi from 0.140.13 to 0.141.1
- **deps-dev**: bump pyinstrument from 5.1.2 to 5.1.3
- **deps-dev**: bump vite from 8.1.5 to 8.2.0 in /web
- **deps**: bump react-aria from 3.50.0 to 3.51.0 in /web
- **deps-dev**: bump hypothesis from 6.163.0 to 6.165.1
- **deps-dev**: bump @types/react-dom from 19.2.3 to 19.2.4 in /web
- **deps**: bump typer from 0.27.0 to 0.27.1
- **deps**: bump uvicorn from 0.52.0 to 0.52.1
- **deps-dev**: bump @testing-library/user-event in /web
- **deps**: bump pysnmp from 7.1.27 to 7.1.28
- **deps-dev**: bump knip from 6.29.0 to 6.31.0 in /web
- **deps**: bump starlette from 1.3.1 to 1.4.0
- **deps-dev**: bump @vitejs/plugin-react from 6.0.4 to 6.0.5 in /web
- **deps-dev**: bump @biomejs/biome from 2.5.6 to 2.5.7 in /web
- **deps-dev**: bump @types/react from 19.2.17 to 19.2.18 in /web
- **deps-dev**: bump ruff from 0.16.0 to 0.16.1
- **deps**: bump aioftp from 0.27.2 to 0.28.0


### Documentation

- **todo**: rescue the dry-run workstream's open items from scratch
- **todo**: add plans for default instructions and host method improvements
- **todo**: add plans for distributed lab host definitions
- **spec**: the dry-run contract -- validate-and-stop by default, previews by opt-in
- **todo**: reboot ignores dry run entirely, and the sweep repeats its caveats
- **todo**: dry-run discards the caller's log mode, so quiet payloads get echoed
- **host**: the two open base64 paths stay open, and now carry the measurement
- **todo**: queue the shell encoding restructure alongside the uu codec
- **todo**: uu is a container format, so it needs the opposite loop from base64
- **todo**: the empty-lane-leg gate is built, and why the obvious one would not have been
- **host**: install/stage was cleared by reasoning, so it is untested now
- **todo**: criterion 7 is closed -- CI proved the three preconditions
- **todo**: track what phase 5 left open, with the evidence for each
- **gates**: the xdist complement is not gated, and never was
- **todo**: correct the record on `no such table: context` + xdist INTERNALERROR
- **spec**: the testing-infra review, and the fix-with-gate plan that burns it down
- **architecture**: the pages describe the code, and the gates get a page
- **spec**: the docs-alignment design, and what the churn review still owes
- **suite**: suite registration reads the top level, and now says so
- **errors**: the OttoError convention says what it actually covers


### Fixed

- **test**: two workers fetching one BusyBox artifact stop sharing a temp file
- **host**: a dry run cannot ask, so it settles nothing and pins nothing
- **BREAKING** **host**: read_file/write_file refuse a device with no base64, instead of blaming the file
- **BREAKING** **link**: --expire on a bash-less host is refused, not reported as success
- **BREAKING** **host**: run() refuses the line ash would truncate, instead of being truncated
- **test**: own the applet symlink targets, and prove the root runs
- **test**: stop the rootfs harness needing an applet to find applets
- **host**: spend the remote sshd's channel budget, don't overrun it
- **host**: end a generation of hop resources, not the transport
- **host**: probe the `timeout` calling convention, not its name
- **host**: release the hop port forward with the port, not with the host
- **host**: reap the remote nc listener; `-w` never bounded it
- **test**: warm the forkserver too, not just the shared-memory arena
- **test**: re-seat the second subprocess capture, and pin the seam
- **host**: close the subprocess transport when a local exec times out
- **test**: read the fixture's commit time as epoch seconds, not strict ISO
- **lifecycle**: sync_phase forces on the second signal even when no handler runs
- **BREAKING** **host**: recovery-timeout rebinds go live, and wall-clock discriminators get a serial lane
- **test**: harness guards fail loud, restore snapshots, and bind what pytest drops
- **test**: the bed-certifying lanes fail loud or pass — never skip
- **test**: the marker rule fails its offender, and cleanup stops eating errors
- **test**: registry discovery is uncached — the count key was identity-blind
- **test**: the SIGWINCH e2e test covered nothing — the child had no ctty
- **test**: a --collect-only session must not trip the browser build gate
- **transfer**: the hop-nc hang dies bounded, and the retry ban is fully armed
- **tests**: the interact e2e must lease the host whose history it dirties
- **bootstrap**: contain a user module that declines to load
- **changelog**: git-cliff must not phone GitHub to render a changelog
- **changelog**: a breaking change is marked, and a scope says what broke
- **labs**: a host summary must agree with the host it summarizes, and cannot stall the shell
- **cli**: stop rich eating brackets out of user-facing errors
- **cli**: a lab-bound command must be async, at the decorator and at invocation
- **completion**: read pytest's python_files the way pytest reads it
- **docker**: stop absorbing exec failures into empty successes
- **docker**: a cached image whose :latest cannot be re-pointed is not a skip
- **completion**: scope link completion to the lab, through the loader's own rule
- **BREAKING** **cli**: an instruction must be async def, and now says so
- **completion**: hash every test source the --tests scan can read


## [0.8.3] - 2026-08-05

### Added

- **BREAKING** **cli**: one seam owns preamble, lifecycle, and rendering
- **cli**: lifecycle bridge at the leaf-invoke wrapper — registration is the opt-in (wave 2)
- **lifecycle**: sync_phase — two-stage interrupt policy for the pytest session (Tier 0.4)
- **quality**: tach + ast-grep architecture gates (lint-arch), bound on push via nox lint


### Changed

- **BREAKING** **host**: the product lifecycle returns Results, and the gate closes
- **BREAKING** **docker**: build results are CommandResults, not (Status, str) pairs
- **labs**: name hosts through the host source, not through lab.json
- one outcome convention — Result-family returns, gated
- **errors**: OttoError root — re-parent otto's public exceptions
- **host**: RemoteHost absorbs the duplicated session-delegation methods
- **BREAKING** **cli**: finish the lifecycle migration — strip and delete async_typer_command
- **core**: move the INSTRUCTIONS registry to otto.instructions (Tier 1.5)


### Documentation

- **spec**: lifecycle uniformity — codify the user contract (plain async def + registration = full policy)
- **spec**: command lifecycle uniformity — policy at the leaf-invoke wrapper, sync_phase primitive for otto test
- **BREAKING** purge SDD execution plans; keep specs (83-audit: 1 deletable)
- **todo**: top-2 TODO refactor feedback + repo-wide churn/design review


### Fixed

- **link**: resolve declared links against the id lab.hosts is keyed by
- **completion**: bound cache staleness for sources the fingerprint cannot see
- **web**: appease Biome's warning tier in the classicScript guard
- **monitor**: one owning seam for open-DB-before-spawn (Tier 0.7)
- **web**: classicScript fails the build when the bundle key vanishes (Tier 0.8)
- **test-harness**: stub tach.pytest_plugin for nested pytester sessions
- **ci**: resolve the three main-push failure classes from issue #193
- **bootstrap**: discovery errors ride the cached tuple; public invalidate() (Tier 0.6)
- **monitor**: first loop escape cancels sibling collection loops (Tier 0.3)
- **monitor**: bound the SSE subscriber queues and the in-memory series
- **host**: EmbeddedHost.close() closes transports even when a session refuses
- **quality**: burn down the architecture gates' initial findings
- **tests**: declare harness env opt-ins so the strip stops eating them


## [0.8.2] - 2026-08-03

### Added

- **vagrant**: provision docker on the dev VM for the loopback chaos venue
- **chaos**: docker + extended-surface chaos, closing the chaos workstream
- **chaos**: tier-3 chaos lane — BedHygiene oracle, seeded injection, live-bed scenarios
- **BREAKING** **settings**: remove the ${sut_dir} template variable
- **chaos**: tier-2 real-signal harness with lifecycle-owned monitor signals
- **host**: truthful two-phase reboot with liveness-gated recovery
- **lifecycle**: guarded teardown chains and shielded compensating actions
- **BREAKING** **cov**: manual-testing coverage overrides and ticket reattribution
- **lifecycle**: canonical run_command entry with two-stage SIGINT/SIGTERM teardown policy


### Changed

- **models**: lazy-export the settings specs; stop counting startup noise (#180)
- **cli**: defer the monitor runtime imports off the --help path (#180)


### Dependencies

- **deps-dev**: bump ty from 0.0.63 to 0.0.64
- **deps-dev**: bump bump-my-version from 1.4.1 to 1.5.0
- **deps-dev**: bump jsdom from 29.1.1 to 30.0.1 in /web
- **deps**: bump fastapi from 0.140.0 to 0.140.13
- **deps-dev**: bump @types/node from 26.1.1 to 26.1.2 in /web
- **deps-dev**: update uv-build requirement from <0.12.0 to <0.13.0
- **deps**: bump uvicorn from 0.51.0 to 0.52.0
- **deps**: bump shiki from 3.23.0 to 4.3.1 in /web
- **deps-dev**: bump @biomejs/biome from 2.5.5 to 2.5.6 in /web
- **deps-dev**: bump hypothesis from 6.161.2 to 6.163.0


### Documentation

- **settings**: document the repo-root path convention; make it hold everywhere
- **todo**: document real life console hang
- **todo**: track the unraisable subprocess-transport flake + prompt-then-freeze reboot consideration
- **todo**: record bootstrap's accumulating discovery errors as a follow-up
- chaos hardening spec + plan 1 (lifecycle core)


### Fixed

- **host**: close the post-timeout recovery leg for a lost connection
- **host**: surface a lost SSH connection as a CommandResult, not a traceback
- **chaos**: resource-slice legs exclude both bed-hostile tiers
- **settings**: anchor relative settings.toml paths to the repo root
- **web**: await the react-aria breadcrumb collection in FilePage's crumbs test
- **tests**: reset otto.bootstrap's module caches between tests
- **import-budget**: keep otto.lifecycle off the CLI --help import paths (#180)
- **docs**: document otto.lifecycle, drop private-typed params from signatures


## [0.8.1] - 2026-07-31

### Added

- **BREAKING** **host**: give every command surface a default timeout
- **web**: raise TS/vitest strictness to Python-tree parity
- **cov**: per-ticket coverage follow-ups — config-hardened git, drift guards, ticket UX
- **BREAKING** **cov**: per-ticket coverage attribution, #/tickets page, and tickets.json export


### Documentation

- **cov**: drop a .gcov artifact claim the pipeline never produced


### Fixed

- **web**: make the tier-1 perf guards deterministic — count work, not time
- **web**: give the series chips an accessible name, un-mute the warning
- **web**: un-hand-edit the vendored slideout scrim, move z-50 to callers
- **cov**: key column claimed a focused context on every page


## [0.8.0] - 2026-07-26

### Added

- **BREAKING** **cov**: delete the Jinja render lane, rename --report to --dir, expand coverage docs
- **cov**: coverage SPA report — covapp replaces the Jinja renderer output
- **cov**: coverage store v4 — run host identity, thresholds, stats vocabulary, ticket slot
- **cov**: manual-coverage validity engine — rename-following anchors, O(1) git batching, supersede
- **deps**: dependency-status panel subtitle, graph pins, root sys.path guard
- **deps**: inter-project dependency management for OTTO_SUT_DIRS repos


### Changed

- **tests**: replace asyncio leak detector heap scan with creation-time registry


### Dependencies

- **deps**: bump react-dom to 19.2.8 alongside react
- **deps**: bump react from 19.2.7 to 19.2.8 in /web
- **deps-dev**: bump ty from 0.0.61 to 0.0.63
- **deps-dev**: bump ruff from 0.15.22 to 0.16.0
- **deps**: bump actions/checkout from 7.0.0 to 7.0.1
- **deps-dev**: bump hypothesis from 6.157.0 to 6.161.2
- **deps-dev**: bump @biomejs/biome from 2.5.4 to 2.5.5 in /web
- **deps-dev**: bump @vitejs/plugin-react from 6.0.3 to 6.0.4 in /web
- **deps-dev**: bump knip from 6.27.0 to 6.29.0 in /web
- **deps**: bump sse-starlette from 3.4.5 to 3.4.6
- **deps**: bump fastapi from 0.139.2 to 0.140.0
- **deps**: bump astral-sh/setup-uv from 8.3.2 to 9.0.0


### Fixed

- **BREAKING** **tests**: webassets consolidation + hermetic unit lane (#175)
- **labs**: normalise unknown lab.json section keys to str before sorting
- **lint**: satisfy ruff 0.16.0 — parenthesize implicit concat, keyword-only wide signatures


### Maintenance

- **githook**: update Opus version to 5


## [0.7.4] - 2026-07-23

### Added

- **host**: keep otto's commands out of shell history (on by default)
- **transfer**: file permission mode on transfers to hosts


### Fixed

- **tests**: pre-init each worker's coverage schema to kill the `no such table: context` race
- **tests**: close hosts in the shell-history e2e fixture
- **monitor**: frontend UX triage (8 items) + hover-scoped y crosshair


## [0.7.3] - 2026-07-20

### Added

- **cov**: catch line-shifted clang stale deploys via function checksums
- **cov**: structural .gcda/.gcno stamp check before lcov capture
- **testbed**: basecamp LLEXT enablement — ext_svc helper, uart1 protocol serial, 32 KB+ sizing


### Documentation

- **cov**: extend the .gcno stamp guard to GCC and clang Unix builds
- **cov**: per-build-type coverage subpages (GCC/clang/embedded) + README feature mention


### Fixed

- **monitor**: keep the per-run access key out of the log files


### Maintenance

- **vagrant**: updated playground host definition for easier addressing
- **vagrant**: set playground VM's SSH forward port


## [0.7.2] - 2026-07-19

### Added

- **make**: browser lane defaults to 2 workers behind a cores+RAM gate
- **BREAKING** **make**: tier `make nox`, env-gate browser sharding, exclude stability from parallel runs (#plan 2026-07-18)
- **BREAKING** **monitor**: dashboard event marking + chart gesture rework (#spec 2026-07-18)
- **BREAKING** **monitor**: collapse CPU into one chart, drop per-PID tracking


### Dependencies

- **deps**: bump @fontsource-variable/inter from 5.2.8 to 5.3.0 in /web
- **deps-dev**: bump hypothesis from 6.156.6 to 6.157.0
- **deps**: bump the tailwindcss group in /web with 2 updates
- **deps-dev**: bump @vitest/coverage-v8 to 4.1.10 alongside vitest
- **deps-dev**: bump vitest from 4.1.9 to 4.1.10 in /web
- **deps-dev**: bump @biomejs/biome from 2.5.3 to 2.5.4 in /web
- **deps**: bump tailwindcss from 4.3.2 to 4.3.3 in /web
- **deps-dev**: bump nox from 2026.4.10 to 2026.7.11
- **deps**: bump typer from 0.26.8 to 0.27.0
- **deps-dev**: bump ty from 0.0.58 to 0.0.61
- **deps**: bump fastapi from 0.139.0 to 0.139.2
- **deps-dev**: bump vite from 8.1.4 to 8.1.5 in /web
- **deps-dev**: bump ruff from 0.15.21 to 0.15.22
- **deps**: bump actions/setup-node from 6.4.0 to 7.0.0


### Fixed

- **tests**: banner spec fed epoch-ms to pause_at, which takes seconds (#161)
- **monitor,web**: catchable startup failures, CI deflakes, palette Backspace, UI polish
- **monitor**: catchable server startup failures; deflake banner + port tests
- **types**: declare options helpers take a dataclass type
- **vagrant**: don't let the SDK toolchain probe abort a fresh provision


## [0.7.1] - 2026-07-18

### Added

- **BREAKING** **monitor-web**: Untitled UI command layer + topology landing view (#specs 2026-07-17)
- **vagrant**: add a lightweight playground VM for user-perspective otto testing
- **BREAKING** **cli**: otto init sample plumbing, full settings.toml, schemas area (#spec 2026-07-17)
- **BREAKING** **monitor**: gate the dashboard behind a per-run access key; optional TLS
- **tunnel**: stability suite (make stability-tunnel) + fix racing add_tunnel
- **BREAKING** **bed**: move the 192.168.1.x data plane to a dedicated eth2 NIC; tunnel stability suite spec + plan
- **BREAKING** **monitor**: render live tunnels as overlays in the topology view
- **cli**: show the banner only on help screens, never during execution (#140)


### Changed

- **BREAKING** **make**: Python↔TS quality/test parity (language-axis targets + merged TS coverage gate)


### Documentation

- post-merge follow-ups — host-schema refs to lab-config, complete seam digest
- **BREAKING** restructure the toctree by functional area; make architecture design-only


### Fixed

- **monitor**: stop the live-chart tooltip crashing mid-zoom (getRawIndex of undefined)
- **monitor**: open the --db session archive eagerly and atomically
- **cli**: lab-scope host-id tab completion everywhere, not just otto host
- **BREAKING** **tunnel**: docker never required or started at endpoints; Rich-table list; real 192.168.1.x data plane (#139)


### Maintenance

- post-merge follow-ups to the Makefile Python↔TS parity work


## [0.7.0] - 2026-07-15

### Added

- **BREAKING** **monitor**: lay the topology map out by the data plane, not the hop chain
- **BREAKING** **monitor**: close the 5b spec gaps; adopt Untitled UI as the shell's foundation
- **BREAKING** **lab**: combine labs with '+' instead of ','
- **BREAKING** **monitor**: live streaming into the session-shaped shell
- **monitor**: retire the fixture-stem enumeration; topology polish
- **monitor**: collapse the topology edge encoding and fix the link inspector
- **BREAKING** **monitor**: sessionized capture and a real format:1 producer
- **web**: explain the topology canvas and stop its edges from disappearing
- **web**: monitor topology — hop-layered map, link inspector, reachability cascade
- **BREAKING** **link**: port-scoped impairment — degrade one service's traffic per link
- **web**: monitor views — derived health, fleet grid, synced ECharts stack, events
- **BREAKING** **api**: post-extraction polish — Result naming, named errors, log ergonomics
- **BREAKING** **web**: review-first monitor shell — Import front door, hash routing, behavior-spec pivot
- **BREAKING** **api**: library-first suite/coverage/reservations + breaking renames
- **monitor**: versioned export format (format: 1) + committed dummy-data fixtures
- **link**: otto link impair/repair/list — netem impairment with endpoint & in-path placements
- **BREAKING** **tunnel**: otto tunnel CLI — bidirectional multi-hop socat tunnels + docker endpoints
- **BREAKING** **cov**: per-run coverage contexts — line-level run traceability
- **cov**: ignore whitespace-only changes in manual-coverage line remapping
- **link**: otto link CLI + live host-resident socat tunnels
- **BREAKING** **host**: derive host id from slugged element; lab-scoped logical index; display name
- **schema**: export lab.json object schema + link schema, retire hosts array schema
- **link**: async discovery contract and all_links reconciliation
- **link**: versioned owner-agnostic sentinel codec + discovery parser
- **link**: declared-link resolution, implicit hop derivation, Lab.static_links()
- **BREAKING** **lab**: hard cutover hosts.json -> lab.json object with hosts/links sections
- **link**: runtime Link/LinkEndpoint/Provenance with deterministic route ids
- **link**: LinkSpec/LinkEndpointSpec boundary models for lab.json links entries
- **host**: interfaces become netdev-keyed Interface objects with string shorthand


### Changed

- **BREAKING** **tunnel,link**: daemon toolkit + pluggable TunnelCarrier seam


### Dependencies

- **deps-dev**: bump ty to 0.0.58 and fix the diagnostics it adds
- **deps-dev**: bump @types/node from 26.1.0 to 26.1.1 in /web
- **deps-dev**: bump vite from 8.1.3 to 8.1.4 in /web
- **deps-dev**: bump hypothesis from 6.156.1 to 6.156.6
- **deps**: bump uvicorn from 0.50.0 to 0.51.0
- **deps-dev**: bump ruff from 0.15.20 to 0.15.21
- **deps-dev**: bump @biomejs/biome from 2.5.2 to 2.5.3 in /web
- **deps**: bump astral-sh/setup-uv from 8.3.0 to 8.3.2
- **deps-dev**: bump typescript from 6.0.3 to 7.0.2 in /web


### Documentation

- **spec**: port-scoped link impairment design
- **spec**: library extraction + breaking renames design
- lab.json cutover across living docs + otto.link API pages
- **plans**: link foundation implementation plan (sub-project #1)
- **plans**: planned out `otto link` feature


### Fixed

- **web**: format two live-streaming test files with Biome
- **monitor**: make the link inspector reserve space instead of overlaying the map
- **web**: make parallel same-column links independently clickable (#131)
- **test**: wait for React Flow's edges instead of snapshot-counting them (#130)
- **test**: evict side-effect origin modules in _isolate_registries (the other half of #108)
- **web**: topology zoom controls follow the app's dark theme
- **docs**: rewrite dashboard media capture for the review-first shell
- **BREAKING** **host**: daemons survive last-logout (linger) and telnet-term ps scans (\grep)
- **docs**: stub the dashboard media capture — the live page it photographs is gone
- **web**: exclude generated monitor fixtures from Biome
- **monitor**: re-abort late connections in force_stop so shutdown converges
- **docs**: exclude discover_dynamic_links_status from otto.link automodule
- **link**: lowercase protocol in link id, contain unrelated-lab link errors, harden doctor + tests
- **link**: harden cross-lab addressing build against malformed unrelated-lab host records
- **test**: isolate global registries, tmp imports, and otto.cli module identity across repeats


### Maintenance

- **vscode**: exclude worktrees from the file watcher and search
- **agents**: define worktree discipline
- increased dev VM RAM to 4 GB


## [0.6.0] - 2026-07-06

### Added

- **host**: login-proxy resync via confirm_live; retire interim knobs + lookbehind
- **host**: recover_session via confirm_live (echo-proof, fixes REPL false-positive)
- **host**: echo-proof exit-code recover probe on BashFrame; recover_pattern
- **host**: shared confirm_live shell-liveness loop
- **host**: per-session timeout override for app_shell() and attach()
- **host**: host.app_shell() context manager + public exports
- **host**: AppShell REPL abstraction with prompt-regex cmd() and session locking
- **host**: Parsed models with nested regex-region parsing
- **result**: ShellResult for AppShell commands
- **host**: login-proxy e2e on the mysql bed; public exports
- **host**: interact --as-user replays login-proxy hops over the bridge
- **host**: oneshot/nc route through proxied pool sessions when the user is proxied
- **host**: proxied logins at session establishment (default, named, pooled)
- **host**: switch_user/as_user route through the login-proxy engine; _perform_su deleted
- **BREAKING** **host**: migrate in-repo lab data + schemas to list-creds
- **BREAKING** **host**: creds become list[Cred]; ConnectionManager resolves the direct-auth chain
- **models**: CredSpec list-creds boundary validation
- **host**: perform_switch engine with recursive via-switching
- **host**: login-proxy registry, Cred, chain resolution, built-in su proxy
- **web**: EventTable — kind="table" tabs render log-event rows
- **web**: log-event data layer — store slice, SSE dispatch, /api/data hydration
- **BREAKING** **monitor**: TabSpec kind/columns — table tabs on the /api/meta wire
- **monitor**: RegexLogEventParser — named groups become table columns
- **monitor**: log-event persistence and wire — DB table, batched SSE, /api/data
- **monitor**: CsvMetricParser + timestamp high-water mark
- **monitor**: parse_tick contract — timed samples, log events, collector cutover
- **examples**: UptimeParser — executed custom-parser template + scoping integration test
- **monitor**: regex host patterns for register_host_parsers (fullmatch, loud ambiguity)
- **monitor**: enterprise net/fs OID contract + named SNMP bundles (otto-core/net/fs)
- **BREAKING** **monitor**: SNMP counter->rate + meta_of descriptors (process_snmp_values replaces points_from_values)
- **monitor**: parser-health warnings (edge-triggered failures + never-produced backstop)
- **monitor**: Swap series rides free -b in MemParser
- **monitor**: ProcCountParser — runnable/blocked/total process counts
- **monitor**: PerCoreCpuParser — busy%% per core from /proc/stat deltas
- **monitor**: DiskIoParser — per-device B/s from /proc/diskstats deltas
- **monitor**: SocketsParser — established/time-wait counts from ss -s
- **monitor**: NetDevParser — per-interface rx/tx rates on a Network tab
- **monitor**: shared counter->rate helpers + ParseContext.ts
- **cov**: otto cov report --prefix strips a display root from report paths
- **BREAKING** **cov**: report sorter served from the vite-built covreport bundle
- **web**: TypeScript port of the coverage-report sorter with Vitest pins
- **cov**: clang gcov support — stamp-based discovery + llvm-cov capture
- **BREAKING** **web**: React monitor dashboard replaces the vanilla-JS frontend
- **cli**: cov get defaults to the standard per-invocation output dir
- **cov**: persist per-file excluded_lines in store.json
- **cov**: expand ${sut_dir} in tier harvest_dirs on read
- **cov**: tier-colored rendering, legend, state rows, provenance table
- **cov**: reporter consumes captures, manual store, unit harvest; e2e pin guard
- **cli**: otto cov clean — zero remote .gcda counters
- **cli**: otto cov get — single retrieval command; test --cov emits captures
- **cov**: per-board capture.json production from fetched counters
- **cov**: line states + provenance in store; blob-anchored manual validity pass
- **cov**: manual capture store dir + exclusion-marker scan
- **cov**: Capture artifact model with dirty-tree remap and blob anchors
- **cov**: bidirectional hunk remap engine
- **cov**: git plumbing helpers for capture pinning
- **cov**: runtime TierConfig accessor for declarative tiers
- **cov**: typed [coverage] settings with declarative tiers, colors, exclusions
- **cli**: warm --tests completion with pytest-collected test names
- **cli**: tab completion for --lab and --tests
- **monitor**: MonitorServer.force_stop(); harness sheds its uvicorn reach-in
- **monitor**: project-level register_parsers() — extend/override defaults for all hosts
- **monitor**: per-parser collection intervals via per-bucket loops
- **BREAKING** **monitor**: parser API v2 — parse(output, *, ctx: ParseContext); no more parser mutation
- **monitor**: typed /api/meta contract (ChartSpec/TabSpec/MonitorMeta) + schema export
- **cli**: otto reservation is lab-free — whoami needs no lab, check loads it lazily
- **cli**: otto init epilogue — next steps + idempotent re-runs
- **cli**: otto init doctor mode — validate existing areas via real ingestion
- **cli**: otto init prompt/flag semantics — per-area confirms, --all, area flags
- **cli**: otto init — area scaffolds with inline templates
- **models**: allow _-prefixed annotation keys in hosts.json entries
- **test**: suite-less selection runs — --tests names and -m alone
- **suite**: default-construct suite Options per class in multi-suite runs
- **suite**: auto-register Test* OttoSuite subclasses; delete @register_suite
- **cov**: incremental product builds + helpful polluted-tree error, no tracebacks
- **cli**: third-party group subcommands tab-complete on the fast path
- **cli**: live Typer apps without help= inherit their own Typer-native help
- **cli**: unified command registry + bootstrap composition root; user-extensible top-level CLI
- **BREAKING** **result**: unify host-verb returns into the Result family
- **cli**: built-in local host + per-command output dirs; fix --help crash across groups
- **cli**: CLI-subprocess e2e coverage + hostless marker + 3 surfaced fixes
- **BREAKING** **host**: remove the repeat-command scheduler (RepeatRunner)


### Changed

- **host**: fold session handshake onto confirm_live
- **host**: give bash recover its own distinct digit marker (drop drain)
- **monitor**: extract history import/export; acknowledge new modules in import budget
- **monitor**: extract MetricStore from MetricCollector
- **monitor**: extract MetricDB from MetricCollector
- **monitor**: extract Broadcaster from MetricCollector
- remove 'from __future__ import annotations' across src (repo ban)


### Dependencies

- **deps-dev**: bump @types/node from 24.13.2 to 26.1.0 in /web
- **deps**: bump plotly.js-gl2d-dist-min from 3.6.0 to 3.7.0 in /web
- **deps**: bump astral-sh/setup-uv from 8.2.0 to 8.3.0
- **deps**: bump typing-extensions from 4.15.0 to 4.16.0
- **deps**: bump fastapi from 0.138.1 to 0.139.0
- **deps**: bump asyncssh from 2.23.1 to 2.24.0
- **deps-dev**: bump ty from 0.0.55 to 0.0.56
- **deps-dev**: bump hypothesis from 6.155.7 to 6.156.1
- **deps**: bump uvicorn from 0.49.0 to 0.50.0
- **deps**: raise ruff floor to 0.15.20, the currently-locked version


### Documentation

- **todo**: add e2e embedded REPL-product harness to the embedded-recovery follow-up
- **todo**: tee up fresh-worktree web-dist build gap (make coverage Error 127)
- **todo**: queue embedded-recovery tests; mark echo-proof I-3 fix implemented
- **monitor**: design spec for Untitled UI + ECharts redesign
- **web**: frontend redesign plan
- **todo**: tee up TS-tooling follow-ups
- **contributing**: document the web/ TypeScript quality gates
- **todo**: track echo-proof recover_session follow-up + deferred AppShell review minors
- **host**: note attach() session-discard caveat after app-shell timeout
- AppShell cookbook, login-proxy extending guide, list-creds host-database guide
- **plan**: update login-proxy and app shell plan
- **todo**: monitor Phase 3 Plan B ship-as-noted follow-ups
- **monitor**: log-sourced data guide — CSV digests, syslog event tables, large files
- **monitor**: Plan B implementation plan — log-sourced data
- **todo**: monitor Phase 3 Plan A ship-as-noted follow-ups
- **monitor**: Phase 3 metrics — Unix parser tables, OID contract, bundles, warnings
- **monitor**: edge-triggered parser-health warnings + Plan A implementation plan
- **monitor**: Phase 3 metrics-expansion design spec
- **cov**: build-time screenshot of the coverage report in the guide
- **tests**: restore browser-guard rationale dropped in the extraction
- **cov**: align guide/reference/architecture with the shipped pipeline
- **plan**: record deferred lcov-rc wiring for custom exclusion markers
- **cov**: custom markers are render-only; provenance table is manual-only; filename pattern
- **cov**: declarative tiers, otto cov get/clean, manual captures, validity semantics
- **todo**: spec the collected-tests completion cache follow-up
- showcase --lab / --tests completion, document the static-scan boundary
- **architecture**: rename "pillars" to "first-party commands"
- **monitor**: API pages for the decomposed modules; ctx/interval/register_parsers guide
- **monitor**: note dormant orphaned-bucket risk at the gather site
- **plans**: save plans and specs for command revamps
- **architecture**: restructure as a story — pillars, lifecycle tree, subsystems, utilities
- onboarding rewritten around otto init; underscore-key idiom documented
- **test**: selection-run syntax, discovery-scope tests key, decorator-less suites
- **specs**: otto init scaffolding + pytest-native flexibility designs and plans
- accuracy sweep across all pages + new architecture tree
- **spec**: selection-based otto test — listing + running by suite/marker/test
- **spec**: test/suite listing rework — registry-based --list-suites + hardened collect_tests
- make @options the standard options decorator


### Fixed

- **test**: reap orphaned uvicorn transports in dashboard harness teardown
- **make**: retry npm ci in web-install only on network-class failures
- **host**: fail-fast on dead session in send/expect; collect long-dead session tests
- **host**: harden login-proxy resync against the tty-flush window
- **test**: give monitor scoping e2e an adequate per-tick timeout budget
- **make**: auto-build web dist for coverage/dashboard on fresh checkouts
- **monitor**: close listening sockets before aborting in force_stop
- **make**: npm-ci web/ deps at the start of release/all/ci
- **test**: isolate the SUITES registry per suite test; guard it in CI
- **monitor**: prime the SSE stream so Firefox reaches "live" at once
- **host**: mark session for recovery when app-shell launch times out
- **host**: parse type-conversion failures return a failed ShellResult; reject non-Parsed list fields
- **host**: line-anchor login-proxy resync marker so it is sound on echo-on bridges
- **host**: resync shell after login-proxy transitions to survive su/sudo tty flush
- **host**: consume matched bytes in _BridgeProxyIO.expect so multi-hop replay waits per hop
- **host**: symmetric full via-cred lookup at session-establishment proxy
- **host**: wrap all login-proxy failures in LoginProxyError
- **vagrant**: retry west update to survive transient module-fetch resets
- **monitor**: provisional-tail guard — confirm the final line across reads
- **monitor**: final-review fixes — log-only hosts, torn-line guard, year rollover
- **monitor**: final-gate fixes — ruff format drift, ty-narrowed table columns
- **monitor**: export/json key-set pin missed log_events
- **monitor**: inject year before strptime for year-less timestamp formats
- **docs**: wire up otto.monitor.rates API page; fix two docstring gates
- **monitor**: explicit params in net-descriptor helper (ty gate)
- **monitor**: failed ticks still parse and record points; only health bookkeeping is success-gated
- **monitor**: silent-parser backstop counts only succeeding ticks; no literal None in failure msg
- **completion**: scope `otto host <TAB>` to the selected lab
- **test**: dashboard dist-guard matcher mirrors the browser tests' full marker set
- **test**: hermetic OTTO_* env — ambient sut_dirs broke the bootstrap test
- **cov**: anchor blobs cwd-relative — nested sut_dir produced empty captures
- **test**: otto test --cov-report renders via the collection model
- **cov**: scope `get --clean` to unix hosts; robust report errors
- **cov**: remap dirty-tree e2e captures HEAD→worktree at report time
- **cov**: resolve relative harvest_dirs against the repo root
- **cov**: clean one-line errors for all new failure modes
- **cov**: exclusion display is render-time; thread extra_markers reporter→renderer
- **cli**: cov clean scopes its sweep to the computed unix hosts
- **cli**: test --cov capture tail never fails the run; cover the tail with tests
- **cov**: preserve never-reached branch state through capture format
- **cov**: position-aware START/STOP handling in exclusion scan
- **cov**: skip unanchored sources (no HEAD blob) in build_capture
- **cov**: gitio raises on non-repo paths; cat_blob routed through _run
- **cli**: user-facing help for the otto test group
- **monitor**: concurrent tick cadence restored; project parsers reach historical catalog; pin gaps closed
- **monitor**: historical collectors declare the parser catalog — --file mode renders again
- **monitor**: release the flock fd when MetricDB.open() fails mid-way
- **test**: add dashboard browser coverage; playwright-proof pytester inner runs; provision chromium libs
- **test**: selection runs skip non-matching repos, per-repo --results, loud --tests+suite conflict
- **cli**: otto init lab doctor rejects non-object hosts.json entries cleanly
- **cli**: otto init epilogue honors comma-or-pathsep OTTO_SUT_DIRS convention
- **cli**: otto init derives area names from existing settings.toml, not the dir name
- **suite**: narrow cls before use in suite_options; docstring + comment polish
- **test**: cut conftest loading at the repo root, not the suite dir
- **firmware**: backport Zephyr fs-shell mount-leak fix for 2.7 and 3.7 beds
- **host**: probe mount state with statvfs instead of leaking Zephyr re-mounts
- **test**: park repo1 suites by source FILE, not origin prefix
- **host**: built-in local host is not fleet — all_hosts/do_for_all_hosts exclude it
- **test**: close the sys.modules isolation gap; fix embedded-cov e2e Result drift
- **cli**: --show-lab/--list-hosts fail loud on contained bootstrap errors
- **cli**: completion resolves real commands by dispatch target only, not COMP_WORDS sniffing
- **cli**: contain phase-1 config-data errors; otto --help survives malformed settings.toml
- **test**: list otto-test suites from the registry; harden collect_tests; add --list-tests/--list-markers


### Maintenance

- **todo**: remove login proxy and app shell as todo items
- **todo**: add frontend component framework update to todo list
- **sql**: update vagrant config to install MySQL on unix hosts
- **todo**: tweaked ideas around application shells
- **todo**: added more future ideas to todo list
- **plans**: save plans for future work
- **todo**: added future work items
- **todo**: mark termynal docs item done
- **todo**: prune stale files; re-verify fable ranked list against main
- **todo**: update todo list
- **cli**: hygiene batch from the registry-unification final review
- **githook**: update Claude model names
- **todo**: updated review by fable


## [0.5.4] - 2026-06-30

### Added

- **logger**: three-sink logging with per-command LogMode + library capture


### Changed

- **host**: finish LogMode API — pure LogMode + session.log transcript
- **otto**: make `import otto` import-light via PEP 562 lazy exports (Part D)
- trim otto startup imports + add deterministic import-budget guard
- **lint**: clear PGH003 + empty the ratchet (strict-linting phase S)
- **lint**: docstring formatting + deny D105/D107 (strict-linting phase D-1)
- **lint**: annotate src/scripts (strict-linting phase A)
- **lint**: clear cleanup-straggler ratchet debt (strict-linting phase 4b)
- **lint**: clear naming & bug-class ratchet debt (strict-linting phase 4)
- **lint**: clear UP/RUF/PERF ratchet debt (strict-linting phase 3)


### Documentation

- **cli,scripts**: docstrings for app layer + clear D ratchet (strict-linting phase D-4)
- **models,coverage,configmodule**: docstrings for the data/config layer (strict-linting phase D-3)
- **host**: docstrings for the public Host API (strict-linting phase D-2)
- **lint**: add more linting plans
- **lint**: linting design
- **logger**: three-sink logging design spec
- **lint**: add plans for stricter linting and formatting


### Fixed

- **import-budget**: gate on non-stdlib module count, not raw sys.modules (#88)
- **configmodule**: define get_completion_names before apply_repo_settings (circular import)
- **lint**: adopt bugbear/comprehension/simplify — B,C4,SIM,PIE (Phase 2)


### Maintenance

- **todo**: update todo items
- **lint**: clean up ruff ignore list
- **todo**: update todo list items
- **lint**: adopt ruff format @100 + strict select=ALL ratchet + wire gate (Phase 0+1)


## [0.5.3] - 2026-06-28

### Added

- **host**: kernel-module load/unload/lsmod + per-class CLI parsers
- **host**: per-session current_user tracking & elevation (Spec A)
- **nfs**: NFS-readiness — monitor DB journal adapt + time-boxed log rotation


### Changed

- **BREAKING** **logger**: make 'otto' a plain logging.Logger


### Dependencies

- **deps**: bump telnetlib3 from 4.0.4 to 4.0.5
- **deps-dev**: bump bump-my-version from 1.3.0 to 1.4.1
- **deps**: bump actions/checkout from 6.0.3 to 7.0.0
- **deps**: bump pydantic-settings from 2.14.0 to 2.14.2
- **deps**: bump pydantic from 2.12.5 to 2.13.4
- **deps-dev**: bump ty from 0.0.54 to 0.0.55
- **deps**: bump starlette from 0.52.1 to 1.3.1
- **deps**: bump typer from 0.26.7 to 0.26.8
- **deps-dev**: bump hypothesis from 6.155.2 to 6.155.7
- **deps**: bump sse-starlette from 3.4.4 to 3.4.5
- **deps**: bump fastapi from 0.136.3 to 0.138.1


### Documentation

- save forgotten plans and todo files
- **release**: drop manual-publish make targets; scrub publishing docs
- drop stale version string from docs title
- **logger**: implementation plan for standard-logging refactor
- **logger**: spec for standard-logging refactor (subclass removal + output_dir→OttoContext); split out import-light __init__


### Fixed

- **logger**: migrate repo1 sample instructions off logger.output_dir
- **host**: tolerate child exiting mid-scan in LocalSession recovery


## [0.5.2] - 2026-06-26

### Added

- **testing**: backend conformance suite + sample reference backends
- **storage**: make the host source a registered, pluggable backend
- **reservations**: modernize interface — multi-holder who_reserved, named-registry backends, -R break-glass, cached --as-user completion


### Dependencies

- **deps**: bump pytest to 9.1.1 and fix show_test_item() call
- **deps-dev**: bump ruff from 0.15.17 to 0.15.20
- **deps-dev**: bump ty from 0.0.49 to 0.0.54


### Documentation

- host-database guide, reservations upgrade, team-setup onboarding
- enable Sphinx nitpicky with zero ignores


## [0.5.1] - 2026-06-25

### Added

- **cli**: converge run/put/get/login onto @cli_exposed


### Changed

- **tests**: speed up the suite — host-pool lease, front-load, docker pooling
- **tests**: restructure into unit/integration/e2e tiers + dedup (fable #5)
- **cli**: skip host build during otto-host verb completion


### Documentation

- harden doctest coverage — execute examples, add gates, drop the +SKIP charade
- **tests**: test-suite speedup design + plan + baseline (fable follow-up)
- **tests**: spec + plan for test-suite restructure & dedup (fable #5)


### Fixed

- **build**: put venv on PATH for release-flow tools in Makefile
- **tests**: re-group docker-e2e CLI tests to avoid coverage schema-init race


### Maintenance

- **lab**: docker on all three Unix test VMs + DRY their definitions


## [0.5.0] - 2026-06-22

### Added

- **cli**: auto-expose host methods as class-scoped `otto host` subcommands
- **host**: converge product defaulting into one [host_preferences] block
- **host**: switch term/transfer via the override seam; drop in-place setters
- **host**: term families + product host_preferences capability resolution
- **host**: lab-declared term/transfer menus with resolved active selection
- **host**: register products via code providers at host ingest
- **host**: add posix remote file operations (PosixFileOps mixin) + embedded subset
- **host**: add power control, reboot/shutdown & reachability waits to hosts
- **host**: add privilege elevation (sudo, su, as_user) to hosts
- **host**: add dependency-injected product lifecycle to hosts


### Changed

- **host**: defer asyncssh/aioftp/telnetlib3 to point-of-use
- **models**: drop products from HostSpec — it's repo-logic, not lab data


### Dependencies

- **deps**: declare starlette as an explicit dependency
- **deps**: bump uvicorn from 0.48.0 to 0.49.0


### Documentation

- **host**: restructure otto host guide into a nested Hosts group; refresh currency
- **deps**: list starlette in the dependency table
- **todo**: added minor cleanup tasks
- **host**: capability-resolution spec + host-preferences end-state backlog
- **host**: add product-providers design spec


### Fixed

- **host**: render element_id as its number in host id/name


## [0.4.5] - 2026-06-19

### Added

- **suite**: @options decorator + first-class pydantic Options validation
- **cli**: support Typer 0.26 — re-home runner options via public Context injection


### Changed

- **host**: split transfer.py into a per-backend transfer/ package
- **models**: collapse pure-data forward types into frozen pydantic dataclasses


### Fixed

- **scripts**: qemu-restart selects embedded guests by creds shape, not os_type


## [0.4.4] - 2026-06-18

### Added

- **host**: registry public API for term/transfer backends (WS#4)


## [0.4.3] - 2026-06-17

### Added

- **schema**: otto schema export — JSON Schema for hosts.json / settings.toml / reservations
- **models**: pydantic monitor records — MetricPoint + import/export rows + frozen SnmpMetric
- **models**: pydantic settings boundary — SettingsModel + docker/os-profile/reservation specs + OttoEnvSettings (Phase A plan 3)
- **models**: integrate host specs — registry carries spec, factory collapse, command_frame, interfaces (Phase A 2b)
- **models**: host spec models (HostSpec/Unix/EmbeddedHostSpec) — Phase A plan 2a
- **models**: pydantic boundary layer + option two-type split (Phase A, plan 1)


### Changed

- **BREAKING** **naming**: snake_case sweep — element vocab, os_* fields, host filenames, API


### Documentation

- list pydantic + pydantic-settings in getting-started dep table


### Fixed

- **docs**: extend RST title underlines broken by the WS#2 rename


### Maintenance

- **plans**: record plan progress
- **plans**: registry factory integration
- **plans**: save some plans
- **plans**: saving claude's plans


## [0.4.2] - 2026-06-14

### Added

- **context**: explicit OttoContext runtime + deterministic host lifecycle


### Dependencies

- **deps-dev**: bump ruff from 0.15.16 to 0.15.17
- **deps-dev**: bump ty from 0.0.45 to 0.0.49
- **deps**: bump asyncssh from 2.23.0 to 2.23.1


### Documentation

- **guide**: add library-usage to user-guide toctree


### Fixed

- **test**: stop nightly stability flake — uncharge the 2s retry backoff from tight test budgets


### Maintenance

- **tasks**: save specs and designs


## [0.4.1] - 2026-06-13

### Added

- **host**: add embedded load()/unload() binary-load API + BinaryLoader strategy
- **host**: add per-command log=False to hide noisy commands from logs
- **host**: log buffered-frame output via parse_output, not the raw stream


### Documentation

- **todo**: Review feedback of otto's design


### Fixed

- **host**: satisfy ty — cast resolved ShellCommand.log to bool


## [0.4.0] - 2026-06-12

### Added

- **BREAKING** **host**: OS-agnostic EmbeddedHost + host-class registry (ZephyrHost)
- **embedded**: Zephyr embedded-host support + test-suite stability hardening (#52)


### Changed

- **test**: drop local loop-cleanup guards now covered by the reaper


### Dependencies

- **deps-dev**: bump ty from 0.0.39 to 0.0.45
- **deps-dev**: bump ruff from 0.15.14 to 0.15.16
- **deps**: bump astral-sh/setup-uv from 8.1.0 to 8.2.0
- **deps**: bump fastapi from 0.136.1 to 0.136.3
- **deps-dev**: bump hypothesis from 6.152.9 to 6.155.2
- **deps**: bump actions/checkout from 6.0.2 to 6.0.3
- **deps**: bump uvicorn from 0.47.0 to 0.48.0
- **deps**: bump telnetlib3 from 4.0.3 to 4.0.4 (#44)


### Documentation

- **embedded**: repoint orphaned :doc: ref after relocating the FS how-to
- **embedded**: relocate embedded-extension how-tos into the guide; drop stale API pages
- add embedded/lab-config/os-profile guides
- **host**: plan generic EmbeddedHost + host-class registry
- **make**: group `make help` with nox/coverage/stability-{unit,unix,embedded} shorthand


### Fixed

- **lab-health**: route host probes by credential shape, not osType literal
- **docker**: poll for the container id after a successful compose up
- **docker**: retry compose up once past the libnetwork "network not found" race
- **embedded**: force LLEXT link tail to track the recompiled object
- **types**: satisfy ty 0.0.45's new lint rules
- **host**: never create an event loop in RemoteHost.__del__
- **cli**: make --xdir optional again, defaulting to CWD
- **test**: don't let the loop reaper close wider-scoped pytest-asyncio loops
- **test**: reap orphaned pytest-asyncio loops to kill misattributed CI flake


### Maintenance

- **submodule**: ignore embedded-gcov worktree churn


## [0.3.6] - 2026-05-24

### Dependencies

- **deps-dev**: bump ty from 0.0.37 to 0.0.39 (#34)
- **deps**: bump telnetlib3 from 4.0.2 to 4.0.3 (#35)
- **deps-dev**: bump ruff from 0.15.13 to 0.15.14 (#36)
- **deps-dev**: bump hypothesis from 6.152.7 to 6.152.9 (#33)
- **deps**: bump peter-evans/create-issue-from-file from 5 to 6 (#32)


### Fixed

- **configmodule**: close inner pytest.main()'s leaked event loop in collectTests
- **cli**: require --xdir and harden removeOldLogs against foreign trees


## [0.3.5] - 2026-05-19

### Added

- **telnet**: speed up telnet connect by removing the login drain


### Changed

- **transfer**: retire the dedicated nc monitor session


### Documentation

- add release runbook and PyPI install instructions


### Fixed

- **host**: stop close() from running a process-wide gc.collect()
- **transfer**: reap orphaned nc listener on cancelled transfer


## [0.3.4] - 2026-05-17

### Added

- **docker**: auto-start container stack on access when not running
- **configmodule**: exclude Docker containers from the fleet host generator
- **logger**: print output dir on exit; clean logs only on subcommands


### Dependencies

- **deps-dev**: bump ruff from 0.15.12 to 0.15.13 (#31)
- **deps**: bump uvicorn from 0.46.0 to 0.47.0 (#26)
- **deps-dev**: bump hypothesis from 6.152.4 to 6.152.7 (#27)
- **deps-dev**: bump nox-uv from 0.7.1 to 0.8.0 (#28)
- **deps**: bump sse-starlette from 3.4.2 to 3.4.4 (#30)
- **deps-dev**: bump ty from 0.0.34 to 0.0.37 (#29)


### Documentation

- true up CLI/API docs and add a dependency-table sync check


### Fixed

- **transfer**: serialize nc-get size prefetch on the monitor session
- **host**: connect oneshot pool sessions concurrently, not serially
- **host**: raise on undefined hop host ID
- **logger**: render console output at the true terminal width


## [0.3.3] - 2026-05-15

### Added

- **host**: bound nc listener wait to prevent infinite transfer hangs


### Fixed

- **host**: serialize ConnectionManager lazy-init to stop transport leaks
- **host**: clean up half-built telnet connections on cancellation
- **host**: recheck cached TelnetClient liveness in ConnectionManager
- **host**: serialize FTP transfers on the shared aioftp client
- **host**: recover ShellSession after external cancellation
- **host**: serialize SessionManager get-or-create paths


### Maintenance

- **todo**: add migration plan
- **todo**: Updated todo list
- **makefile**: Cleanup up Makefile help strings


## [0.3.1] - 2026-05-09

### Added

- Added docker container library and CLI support


### Dependencies

- **deps**: bump sse-starlette from 3.4.1 to 3.4.2 (#25)
- **deps**: bump asyncssh from 2.22.0 to 2.23.0 (#24)


### Documentation

- add python version support badges
- clarified that the `asyncio.run()` method is being used to call `Host.run()`
- added docker documentation to the doc tree


### Fixed

- **docker**: filter repos by default_host, not `--on`, for lab applicability
- fixed some type annotation errors


## [0.2.1] - 2026-05-05

### Added

- nox matrix on Python 3.10-3.14, OIDC release workflows (#12)
- add event monitoring to test suites
- add iteration banners
- add suite-level monitoring
- Added repo-wide and per-invocation host options


### Dependencies

- **deps**: bump sse-starlette from 3.3.3 to 3.4.1 (#18)
- **deps-dev**: update uv-build requirement from <0.11.0 to <0.12.0 (#17)
- **deps-dev**: bump ruff from 0.14.7 to 0.15.12 (#16)
- **deps-dev**: bump ty from 0.0.31 to 0.0.34 (#15)
- **deps**: bump actions/upload-artifact from 4 to 7 (#14)
- **deps**: bump actions/download-artifact from 4 to 8 (#13)
- **deps**: bump fastapi from 0.135.2 to 0.136.1 (#11)
- **deps**: bump telnetlib3 from 4.0.1 to 4.0.2 (#8)
- **deps**: bump uvicorn from 0.42.0 to 0.46.0 (#7)
- **deps-dev**: bump pytest-cov from 7.0.0 to 7.1.0 (#5)
- **deps**: bump typer from 0.24.1 to 0.25.1 (#19)
- **deps**: bump rich from 14.3.3 to 15.0.0 (#9)
- **deps-dev**: bump py-spy from 0.4.1 to 0.4.2 (#6)
- **deps**: bump tomli from 2.4.0 to 2.4.1 (#4)
- **deps**: bump pytest from 9.0.1 to 9.0.3 (#3)


### Documentation

- add host options to docs


### Maintenance

- Updated checkout and setup-uv github action versions (#20)
- Added plan for defining host default options
- Added official publish make target


## [0.1.0] - 2026-04-28

### Maintenance

- Added a `publish-test` makefile target


## [0.0.2] - 2026-04-28

### Fixed

- fix pytest requirement vs dev dependency


### Maintenance

- Added to the existing commit hook to prompt for commit type
- fixed release build versioning


## [0.0.1] - 2026-04-26

### Maintenance

- Made release target more verbose
- added GitHub templates
- set up release management

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/ludachrish3/otto-sh/compare/v0.8.8...v0.9.0
[0.8.8]: https://github.com/ludachrish3/otto-sh/compare/v0.8.7...v0.8.8
[0.8.7]: https://github.com/ludachrish3/otto-sh/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/ludachrish3/otto-sh/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/ludachrish3/otto-sh/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/ludachrish3/otto-sh/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/ludachrish3/otto-sh/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/ludachrish3/otto-sh/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/ludachrish3/otto-sh/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/ludachrish3/otto-sh/compare/v0.7.4...v0.8.0
[0.7.4]: https://github.com/ludachrish3/otto-sh/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/ludachrish3/otto-sh/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/ludachrish3/otto-sh/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/ludachrish3/otto-sh/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/ludachrish3/otto-sh/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/ludachrish3/otto-sh/compare/v0.5.4...v0.6.0
[0.5.4]: https://github.com/ludachrish3/otto-sh/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/ludachrish3/otto-sh/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/ludachrish3/otto-sh/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/ludachrish3/otto-sh/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/ludachrish3/otto-sh/compare/v0.4.5...v0.5.0
[0.4.5]: https://github.com/ludachrish3/otto-sh/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/ludachrish3/otto-sh/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/ludachrish3/otto-sh/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/ludachrish3/otto-sh/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/ludachrish3/otto-sh/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/ludachrish3/otto-sh/compare/v0.3.6...v0.4.0
[0.3.6]: https://github.com/ludachrish3/otto-sh/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/ludachrish3/otto-sh/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/ludachrish3/otto-sh/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/ludachrish3/otto-sh/compare/v0.3.2...v0.3.3
[0.3.1]: https://github.com/ludachrish3/otto-sh/compare/v0.2.1...v0.3.1
[0.2.1]: https://github.com/ludachrish3/otto-sh/compare/v0.1.0...v0.2.1
[0.1.0]: https://github.com/ludachrish3/otto-sh/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/ludachrish3/otto-sh/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/ludachrish3/otto-sh/releases/tag/v0.0.1

