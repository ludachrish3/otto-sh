# `--doctest-modules` raises `ImportPathMismatchError` on two bare test-dir args

Pre-existing, unrelated to #175 (reproduces against unmodified `1cd9a128`). Passing
two `__init__.py`-less top-level dirs as separate CLI args (e.g.
`pytest tests/unit/cov tests/unit/monitor`) makes `--doctest-modules`'s second,
non-deconflicted import pass collide on both dirs' bare `conftest` module name.
Not a real-gate risk: `noxfile.py` passes `tests/unit` as one un-split arg. See
`.superpowers/sdd/2026-07-26-webassets-175/task-2-report.md` deviation 2 for the
full root-cause trace; fix options there if ever picked up.
