# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-05-05

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

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ludachrish3/otto-sh/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ludachrish3/otto-sh/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/ludachrish3/otto-sh/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/ludachrish3/otto-sh/releases/tag/v0.0.1

