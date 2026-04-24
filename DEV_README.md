# Developer Guide

## Developing and testing `otto`

### Environment

Vagrant can be used to develop and test changes to `otto`. After installing vagrant, you can run the following to start up all VMs (dev and test):

```sh
vagrant up
```

The `dev` VM is the default, so running `vagrant ssh` will connect to the `dev` VM. There are three test VMs, named `test`, `test2`, and `test3`, all privately networked together and with the `dev` VM. It is recommended to develop and test from the `dev` VM because all unit, integration, and end-to-end tests assume connectivity to the test VMs.

Install Vagrant here: <https://developer.hashicorp.com/vagrant/install>

###

Once the repo is cloned in the development VM, the following commands should be run to install `otto` and all dependencies in the virtual environment. They will place `otto` at `otto-sh/.venv/bin/otto`.

```sh
uv sync                 # Init virtual env and install all dependencies
uv add otto-sh --dev    # Install the local version of otto in the virtual environment
source project_env      # Optional environment that sets up usage with test repos
```

## Documentation

### Building docs

```sh
make docs          # HTML output in docs/_build/html/
make doctest       # Run doctests in .md/.rst documentation files (Sphinx)
make docs-all      # Build HTML + run doctests
make docs-lint     # Build HTML with warnings-as-errors (catches broken refs)
```

pytest also runs doctests from Python source files automatically via `--doctest-modules`.

### Rules of thumb

- **New public function or class?** Add a Google-style docstring with a one-line summary, Args/Returns sections, and a `>>>` example if the function is pure and deterministic.
- **Changed a function's signature or behavior?** Update its docstring and any `>>>` examples to match. Stale doctests will fail in CI.
- **Async, I/O, or nondeterministic code?** Write a docstring (with `>>>` examples). Also test these in `tests/unit/`.
- **Keep doctests minimal.** 2-4 lines showing the happy path is enough. Edge cases belong in unit tests.
- **Doc files live in `docs/` (Markdown) and `docs/api/` (reStructuredText).** Narrative guides go in `docs/guide/` and `docs/cookbook/`; API reference is auto-generated from docstrings via `docs/api/`.

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

Common imports (`Status`, `CommandStatus`, `LocalHost`) are pre-loaded in doc-file doctests via `doctest_global_setup` in `docs/conf.py`.

## Coverage Reports

### From pytest

```sh
make coverage
```

### Manually

```sh
uv run coverage run --source=otto --context=manual -m otto <subcommand> [args]
uv run coverage html -d coverage_report
```

## Type Checking

`ty` (from astral) is being trialled as a replacement for pyright. The project keeps a `[tool.pyright]` block for Pylance/VS Code, while `[tool.ty]` drives the CLI checker and the optional ty language server.

```sh
make typecheck     # run ty check against src/ with all rules at error
```

Config lives under `[tool.ty.*]` in `pyproject.toml`. ty is pinned to an exact version (`ty==0.0.31`) because its 0.0.x releases allow breaking diagnostic changes between any two versions — floating the pin would cause unannounced CI churn.

Work the count down with per-line `# ty: ignore[rule-name]` suppressions (justified in the surrounding context) or by fixing the underlying type. Do not silence rules globally in `[tool.ty.rules]` — an individual demotion there needs to be defensible in review.

Use `uv run ty explain rule <name>` for the full rationale and examples behind any diagnostic.

For VS Code: install the "Astral ty" extension (Ctrl+Shift+P → Extensions → search "ty"). It reads `[tool.ty]` from `pyproject.toml`, so LSP diagnostics and `make typecheck` stay in sync. Periodic evaluation of ty vs. Pylance is tracked in [todo/ty_vs_pylance_eval.md](todo/ty_vs_pylance_eval.md).

## Performance Reports

```sh
uv run pyinstrument -o profile.txt -m otto <subcommand> [args]
```
