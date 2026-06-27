# Import-light / lazy `otto/__init__.py` (separate workstream)

**Split out from** the logger-standardization refactor
(`docs/superpowers/specs/2026-06-27-logger-standardization-design.md`) on
2026-06-27 — kept separate because it carries registration-ordering risk and is
**not** required for any correctness fix. Sequence it **after** the logger
refactor lands.

## Goal

Make `import otto` side-effect-free / import-light. Today `src/otto/__init__.py`
eagerly imports `otto.cli` (the whole Typer app), `otto.configmodule`, and
`otto.context`, and (pre-refactor) eagerly created the logger singleton. That
makes a bare `import otto` drag in most of the framework — a startup-cost smell
the fable review flagged, and what made coverage's early `find_spec('otto')`
import cascade so far.

## Approach (sketch)

- Convert `otto/__init__.py` to PEP 562 lazy exports via module `__getattr__` +
  `__all__`: `app`, `options`, `get_otto_logger`, `all_hosts`, `get_host`,
  `get_lab`, `run_on_all_hosts`, `OttoContext`, `get_context`, `open_context`,
  `try_get_context`. Each resolves its submodule on first attribute access.
- Entry point `otto:app` and `from otto import app/options` keep working
  (attribute access triggers `__getattr__`).

## The risk to design around

The eager imports may currently guarantee **registration ordering** — host-class
/ backend / os-profile registries that self-register at import. Deferring the
imports could mean a registry is empty when something queries it. The plan must:

- enumerate what registers at import of `cli`/`configmodule`/`context`;
- verify both entry paths still populate registries: the CLI (`otto --help`, a
  real command) and library use (`from otto import all_hosts`, then use it);
- add a guard/smoke test for each.

## Verification

Full gate (`make coverage`, `make typecheck`, `make docs`) + the registration
smokes above + `import otto` no longer pulls in `otto.cli`/`otto.configmodule`
(assert via `sys.modules` after a bare `import otto`).
