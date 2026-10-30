# Coverage toolchain

The `toolchain` object points to the cross-toolchain binaries used by the
coverage pipeline.  See {doc}`../../cli/cov/index`.

| Field | Type | Description |
|-------|------|-------------|
| `toolchain` | object | The cross-toolchain block.  Omit it to leave the host on the gcov its data names (the system `gcov`, `gcov-<major>` or `llvm-cov`; see {ref}`coverage-gcov-resolution`) and the system `lcov`. |
| `toolchain.sysroot` | string | Path to the cross-toolchain sysroot. |
| `toolchain.gcov` | string | Path to `gcov` relative to `sysroot`, or an absolute path. |
| `toolchain.lcov` | string | Path to the `lcov` binary. |
| `toolchain.lcov_args` | array of strings | Extra arguments for the `lcov` capture of this host's coverage data, one argument per entry, e.g. `["--base-directory", "/usr/src/linux"]` to resolve a clang-built kernel module's header records.  otto always passes `--ignore-errors source` itself, so a source file missing on the reporting machine is never a capture error.  A clang host's settings for real branch counts and for C++ end-line errors are in {ref}`coverage-clang-branch-counts`. |
| `toolchain.tools` | array of objects | Artifacts otto *installs onto* this host (`name`, `source`, `dest`, `user`, `mode`) — the inverse of the three fields above, which otto only reads from.  See {doc}`../../cli/host/capabilities/index`. |
