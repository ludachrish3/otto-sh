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

(coverage-gcc-stamp-guard)=
## Stale deploys: the `.gcno` stamp guard

Every GCC compilation stamps a fresh 32-bit value into both the `.gcno`
notes file and the `gcov_info` struct linked into the binary —
recompiling even an *unchanged* source produces a new stamp.  Counters
are only decodable against the exact compilation that produced them; a
`.gcda` whose stamp disagrees with the `.gcno` is refused at collection
(*"stamp mismatch with notes file"*), which otto surfaces as the
`CoverageDataMismatchError` described in
{ref}`coverage-report-stale-builds`.

When the product runs in place, straight out of the build tree, that
error and its remedy (re-collect) are all you need.  A build-time guard
earns its keep when the flow has a **ship step** — the product is built
on one machine and deployed to the target Unix hosts.  Rebuild locally
after deploying, and every later run of the stale deploy produces
undecodable counters; without a guard, the mistake surfaces minutes
later at collection instead of at the moment it was made.

The check: every `.gcno`'s stamp must appear in the binary about to be
shipped.  The stamp is the 32-bit word at byte offset 8 of the `.gcno`;
in the binary it sits inside each `gcov_info`, following the 4-byte gcov
format-version marker (the `.gcno`'s bytes 4–8) — 8 bytes after it on
32-bit targets, 16 on 64-bit (the struct's `next` pointer plus alignment
padding sit between):

```python
import glob, struct, sys, pathlib
build, binary = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
notes = {f: (b[4:8], struct.unpack_from("<I", b, 8)[0])
         for f in glob.glob(str(build / "**" / "*.gcno"), recursive=True)
         for b in [pathlib.Path(f).read_bytes()]}
blob = binary.read_bytes()
embedded = {struct.unpack_from("<I", blob, i + off)[0]
            for ver in {v for v, _ in notes.values()}
            for off in (8, 16)              # 32-bit / 64-bit gcov_info layouts
            for i in range(len(blob) - 24) if blob[i:i + 4] == ver}
stale = {f: s for f, (_, s) in notes.items() if s not in embedded}
if stale:
    sys.exit("stamp guard: %s does not embed the stamp of %s "
             "— it is stale relative to the notes; re-link before deploying"
             % (binary, ", ".join("%s (%#x)" % kv for kv in stale.items())))
print("stamp guard OK: %d notes file(s) match" % len(notes))
```

Run it as the last step of the build (or the first step of the deploy),
pointed at a build tree containing only the objects that actually link
into the shipped binary — `.gcno` files from other targets sharing the
tree (unit tests, tools) would false-positive.  The embedded flavor of
the same guard — where the ship step is `llext load_hex` — is on the
{ref}`embedded page <coverage-embedded-stamp-guard>`; clang products
need a different approach entirely — see
{ref}`the clang page <coverage-clang-stale-deploys>`.
