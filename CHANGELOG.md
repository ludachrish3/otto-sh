# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/v0.5.2...HEAD
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

