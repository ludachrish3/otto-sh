.DEFAULT_GOAL := all

.PHONY: help all ci nox nox-all validate clean-dist dev build test coverage coverage-unit docs docs-html doctest typecheck clean changelog release publish-test publish

# Bump component for `make release`. Override on the command line:
#   make release BUMP=minor
BUMP ?= patch

# Coverage target invoked by `validate`. `ci` overrides this to
# `coverage-unit` because GitHub Actions doesn't have the Vagrant VMs
# that integration/hops tests require.
COVERAGE_TARGET ?= coverage

COVERAGE_THRESHOLD := 85
# CI runs unit tests only (integration/hops markers need Vagrant VMs that
# don't exist in GitHub Actions), so the achievable threshold is lower.
CI_COVERAGE_THRESHOLD := 80

# Hard ceiling on the pytest invocation so a hung test (e.g. an integration
# test waiting on an unreachable VM) can't stall the pipeline indefinitely.
# Docker integration tests are pinned to one xdist worker (xdist_group)
# because they share /tmp/otto-docker/repo1/ on the parent and can't safely
# parallelize compose_up's `rm -rf` of the staging dir. That serialization
# is what dominates wall time; 4 min leaves headroom for slower runners.
# --kill-after escalates SIGTERM → SIGKILL if xdist workers don't drain.
PYTEST_TIMEOUT := 240s
TIMEOUT_CMD := timeout --foreground --kill-after=10s $(PYTEST_TIMEOUT)

all: ## Run full pipeline against the dev VM (includes integration tests)
	@$(MAKE) validate \
		&& $(MAKE) build

ci: ## Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) validate COVERAGE_TARGET=coverage-unit \
		&& $(MAKE) build

changelog: ## Regenerate CHANGELOG.md from conventional commit history (Unreleased only — does not touch released sections)
	git-cliff -o CHANGELOG.md

release: ## Validate (typecheck + docs + FULL nox matrix across all Pythons, requires dev VM), regenerate changelog at the new version, bump version, then build dist (BUMP=patch|minor|major, default patch; or NEW_VERSION=X.Y.Z[rcN] for prereleases)
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) docs \
		&& $(MAKE) nox-all \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
		&& echo "Targeting v$$NEW_VERSION" \
		&& git-cliff --tag "v$$NEW_VERSION" -o CHANGELOG.md \
		&& git add CHANGELOG.md \
		&& bump-my-version bump --verbose --allow-dirty --new-version "$$NEW_VERSION" $(BUMP) \
		&& $(MAKE) build \
		&& echo \
		&& echo "Regenerated CHANGELOG.md, bumped version, tagged, and built dist/." \
		&& echo "Pushing the tag fires .github/workflows/release.yml, which" \
		&& echo "publishes to PyPI via OIDC (gated by the 'pypi' environment)." \
		&& echo "Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo \
		&& echo "Manual fallbacks (require UV_PUBLISH_TOKEN in env):" \
		&& echo "    make publish-test    # upload dist/ to TestPyPI" \
		&& echo "    make publish         # upload dist/ to PyPI"

publish-test: ## Manual fallback: upload dist/ to TestPyPI (prefer dispatching release-testpypi.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--publish-url https://test.pypi.org/legacy/ \
		--check-url   https://test.pypi.org/simple/

publish: ## Manual fallback: upload dist/ to PyPI — permanent (prefer pushing a v* tag to fire release.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--check-url https://pypi.org/simple/

nox: ## Run the default nox session matrix (unit tests across all supported Pythons + typecheck + docs)
	uv run nox

nox-all: ## Run the FULL test suite (unit + integration + hops) across all supported Pythons. Requires dev VM with Vagrant hosts up. Not used by CI.
	uv run nox -s tests_all

validate: ## Run validation (clean-dist, typecheck, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

clean-dist:
	@rm -rf dist

dev:
	uv sync
	git config core.hooksPath .githooks
	@echo "Dev environment ready"

build: ## Build the project with uv
	uv build

test: ## Run tests (use TESTS= to filter)
	uv run pytest -k '$(TESTS)'

coverage: ## Run tests and enforce coverage threshold
	$(TIMEOUT_CMD) uv run pytest --cov-fail-under=$(COVERAGE_THRESHOLD)

coverage-unit: ## Run unit tests only (no Vagrant VMs needed) and enforce CI threshold
	$(TIMEOUT_CMD) uv run pytest tests/unit -m "not integration and not hops" --cov-fail-under=$(CI_COVERAGE_THRESHOLD)

typecheck: ## Run ty type checker (advisory during trial; not wired into `all`)
	uv run ty check

SPHINX_SRCS :=  docs/conf.py                        \
                $(shell find docs -name '*.rst')    \
                $(shell find docs -name '*.md')    \
                $(shell find src/otto -name '*.py') \

docs: docs-html doctest ## Build HTML docs and run doctests

docs-html: docs/_build/html/index.html ## Build HTML docs only (warnings are errors)

docs/_build/html/index.html: $(SPHINX_SRCS)
	uv run sphinx-build -W -b html docs/ docs/_build/html

doctest: ## Run Sphinx doctests
	uv run sphinx-build -b doctest docs/ docs/_build/doctest

clean: ## Remove all generated artifacts
	@rm -rf dist
	@rm -rf coverage_report .coverage .coverage.*
	@rm -rf docs/_build

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
