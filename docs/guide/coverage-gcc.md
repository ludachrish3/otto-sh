# GCC Products

The default coverage path: a product compiled with GNU GCC on a Unix-like
target, counters fetched over the network. Everything on the main
{doc}`coverage` page applies as-is; this page collects the GCC-specific
setup.

## Build flags

Compile *and link* the product with `--coverage` (shorthand for
`-fprofile-arcs -ftest-coverage` plus the gcov link library). Keep the build
tree around: the compiler writes one `.gcno` (notes) file per object at
build time, and the report step needs them to decode the `.gcda` counters
the product writes at run time.

```make
CFLAGS  += --coverage -O0
LDFLAGS += --coverage
```

`-O0` is not required, but optimized builds fold lines and branches
together and make the line table noticeably harder to read.

## Where counters land

Each instrumented process writes its `.gcda` files on exit, by default next
to the build's object files (the absolute path is baked in at compile time).
Point `[coverage].gcda_remote_dir` in `.otto/settings.toml` at that
directory so `otto cov get` knows where to fetch from — see
{ref}`the configuration section <coverage-configuration>`.

## Version matching

The `gcov` that processes the counters must match the GCC **major version**
that compiled the product — the on-disk gcov record format changes between
releases (GCC 12 notably changed record length encoding, and GCC 14 added a
counter kind). A mismatched `gcov` fails with *"record size mismatch"* or
silently reports 0%. otto auto-discovers the right tool from the `.gcno`
version stamp where it can; see the toolchain resolution order on the main
page.

## Cross-compiled products

A cross-GCC's `gcov` cannot be discovered from the `.gcno` alone —
configure it per host in `lab.json`:

```json
{
    "element": "target1",
    "toolchain": {
        "sysroot": "/opt/toolchains/arm-none-eabi",
        "gcov": "bin/arm-none-eabi-gcov",
        "lcov": "/usr/bin/lcov"
    }
}
```

`lcov` is a host-side orchestrator, not part of the cross toolchain — point
it at the otto host's own `lcov`.
