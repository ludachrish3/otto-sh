.DEFAULT_GOAL := all

.PHONY: help all ci clean-dist dev build test coverage coverage-unit docs docs-html doctest typecheck clean

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
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) coverage \
		&& $(MAKE) docs \
		&& $(MAKE) build

ci: ## Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) coverage-unit \
		&& $(MAKE) docs \
		&& $(MAKE) build

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
