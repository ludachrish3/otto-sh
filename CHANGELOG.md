# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.4] - 2026-07-23

### Added

- keep otto's commands out of shell history (on by default)
- file permission mode on transfers to hosts


### Fixed

- pre-init each worker's coverage schema to kill the `no such table: context` race
- close hosts in the shell-history e2e fixture
- frontend UX triage (8 items) + hover-scoped y crosshair


## [0.7.3] - 2026-07-20

### Added

- catch line-shifted clang stale deploys via function checksums
- structural .gcda/.gcno stamp check before lcov capture
- basecamp LLEXT enablement — ext_svc helper, uart1 protocol serial, 32 KB+ sizing


### Documentation

- extend the .gcno stamp guard to GCC and clang Unix builds
- per-build-type coverage subpages (GCC/clang/embedded) + README feature mention


### Fixed

- keep the per-run access key out of the log files


### Maintenance

- updated playground host definition for easier addressing
- set playground VM's SSH forward port


## [0.7.2] - 2026-07-19

### Added

- browser lane defaults to 2 workers behind a cores+RAM gate
- tier `make nox`, env-gate browser sharding, exclude stability from parallel runs (#plan 2026-07-18)
- dashboard event marking + chart gesture rework (#spec 2026-07-18)
- collapse CPU into one chart, drop per-PID tracking


### Dependencies

- bump @fontsource-variable/inter from 5.2.8 to 5.3.0 in /web
- bump hypothesis from 6.156.6 to 6.157.0
- bump the tailwindcss group in /web with 2 updates
- bump @vitest/coverage-v8 to 4.1.10 alongside vitest
- bump vitest from 4.1.9 to 4.1.10 in /web
- bump @biomejs/biome from 2.5.3 to 2.5.4 in /web
- bump tailwindcss from 4.3.2 to 4.3.3 in /web
- bump nox from 2026.4.10 to 2026.7.11
- bump typer from 0.26.8 to 0.27.0
- bump ty from 0.0.58 to 0.0.61
- bump fastapi from 0.139.0 to 0.139.2
- bump vite from 8.1.4 to 8.1.5 in /web
- bump ruff from 0.15.21 to 0.15.22
- bump actions/setup-node from 6.4.0 to 7.0.0


### Fixed

- banner spec fed epoch-ms to pause_at, which takes seconds (#161)
- catchable startup failures, CI deflakes, palette Backspace, UI polish
- catchable server startup failures; deflake banner + port tests
- declare options helpers take a dataclass type
- don't let the SDK toolchain probe abort a fresh provision


## [0.7.1] - 2026-07-18

### Added

- Untitled UI command layer + topology landing view (#specs 2026-07-17)
- add a lightweight playground VM for user-perspective otto testing
- otto init sample plumbing, full settings.toml, schemas area (#spec 2026-07-17)
- gate the dashboard behind a per-run access key; optional TLS
- stability suite (make stability-tunnel) + fix racing add_tunnel
- move the 192.168.1.x data plane to a dedicated eth2 NIC; tunnel stability suite spec + plan
- render live tunnels as overlays in the topology view
- show the banner only on help screens, never during execution (#140)


### Changed

- Python↔TS quality/test parity (language-axis targets + merged TS coverage gate)


### Documentation

- post-merge follow-ups — host-schema refs to lab-config, complete seam digest
- restructure the toctree by functional area; make architecture design-only


### Fixed

- stop the live-chart tooltip crashing mid-zoom (getRawIndex of undefined)
- open the --db session archive eagerly and atomically
- lab-scope host-id tab completion everywhere, not just otto host
- docker never required or started at endpoints; Rich-table list; real 192.168.1.x data plane (#139)


### Maintenance

- post-merge follow-ups to the Makefile Python↔TS parity work


## [0.7.0] - 2026-07-15

### Added

- lay the topology map out by the data plane, not the hop chain
- close the 5b spec gaps; adopt Untitled UI as the shell's foundation
- combine labs with '+' instead of ','
- live streaming into the session-shaped shell
- retire the fixture-stem enumeration; topology polish
- collapse the topology edge encoding and fix the link inspector
- sessionized capture and a real format:1 producer
- explain the topology canvas and stop its edges from disappearing
- monitor topology — hop-layered map, link inspector, reachability cascade
- port-scoped impairment — degrade one service's traffic per link
- monitor views — derived health, fleet grid, synced ECharts stack, events
- post-extraction polish — Result naming, named errors, log ergonomics
- review-first monitor shell — Import front door, hash routing, behavior-spec pivot
- library-first suite/coverage/reservations + breaking renames
- versioned export format (format: 1) + committed dummy-data fixtures
- otto link impair/repair/list — netem impairment with endpoint & in-path placements
- otto tunnel CLI — bidirectional multi-hop socat tunnels + docker endpoints
- per-run coverage contexts — line-level run traceability
- ignore whitespace-only changes in manual-coverage line remapping
- otto link CLI + live host-resident socat tunnels
- derive host id from slugged element; lab-scoped logical index; display name
- export lab.json object schema + link schema, retire hosts array schema
- async discovery contract and all_links reconciliation
- versioned owner-agnostic sentinel codec + discovery parser
- declared-link resolution, implicit hop derivation, Lab.static_links()
- hard cutover hosts.json -> lab.json object with hosts/links sections
- runtime Link/LinkEndpoint/Provenance with deterministic route ids
- LinkSpec/LinkEndpointSpec boundary models for lab.json links entries
- interfaces become netdev-keyed Interface objects with string shorthand


### Changed

- daemon toolkit + pluggable TunnelCarrier seam


### Dependencies

- bump ty to 0.0.58 and fix the diagnostics it adds
- bump @types/node from 26.1.0 to 26.1.1 in /web
- bump vite from 8.1.3 to 8.1.4 in /web
- bump hypothesis from 6.156.1 to 6.156.6
- bump uvicorn from 0.50.0 to 0.51.0
- bump ruff from 0.15.20 to 0.15.21
- bump @biomejs/biome from 2.5.2 to 2.5.3 in /web
- bump astral-sh/setup-uv from 8.3.0 to 8.3.2
- bump typescript from 6.0.3 to 7.0.2 in /web


### Documentation

- port-scoped link impairment design
- library extraction + breaking renames design
- lab.json cutover across living docs + otto.link API pages
- link foundation implementation plan (sub-project #1)
- planned out `otto link` feature


### Fixed

- format two live-streaming test files with Biome
- make the link inspector reserve space instead of overlaying the map
- make parallel same-column links independently clickable (#131)
- wait for React Flow's edges instead of snapshot-counting them (#130)
- evict side-effect origin modules in _isolate_registries (the other half of #108)
- topology zoom controls follow the app's dark theme
- rewrite dashboard media capture for the review-first shell
- daemons survive last-logout (linger) and telnet-term ps scans (\grep)
- stub the dashboard media capture — the live page it photographs is gone
- exclude generated monitor fixtures from Biome
- re-abort late connections in force_stop so shutdown converges
- exclude discover_dynamic_links_status from otto.link automodule
- lowercase protocol in link id, contain unrelated-lab link errors, harden doctor + tests
- harden cross-lab addressing build against malformed unrelated-lab host records
- isolate global registries, tmp imports, and otto.cli module identity across repeats


### Maintenance

- exclude worktrees from the file watcher and search
- define worktree discipline
- increased dev VM RAM to 4 GB


## [0.6.0] - 2026-07-06

### Added

- login-proxy resync via confirm_live; retire interim knobs + lookbehind
- recover_session via confirm_live (echo-proof, fixes REPL false-positive)
- echo-proof exit-code recover probe on BashFrame; recover_pattern
- shared confirm_live shell-liveness loop
- per-session timeout override for app_shell() and attach()
- host.app_shell() context manager + public exports
- AppShell REPL abstraction with prompt-regex cmd() and session locking
- Parsed models with nested regex-region parsing
- ShellResult for AppShell commands
- login-proxy e2e on the mysql bed; public exports
- interact --as-user replays login-proxy hops over the bridge
- oneshot/nc route through proxied pool sessions when the user is proxied
- proxied logins at session establishment (default, named, pooled)
- switch_user/as_user route through the login-proxy engine; _perform_su deleted
- migrate in-repo lab data + schemas to list-creds
- creds become list[Cred]; ConnectionManager resolves the direct-auth chain
- CredSpec list-creds boundary validation
- perform_switch engine with recursive via-switching
- login-proxy registry, Cred, chain resolution, built-in su proxy
- EventTable — kind="table" tabs render log-event rows
- log-event data layer — store slice, SSE dispatch, /api/data hydration
- TabSpec kind/columns — table tabs on the /api/meta wire
- RegexLogEventParser — named groups become table columns
- log-event persistence and wire — DB table, batched SSE, /api/data
- CsvMetricParser + timestamp high-water mark
- parse_tick contract — timed samples, log events, collector cutover
- UptimeParser — executed custom-parser template + scoping integration test
- regex host patterns for register_host_parsers (fullmatch, loud ambiguity)
- enterprise net/fs OID contract + named SNMP bundles (otto-core/net/fs)
- SNMP counter->rate + meta_of descriptors (process_snmp_values replaces points_from_values)
- parser-health warnings (edge-triggered failures + never-produced backstop)
- Swap series rides free -b in MemParser
- ProcCountParser — runnable/blocked/total process counts
- PerCoreCpuParser — busy%% per core from /proc/stat deltas
- DiskIoParser — per-device B/s from /proc/diskstats deltas
- SocketsParser — established/time-wait counts from ss -s
- NetDevParser — per-interface rx/tx rates on a Network tab
- shared counter->rate helpers + ParseContext.ts
- otto cov report --prefix strips a display root from report paths
- report sorter served from the vite-built covreport bundle
- TypeScript port of the coverage-report sorter with Vitest pins
- clang gcov support — stamp-based discovery + llvm-cov capture
- React monitor dashboard replaces the vanilla-JS frontend
- cov get defaults to the standard per-invocation output dir
- persist per-file excluded_lines in store.json
- expand ${sut_dir} in tier harvest_dirs on read
- tier-colored rendering, legend, state rows, provenance table
- reporter consumes captures, manual store, unit harvest; e2e pin guard
- otto cov clean — zero remote .gcda counters
- otto cov get — single retrieval command; test --cov emits captures
- per-board capture.json production from fetched counters
- line states + provenance in store; blob-anchored manual validity pass
- manual capture store dir + exclusion-marker scan
- Capture artifact model with dirty-tree remap and blob anchors
- bidirectional hunk remap engine
- git plumbing helpers for capture pinning
- runtime TierConfig accessor for declarative tiers
- typed [coverage] settings with declarative tiers, colors, exclusions
- warm --tests completion with pytest-collected test names
- tab completion for --lab and --tests
- MonitorServer.force_stop(); harness sheds its uvicorn reach-in
- project-level register_parsers() — extend/override defaults for all hosts
- per-parser collection intervals via per-bucket loops
- parser API v2 — parse(output, *, ctx: ParseContext); no more parser mutation
- typed /api/meta contract (ChartSpec/TabSpec/MonitorMeta) + schema export
- otto reservation is lab-free — whoami needs no lab, check loads it lazily
- otto init epilogue — next steps + idempotent re-runs
- otto init doctor mode — validate existing areas via real ingestion
- otto init prompt/flag semantics — per-area confirms, --all, area flags
- otto init — area scaffolds with inline templates
- allow _-prefixed annotation keys in hosts.json entries
- suite-less selection runs — --tests names and -m alone
- default-construct suite Options per class in multi-suite runs
- auto-register Test* OttoSuite subclasses; delete @register_suite
- incremental product builds + helpful polluted-tree error, no tracebacks
- third-party group subcommands tab-complete on the fast path
- live Typer apps without help= inherit their own Typer-native help
- unified command registry + bootstrap composition root; user-extensible top-level CLI
- unify host-verb returns into the Result family
- built-in local host + per-command output dirs; fix --help crash across groups
- CLI-subprocess e2e coverage + hostless marker + 3 surfaced fixes
- remove the repeat-command scheduler (RepeatRunner)


### Changed

- fold session handshake onto confirm_live
- give bash recover its own distinct digit marker (drop drain)
- extract history import/export; acknowledge new modules in import budget
- extract MetricStore from MetricCollector
- extract MetricDB from MetricCollector
- extract Broadcaster from MetricCollector
- remove 'from __future__ import annotations' across src (repo ban)


### Dependencies

- bump @types/node from 24.13.2 to 26.1.0 in /web
- bump plotly.js-gl2d-dist-min from 3.6.0 to 3.7.0 in /web
- bump astral-sh/setup-uv from 8.2.0 to 8.3.0
- bump typing-extensions from 4.15.0 to 4.16.0
- bump fastapi from 0.138.1 to 0.139.0
- bump asyncssh from 2.23.1 to 2.24.0
- bump ty from 0.0.55 to 0.0.56
- bump hypothesis from 6.155.7 to 6.156.1
- bump uvicorn from 0.49.0 to 0.50.0
- raise ruff floor to 0.15.20, the currently-locked version


### Documentation

- add e2e embedded REPL-product harness to the embedded-recovery follow-up
- tee up fresh-worktree web-dist build gap (make coverage Error 127)
- queue embedded-recovery tests; mark echo-proof I-3 fix implemented
- design spec for Untitled UI + ECharts redesign
- frontend redesign plan
- tee up TS-tooling follow-ups
- document the web/ TypeScript quality gates
- track echo-proof recover_session follow-up + deferred AppShell review minors
- note attach() session-discard caveat after app-shell timeout
- AppShell cookbook, login-proxy extending guide, list-creds host-database guide
- update login-proxy and app shell plan
- monitor Phase 3 Plan B ship-as-noted follow-ups
- log-sourced data guide — CSV digests, syslog event tables, large files
- Plan B implementation plan — log-sourced data
- monitor Phase 3 Plan A ship-as-noted follow-ups
- Phase 3 metrics — Unix parser tables, OID contract, bundles, warnings
- edge-triggered parser-health warnings + Plan A implementation plan
- Phase 3 metrics-expansion design spec
- build-time screenshot of the coverage report in the guide
- restore browser-guard rationale dropped in the extraction
- align guide/reference/architecture with the shipped pipeline
- record deferred lcov-rc wiring for custom exclusion markers
- custom markers are render-only; provenance table is manual-only; filename pattern
- declarative tiers, otto cov get/clean, manual captures, validity semantics
- spec the collected-tests completion cache follow-up
- showcase --lab / --tests completion, document the static-scan boundary
- rename "pillars" to "first-party commands"
- API pages for the decomposed modules; ctx/interval/register_parsers guide
- note dormant orphaned-bucket risk at the gather site
- save plans and specs for command revamps
- restructure as a story — pillars, lifecycle tree, subsystems, utilities
- onboarding rewritten around otto init; underscore-key idiom documented
- selection-run syntax, discovery-scope tests key, decorator-less suites
- otto init scaffolding + pytest-native flexibility designs and plans
- accuracy sweep across all pages + new architecture tree
- selection-based otto test — listing + running by suite/marker/test
- test/suite listing rework — registry-based --list-suites + hardened collect_tests
- make @options the standard options decorator


### Fixed

- reap orphaned uvicorn transports in dashboard harness teardown
- retry npm ci in web-install only on network-class failures
- fail-fast on dead session in send/expect; collect long-dead session tests
- harden login-proxy resync against the tty-flush window
- give monitor scoping e2e an adequate per-tick timeout budget
- auto-build web dist for coverage/dashboard on fresh checkouts
- close listening sockets before aborting in force_stop
- npm-ci web/ deps at the start of release/all/ci
- isolate the SUITES registry per suite test; guard it in CI
- prime the SSE stream so Firefox reaches "live" at once
- mark session for recovery when app-shell launch times out
- parse type-conversion failures return a failed ShellResult; reject non-Parsed list fields
- line-anchor login-proxy resync marker so it is sound on echo-on bridges
- resync shell after login-proxy transitions to survive su/sudo tty flush
- consume matched bytes in _BridgeProxyIO.expect so multi-hop replay waits per hop
- symmetric full via-cred lookup at session-establishment proxy
- wrap all login-proxy failures in LoginProxyError
- retry west update to survive transient module-fetch resets
- provisional-tail guard — confirm the final line across reads
- final-review fixes — log-only hosts, torn-line guard, year rollover
- final-gate fixes — ruff format drift, ty-narrowed table columns
- export/json key-set pin missed log_events
- inject year before strptime for year-less timestamp formats
- wire up otto.monitor.rates API page; fix two docstring gates
- explicit params in net-descriptor helper (ty gate)
- failed ticks still parse and record points; only health bookkeeping is success-gated
- silent-parser backstop counts only succeeding ticks; no literal None in failure msg
- scope `otto host <TAB>` to the selected lab
- dashboard dist-guard matcher mirrors the browser tests' full marker set
- hermetic OTTO_* env — ambient sut_dirs broke the bootstrap test
- anchor blobs cwd-relative — nested sut_dir produced empty captures
- otto test --cov-report renders via the collection model
- scope `get --clean` to unix hosts; robust report errors
- remap dirty-tree e2e captures HEAD→worktree at report time
- resolve relative harvest_dirs against the repo root
- clean one-line errors for all new failure modes
- exclusion display is render-time; thread extra_markers reporter→renderer
- cov clean scopes its sweep to the computed unix hosts
- test --cov capture tail never fails the run; cover the tail with tests
- preserve never-reached branch state through capture format
- position-aware START/STOP handling in exclusion scan
- skip unanchored sources (no HEAD blob) in build_capture
- gitio raises on non-repo paths; cat_blob routed through _run
- user-facing help for the otto test group
- concurrent tick cadence restored; project parsers reach historical catalog; pin gaps closed
- historical collectors declare the parser catalog — --file mode renders again
- release the flock fd when MetricDB.open() fails mid-way
- add dashboard browser coverage; playwright-proof pytester inner runs; provision chromium libs
- selection runs skip non-matching repos, per-repo --results, loud --tests+suite conflict
- otto init lab doctor rejects non-object hosts.json entries cleanly
- otto init epilogue honors comma-or-pathsep OTTO_SUT_DIRS convention
- otto init derives area names from existing settings.toml, not the dir name
- narrow cls before use in suite_options; docstring + comment polish
- cut conftest loading at the repo root, not the suite dir
- backport Zephyr fs-shell mount-leak fix for 2.7 and 3.7 beds
- probe mount state with statvfs instead of leaking Zephyr re-mounts
- park repo1 suites by source FILE, not origin prefix
- built-in local host is not fleet — all_hosts/do_for_all_hosts exclude it
- close the sys.modules isolation gap; fix embedded-cov e2e Result drift
- --show-lab/--list-hosts fail loud on contained bootstrap errors
- completion resolves real commands by dispatch target only, not COMP_WORDS sniffing
- contain phase-1 config-data errors; otto --help survives malformed settings.toml
- list otto-test suites from the registry; harden collect_tests; add --list-tests/--list-markers


### Maintenance

- remove login proxy and app shell as todo items
- add frontend component framework update to todo list
- update vagrant config to install MySQL on unix hosts
- tweaked ideas around application shells
- added more future ideas to todo list
- save plans for future work
- added future work items
- mark termynal docs item done
- prune stale files; re-verify fable ranked list against main
- update todo list
- hygiene batch from the registry-unification final review
- update Claude model names
- updated review by fable


## [0.5.4] - 2026-06-30

### Added

- three-sink logging with per-command LogMode + library capture


### Changed

- finish LogMode API — pure LogMode + session.log transcript
- make `import otto` import-light via PEP 562 lazy exports (Part D)
- trim otto startup imports + add deterministic import-budget guard
- clear PGH003 + empty the ratchet (strict-linting phase S)
- docstring formatting + deny D105/D107 (strict-linting phase D-1)
- annotate src/scripts (strict-linting phase A)
- clear cleanup-straggler ratchet debt (strict-linting phase 4b)
- clear naming & bug-class ratchet debt (strict-linting phase 4)
- clear UP/RUF/PERF ratchet debt (strict-linting phase 3)


### Documentation

- docstrings for app layer + clear D ratchet (strict-linting phase D-4)
- docstrings for the data/config layer (strict-linting phase D-3)
- docstrings for the public Host API (strict-linting phase D-2)
- add more linting plans
- linting design
- three-sink logging design spec
- add plans for stricter linting and formatting


### Fixed

- gate on non-stdlib module count, not raw sys.modules (#88)
- define get_completion_names before apply_repo_settings (circular import)
- adopt bugbear/comprehension/simplify — B,C4,SIM,PIE (Phase 2)


### Maintenance

- update todo items
- clean up ruff ignore list
- update todo list items
- adopt ruff format @100 + strict select=ALL ratchet + wire gate (Phase 0+1)


## [0.5.3] - 2026-06-28

### Added

- kernel-module load/unload/lsmod + per-class CLI parsers
- per-session current_user tracking & elevation (Spec A)
- NFS-readiness — monitor DB journal adapt + time-boxed log rotation


### Changed

- make 'otto' a plain logging.Logger


### Dependencies

- bump telnetlib3 from 4.0.4 to 4.0.5
- bump bump-my-version from 1.3.0 to 1.4.1
- bump actions/checkout from 6.0.3 to 7.0.0
- bump pydantic-settings from 2.14.0 to 2.14.2
- bump pydantic from 2.12.5 to 2.13.4
- bump ty from 0.0.54 to 0.0.55
- bump starlette from 0.52.1 to 1.3.1
- bump typer from 0.26.7 to 0.26.8
- bump hypothesis from 6.155.2 to 6.155.7
- bump sse-starlette from 3.4.4 to 3.4.5
- bump fastapi from 0.136.3 to 0.138.1


### Documentation

- save forgotten plans and todo files
- drop manual-publish make targets; scrub publishing docs
- drop stale version string from docs title
- implementation plan for standard-logging refactor
- spec for standard-logging refactor (subclass removal + output_dir→OttoContext); split out import-light __init__


### Fixed

- migrate repo1 sample instructions off logger.output_dir
- tolerate child exiting mid-scan in LocalSession recovery


## [0.5.2] - 2026-06-26

### Added

- backend conformance suite + sample reference backends
- make the host source a registered, pluggable backend
- modernize interface — multi-holder who_reserved, named-registry backends, -R break-glass, cached --as-user completion


### Dependencies

- bump pytest to 9.1.1 and fix show_test_item() call
- bump ruff from 0.15.17 to 0.15.20
- bump ty from 0.0.49 to 0.0.54


### Documentation

- host-database guide, reservations upgrade, team-setup onboarding
- enable Sphinx nitpicky with zero ignores


## [0.5.1] - 2026-06-25

### Added

- converge run/put/get/login onto @cli_exposed


### Changed

- speed up the suite — host-pool lease, front-load, docker pooling
- restructure into unit/integration/e2e tiers + dedup (fable #5)
- skip host build during otto-host verb completion


### Documentation

- harden doctest coverage — execute examples, add gates, drop the +SKIP charade
- test-suite speedup design + plan + baseline (fable follow-up)
- spec + plan for test-suite restructure & dedup (fable #5)


### Fixed

- put venv on PATH for release-flow tools in Makefile
- re-group docker-e2e CLI tests to avoid coverage schema-init race


### Maintenance

- docker on all three Unix test VMs + DRY their definitions


## [0.5.0] - 2026-06-22

### Added

- auto-expose host methods as class-scoped `otto host` subcommands
- converge product defaulting into one [host_preferences] block
- switch term/transfer via the override seam; drop in-place setters
- term families + product host_preferences capability resolution
- lab-declared term/transfer menus with resolved active selection
- register products via code providers at host ingest
- add posix remote file operations (PosixFileOps mixin) + embedded subset
- add power control, reboot/shutdown & reachability waits to hosts
- add privilege elevation (sudo, su, as_user) to hosts
- add dependency-injected product lifecycle to hosts


### Changed

- defer asyncssh/aioftp/telnetlib3 to point-of-use
- drop products from HostSpec — it's repo-logic, not lab data


### Dependencies

- declare starlette as an explicit dependency
- bump uvicorn from 0.48.0 to 0.49.0


### Documentation

- restructure otto host guide into a nested Hosts group; refresh currency
- list starlette in the dependency table
- added minor cleanup tasks
- capability-resolution spec + host-preferences end-state backlog
- add product-providers design spec


### Fixed

- render element_id as its number in host id/name


## [0.4.5] - 2026-06-19

### Added

- @options decorator + first-class pydantic Options validation
- support Typer 0.26 — re-home runner options via public Context injection


### Changed

- split transfer.py into a per-backend transfer/ package
- collapse pure-data forward types into frozen pydantic dataclasses


### Fixed

- qemu-restart selects embedded guests by creds shape, not os_type


## [0.4.4] - 2026-06-18

### Added

- registry public API for term/transfer backends (WS#4)


## [0.4.3] - 2026-06-17

### Added

- otto schema export — JSON Schema for hosts.json / settings.toml / reservations
- pydantic monitor records — MetricPoint + import/export rows + frozen SnmpMetric
- pydantic settings boundary — SettingsModel + docker/os-profile/reservation specs + OttoEnvSettings (Phase A plan 3)
- integrate host specs — registry carries spec, factory collapse, command_frame, interfaces (Phase A 2b)
- host spec models (HostSpec/Unix/EmbeddedHostSpec) — Phase A plan 2a
- pydantic boundary layer + option two-type split (Phase A, plan 1)


### Changed

- snake_case sweep — element vocab, os_* fields, host filenames, API


### Documentation

- list pydantic + pydantic-settings in getting-started dep table


### Fixed

- extend RST title underlines broken by the WS#2 rename


### Maintenance

- record plan progress
- registry factory integration
- save some plans
- saving claude's plans


## [0.4.2] - 2026-06-14

### Added

- explicit OttoContext runtime + deterministic host lifecycle


### Dependencies

- bump ruff from 0.15.16 to 0.15.17
- bump ty from 0.0.45 to 0.0.49
- bump asyncssh from 2.23.0 to 2.23.1


### Documentation

- add library-usage to user-guide toctree


### Fixed

- stop nightly stability flake — uncharge the 2s retry backoff from tight test budgets


### Maintenance

- save specs and designs


## [0.4.1] - 2026-06-13

### Added

- add embedded load()/unload() binary-load API + BinaryLoader strategy
- add per-command log=False to hide noisy commands from logs
- log buffered-frame output via parse_output, not the raw stream


### Documentation

- Review feedback of otto's design


### Fixed

- satisfy ty — cast resolved ShellCommand.log to bool


## [0.4.0] - 2026-06-12

### Added

- OS-agnostic EmbeddedHost + host-class registry (ZephyrHost)
- Zephyr embedded-host support + test-suite stability hardening (#52)


### Changed

- drop local loop-cleanup guards now covered by the reaper


### Dependencies

- bump ty from 0.0.39 to 0.0.45
- bump ruff from 0.15.14 to 0.15.16
- bump astral-sh/setup-uv from 8.1.0 to 8.2.0
- bump fastapi from 0.136.1 to 0.136.3
- bump hypothesis from 6.152.9 to 6.155.2
- bump actions/checkout from 6.0.2 to 6.0.3
- bump uvicorn from 0.47.0 to 0.48.0
- bump telnetlib3 from 4.0.3 to 4.0.4 (#44)


### Documentation

- repoint orphaned :doc: ref after relocating the FS how-to
- relocate embedded-extension how-tos into the guide; drop stale API pages
- add embedded/lab-config/os-profile guides
- plan generic EmbeddedHost + host-class registry
- group `make help` with nox/coverage/stability-{unit,unix,embedded} shorthand


### Fixed

- route host probes by credential shape, not osType literal
- poll for the container id after a successful compose up
- retry compose up once past the libnetwork "network not found" race
- force LLEXT link tail to track the recompiled object
- satisfy ty 0.0.45's new lint rules
- never create an event loop in RemoteHost.__del__
- make --xdir optional again, defaulting to CWD
- don't let the loop reaper close wider-scoped pytest-asyncio loops
- reap orphaned pytest-asyncio loops to kill misattributed CI flake


### Maintenance

- ignore embedded-gcov worktree churn


## [0.3.6] - 2026-05-24

### Dependencies

- bump ty from 0.0.37 to 0.0.39 (#34)
- bump telnetlib3 from 4.0.2 to 4.0.3 (#35)
- bump ruff from 0.15.13 to 0.15.14 (#36)
- bump hypothesis from 6.152.7 to 6.152.9 (#33)
- bump peter-evans/create-issue-from-file from 5 to 6 (#32)


### Fixed

- close inner pytest.main()'s leaked event loop in collectTests
- require --xdir and harden removeOldLogs against foreign trees


## [0.3.5] - 2026-05-19

### Added

- speed up telnet connect by removing the login drain


### Changed

- retire the dedicated nc monitor session


### Documentation

- add release runbook and PyPI install instructions


### Fixed

- stop close() from running a process-wide gc.collect()
- reap orphaned nc listener on cancelled transfer


## [0.3.4] - 2026-05-17

### Added

- auto-start container stack on access when not running
- exclude Docker containers from the fleet host generator
- print output dir on exit; clean logs only on subcommands


### Dependencies

- bump ruff from 0.15.12 to 0.15.13 (#31)
- bump uvicorn from 0.46.0 to 0.47.0 (#26)
- bump hypothesis from 6.152.4 to 6.152.7 (#27)
- bump nox-uv from 0.7.1 to 0.8.0 (#28)
- bump sse-starlette from 3.4.2 to 3.4.4 (#30)
- bump ty from 0.0.34 to 0.0.37 (#29)


### Documentation

- true up CLI/API docs and add a dependency-table sync check


### Fixed

- serialize nc-get size prefetch on the monitor session
- connect oneshot pool sessions concurrently, not serially
- raise on undefined hop host ID
- render console output at the true terminal width


## [0.3.3] - 2026-05-15

### Added

- bound nc listener wait to prevent infinite transfer hangs


### Fixed

- serialize ConnectionManager lazy-init to stop transport leaks
- clean up half-built telnet connections on cancellation
- recheck cached TelnetClient liveness in ConnectionManager
- serialize FTP transfers on the shared aioftp client
- recover ShellSession after external cancellation
- serialize SessionManager get-or-create paths


### Maintenance

- add migration plan
- Updated todo list
- Cleanup up Makefile help strings


## [0.3.1] - 2026-05-09

### Added

- Added docker container library and CLI support


### Dependencies

- bump sse-starlette from 3.4.1 to 3.4.2 (#25)
- bump asyncssh from 2.22.0 to 2.23.0 (#24)


### Documentation

- add python version support badges
- clarified that the `asyncio.run()` method is being used to call `Host.run()`
- added docker documentation to the doc tree


### Fixed

- filter repos by default_host, not `--on`, for lab applicability
- fixed some type annotation errors


## [0.2.1] - 2026-05-05

### Added

- nox matrix on Python 3.10-3.14, OIDC release workflows (#12)
- add event monitoring to test suites
- add iteration banners
- add suite-level monitoring
- Added repo-wide and per-invocation host options


### Dependencies

- bump sse-starlette from 3.3.3 to 3.4.1 (#18)
- update uv-build requirement from <0.11.0 to <0.12.0 (#17)
- bump ruff from 0.14.7 to 0.15.12 (#16)
- bump ty from 0.0.31 to 0.0.34 (#15)
- bump actions/upload-artifact from 4 to 7 (#14)
- bump actions/download-artifact from 4 to 8 (#13)
- bump fastapi from 0.135.2 to 0.136.1 (#11)
- bump telnetlib3 from 4.0.1 to 4.0.2 (#8)
- bump uvicorn from 0.42.0 to 0.46.0 (#7)
- bump pytest-cov from 7.0.0 to 7.1.0 (#5)
- bump typer from 0.24.1 to 0.25.1 (#19)
- bump rich from 14.3.3 to 15.0.0 (#9)
- bump py-spy from 0.4.1 to 0.4.2 (#6)
- bump tomli from 2.4.0 to 2.4.1 (#4)
- bump pytest from 9.0.1 to 9.0.3 (#3)


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

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/v0.7.4...HEAD
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

