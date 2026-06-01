# repo3 LLEXT sample product (skeleton — not built)

The embedded analogue of repo1's `math_ops` (add/sub/mul/div/clamp), to be built
as a Zephyr **LLEXT extension** rather than a host binary:

- `add_llext_target` + `llext_compile_options(-fprofile-arcs -ftest-coverage)`
- the vendored `../third_party/embedded-gcov` runtime compiled *into* the extension
- exported entry points invoked via `llext call_fn`: one per operation to exercise
  code paths, plus `cov_dump`, which walks the extension's own gcov info and prints
  the `.gcda` content as hex over the console (no filesystem required).

`.gcno` land in the extension's build dir — that path becomes the report step's
`source_root`.

**Lifecycle mirrors Unix:** install (`llext load_hex`) -> exercise (`call_fn <op>`)
-> collect (`call_fn cov_dump`) -> uninstall (`llext unload`).

**Status:** intentionally empty. Building this requires the Zephyr SDK / west and
is sequenced *after* the LLEXT feasibility gate (lab-bound). See
[`../../../todo/embedded_coverage.md`](../../../todo/embedded_coverage.md).
