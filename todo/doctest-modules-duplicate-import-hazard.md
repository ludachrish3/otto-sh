# `--doctest-modules` + module-singleton + monkeypatch = patch hits the wrong module copy

**Surfaced:** 2026-06-26, during the NFS-readiness workstream (a `create_output_dir`
network-FS breadcrumb test). The breadcrumb feature was dropped to avoid the
fragility; this note records the underlying hazard for a future fix.

## Symptom

A test that `monkeypatch.setattr(otto.logger.logger, 'network_fs_type', ...)` and
then drives the **`get_otto_logger()` singleton** saw its patch ignored — only
under `pytest -n auto` (xdist), not serial (`-n0`) and not the full suite. The
failure correlated with `--cov` flags, which was a **red herring** (cov only
perturbs xdist load/collection timing).

## Root cause (proven by instrumentation)

`otto.logger.logger` gets **imported as two distinct module objects** (M1 and M2)
in an xdist worker — almost certainly `--doctest-modules` (in `addopts`) importing
`src/otto/logger/logger.py` under a key distinct from the package import the tests
use. The logging-manager singleton `getLogger('otto')` ends up an instance of
**M2**'s `OttoLogger` (whichever copy's `setLoggerClass` ran first wins), while the
test patches **M1**. A method on the singleton resolves module globals from
`M2.__dict__`, so the M1 patch is invisible. Confirmed:
`type(logger) is not logger_mod.OttoLogger`, `create_output_dir.__globals__ is not
logger_mod.__dict__`, and the in-method `network_fs_type` id ≠ the patched lambda id.

## Why it matters

Any test that monkeypatches a **module global** that an **indirectly-constructed
singleton** (or any object whose class came from the other copy) reads is silently
fragile. The `get_otto_logger()` module-singleton pattern is the amplifier.

## Possible fixes (pick when addressed)

- Set pytest `--import-mode=importlib` and/or scope `--doctest-modules` so a module
  is never imported under two keys (verify no duplicate `otto.*` in `sys.modules`).
- Or stop relying on the module-level `get_otto_logger()` singleton in tests; patch
  via the actual instance / inject the dependency.
- Add a session-scoped guard test asserting no duplicate `otto.*` module objects.
