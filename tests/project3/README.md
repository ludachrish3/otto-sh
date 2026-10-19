# project3 — Embedded (Zephyr) code coverage bed

**Status: SKELETON / prep only.** This directory is the scaffold for the embedded
coverage work designed in [`../../todo/embedded_coverage.md`](../../todo/embedded_coverage.md).
It was created as lab-safe prep while the lab was otherwise occupied; nothing here
is wired up or runnable yet.

project3 is deliberately separate from project1 (Unix coverage) and project2 (multi-project +
Docker fixture): it isolates the heavy Zephyr SDK/west toolchain and mirrors the
real world where embedded products are separate codebases.

## What exists (this commit)

- `.otto/settings.toml` — project config modeled on project1 (labs/libs/tests/init,
  zephyr `os_profiles`, placeholder `[coverage]`).
- `pylib/project3_instructions/` — empty init module (install/uninstall to come).
- `product/` — placeholder for the LLEXT sample product (no C sources yet).
- `third_party/embedded-gcov/` — vendored NASA embedded-gcov (git submodule).
- `tests/test_embedded_coverage.py` — placeholder suite docstring (no tests).

## Deliberately NOT here yet

- The host-side **console-dump decoder** — held until it can be validated against
  a real captured `cov_dump` console stream.
- The **`EmbeddedGcdaCollector`** (`src/otto/coverage/fetcher/embedded.py`) and the
  `remote.py` routing change (replacing the current `EmbeddedHost` skip).
- The **LLEXT product** C sources / CMake (`add_llext_target` + coverage flags).
- The live **`qemu_cortex_m3`** coverage instance + the LLEXT feasibility gate
  (all lab-bound).

See the plan's *"The three pieces to build"* and *"Verification"* sections.
