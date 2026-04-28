# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.2] - 2026-04-27

## [0.0.1] - 2026-04-26

### Added

- MIT license and PEP 639 metadata in `pyproject.toml`.
- GitHub Actions CI workflow running `make ci` on every push and pull request.
- `make ci` and `make coverage-unit` Makefile targets that run unit-only
  tests (skipping `integration`/`hops` markers) so the pipeline works
  without Vagrant VMs.
- Dependabot configuration for `uv` and `github-actions` ecosystems.
- Root-level `CONTRIBUTING.md` pointing at the detailed contributor guide.
- Read the Docs config (`.readthedocs.yaml`) building Sphinx docs via uv.

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/ludachrish3/otto-sh/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/ludachrish3/otto-sh/compare/v0.0.0...v0.0.1
