# Contributing

## Development environment

Vagrant can be used to develop and test changes to `otto`. After
[installing Vagrant](https://developer.hashicorp.com/vagrant/install), run:

```bash
vagrant up            # start the dev VM and three test VMs
vagrant ssh           # connect to the `dev` VM (the default)
```

The `dev` VM is privately networked with three test VMs (`test`, `test2`,
`test3`). Develop and test from the `dev` VM — unit, integration, and
end-to-end tests assume connectivity to the test VMs.

## Development setup

Otto uses [uv](https://docs.astral.sh/uv/) for dependency management. Once
the repo is cloned in the dev VM:

```bash
make dev              # runs `uv sync` and sets up git hooks
source project_env    # optional: sets up usage with test repos
uv run pytest         # run the test suite
```

`make dev` places `otto` at `otto-sh/.venv/bin/otto`.

## Running tests

```bash
make test                     # run all tests
make test TESTS=test_host     # filter by keyword
make coverage                 # run tests and enforce coverage threshold
```

## Documentation

### Building docs

```bash
make docs          # HTML output + doctests
make doctest       # run doctests only (from .md/.rst files)
make docs-html     # HTML only (warnings are errors)
```

pytest also runs doctests from Python source files automatically via
`--doctest-modules`.

### Documentation layout

```text
docs/
├── overview.md          # Project overview
├── getting-started.md   # Installation and first steps
├── guide/               # Narrative user guides (Markdown)
├── cookbook/             # Recipes with doctest examples (Markdown)
├── contributing.md      # This page
└── api/                 # API reference (reStructuredText, auto-generated)
```

Narrative guides go in `guide/` or as top-level Markdown files.  API
reference pages live in `api/` and use `.. automodule::` directives to
pull documentation from docstrings.

### Docstring rules of thumb

- **New public function or class?** Add a Google-style docstring with a
  one-line summary, Args/Returns sections, and a `>>>` example if the
  function is pure and deterministic.
- **Changed a function's signature or behavior?** Update its docstring and
  any `>>>` examples to match.  Stale doctests will fail in CI.
- **Async, I/O, or nondeterministic code?** Write a docstring with `>>>`
  examples.  Also test these in `tests/unit/`.
- **Keep doctests minimal.** 2-4 lines showing the happy path is enough.
  Edge cases belong in unit tests.

### Doctest quick reference

In Python source files (collected by pytest):

```python
def add(a: int, b: int) -> int:
    """Add two numbers.

    >>> add(1, 2)
    3
    """
    return a + b
```

In Markdown documentation files (collected by Sphinx):

````markdown
```{doctest}
>>> from otto.utils import Status
>>> Status.Success
<Status.Success: 0>
```
````

Common imports (`Status`, `CommandStatus`, `LocalHost`) are pre-loaded in
doc-file doctests via `doctest_global_setup` in `docs/conf.py`.

## Coverage reports

### From pytest

```bash
make coverage
```

### Manually

```bash
uv run coverage run --source=otto --context=manual -m otto <subcommand> [args]
uv run coverage html -d coverage_report
```

## Type checking

`ty` (from astral) is being trialled as a replacement for pyright. The
project keeps a `[tool.pyright]` block for Pylance/VS Code, while
`[tool.ty]` drives the CLI checker and the optional ty language server.

```bash
make typecheck     # run ty check against src/ with all rules at error
```

Config lives under `[tool.ty.*]` in `pyproject.toml`. ty is pinned to an
exact version (`ty==0.0.31`) because its 0.0.x releases allow breaking
diagnostic changes between any two versions — floating the pin would cause
unannounced CI churn.

Work the count down with per-line `# ty: ignore[rule-name]` suppressions
(justified in the surrounding context) or by fixing the underlying type.
Do not silence rules globally in `[tool.ty.rules]` — an individual
demotion there needs to be defensible in review.

Use `uv run ty explain rule <name>` for the full rationale and examples
behind any diagnostic.

For VS Code: install the "Astral ty" extension (Ctrl+Shift+P → Extensions
→ search "ty"). It reads `[tool.ty]` from `pyproject.toml`, so LSP
diagnostics and `make typecheck` stay in sync.

## Performance reports

```bash
uv run pyinstrument -o profile.txt -m otto <subcommand> [args]
```

## AI-Assisted Contributions

AI coding tools (e.g., GitHub Copilot, Claude, Cursor) are permitted for
contributions to otto. If your PR contains AI-assisted code, please note it
in the PR description. Regardless of how code was generated, contributors
are responsible for understanding, testing, and owning what they submit.
