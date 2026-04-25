# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MIT license and PEP 639 metadata in `pyproject.toml`.
- GitHub Actions CI workflow running `make ci` on every push and pull request.
- `make ci` and `make coverage-unit` Makefile targets that run unit-only
  tests (skipping `integration`/`hops` markers) so the pipeline works
  without Vagrant VMs.
- Dependabot configuration for `uv` and `github-actions` ecosystems.
- Root-level `CONTRIBUTING.md` pointing at the detailed contributor guide.
- Read the Docs config (`.readthedocs.yaml`) building Sphinx docs via uv.

[Unreleased]: https://github.com/ludachrish3/otto-sh/compare/HEAD...HEAD
