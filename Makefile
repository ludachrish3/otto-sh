.DEFAULT_GOAL := all

.PHONY: help all ci validate clean-dist dev build test coverage coverage-unit docs docs-html doctest typecheck clean release publish-test

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
# Full suite runs in ~40s locally; 2 min leaves headroom for slower runners.
# --kill-after escalates SIGTERM → SIGKILL if xdist workers don't drain.
PYTEST_TIMEOUT := 120s
TIMEOUT_CMD := timeout --foreground --kill-after=10s $(PYTEST_TIMEOUT)

all: ## Run full pipeline against the dev VM (includes integration tests)
	@$(MAKE) validate \
		&& $(MAKE) build

ci: ## Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) validate COVERAGE_TARGET=coverage-unit \
		&& $(MAKE) build

release: ## Validate, bump version, then build dist at the new version (BUMP=patch|minor|major, default patch)
	@$(MAKE) validate \
		&& bump-my-version bump --verbose $(BUMP) \
		&& $(MAKE) build \
		&& echo \
		&& echo "Bumped version, tagged, and built dist/ at the new version. Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo "Publish to TestPyPI with:" \
		&& echo "    make publish-test    (requires UV_PUBLISH_TOKEN in env)"

publish-test: ## Upload dist/ to TestPyPI (requires UV_PUBLISH_TOKEN in env)
	uv publish \
		--publish-url https://test.pypi.org/legacy/ \
		--check-url   https://test.pypi.org/simple/

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
	@rm -rf coverage_report .coverage
	@rm -rf docs/_build

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
