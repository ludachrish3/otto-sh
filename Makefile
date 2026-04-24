.DEFAULT_GOAL := all

.PHONY: help all clean-dist build test coverage docs docs-html doctest typecheck clean

COVERAGE_THRESHOLD := 85

all: ## Run full CI pipeline (stops at the first failing step)
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) coverage \
		&& $(MAKE) docs \
		&& $(MAKE) build

clean-dist:
	@rm -rf dist

build: ## Build the project with uv
	uv build

test: ## Run tests (use TESTS= to filter)
	uv run pytest -k '$(TESTS)'

coverage: ## Run tests and enforce coverage threshold
	uv run pytest --cov-fail-under=$(COVERAGE_THRESHOLD)

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
