# GCC products

The default coverage path: a product compiled with GNU GCC on a Unix-like
target, counters fetched over the network. Everything on the main
{doc}`../index` page applies as-is; this page collects the GCC-specific
setup.

## Build flags

Compile *and link* the product with `--coverage` (shorthand for
`-fprofile-arcs -ftest-coverage` plus the gcov link library). Keep the build
tree around: the compiler writes one `.gcno` (notes) file per object at
build time, and collection needs them to decode the `.gcda` counters
the product writes at run time.

```make
CFLAGS  += --coverage -O0
LDFLAGS += --coverage
```

`-O0` is not required, but optimized builds fold lines and branches
together and make the line table noticeably harder to read.

(coverage-build-requirement)=
## The one build requirement

**Each object file needs a unique path: absolute, or relative to the project
root.**

This is the only build requirement coverage has, and it applies to clang
builds ({doc}`clang`) as much as to gcc ones.

otto matches each `.gcda` it fetches to its `.gcno` by the path the `.gcda`
was written at, never by file name alone. Two `main.cpp` files in two
directories, or in two products, therefore stay apart. An ordinary build
meets the rule without effort. The compiler bakes each object's absolute
path into the binary, and the `.gcda` is written at that path (see
[Where counters land](#where-counters-land) below).

Two things can break it:

- **`GCOV_PREFIX_STRIP` set too deep.** The strip count drops leading
  components from each path before the `.gcda` is written. Strip far enough
  that two objects' remaining paths are the same, for example both
  `main.cpp.gcda`, and the product's runtime merges both objects' counters
  into one file on the device. otto then refuses the file (see
  [Pairing errors](#pairing-errors)).
- **`-fprofile-dir`.** It folds the whole path into a single file name, so
  otto cannot follow it. otto does not support it. Use `GCOV_PREFIX` and
  `GCOV_PREFIX_STRIP` instead.

### Clean the counters after a rebuild

Each process adds its counts to whatever `.gcda` is already on disk when it
exits. After you rebuild a product, clear the counters it wrote before the
rebuild. `otto test --cov` does this for you before every run
(`--cov-clean`, on by default; see {doc}`../during-tests`). Outside a test
run, use {doc}`../clean`. The two compilers differ in what happens if you
skip the clean:

- **gcc** sees that the old file belongs to another build and overwrites it,
  so you only lose the earlier counts.
- **clang** adds the new counts into the old file without checking that it
  belongs to the same build. The counts come out wrong, and nothing reports
  an error.

### Shared libraries and plugins

otto reads the list of objects built into a `shell` product from its
`artifact`. For an ELF executable it also follows the libraries the
executable links against (its `DT_NEEDED` entries). otto follows a library
when exactly one file under the project root has its name (its soname). Those libraries'
objects are paired exactly like the executable's own.

A library the product loads with `dlopen` (a plugin) cannot be found that
way. Its counters are still fetched and paired by their path alone, and one
INFO line per product lists them. Pairing by path alone searches the project
root, so a second build tree there can make it ambiguous. To have a plugin's
objects paired like the rest, declare the product's `artifact` as a
directory or an archive that contains the plugins too
({doc}`../../../configuration/declared-products-tools`).

(coverage-pairing-errors)=
### Pairing errors

When otto cannot pair a `.gcda` with exactly one `.gcno`, collection fails
for that product and names the file, every candidate otto considered and
why it was rejected. The product's counters stay on the host, so once the
cause is fixed, `otto cov get` ({doc}`../get`) captures them. The exception
is a merge on the device: those counters are already mixed, so clean them
and run the tests again. Each error ends with its remedy:

| The error says | What happened | What to do |
|---|---|---|
| *matches several objects … the device wrote their counters to one file; lower GCOV_PREFIX_STRIP* | The strip count left two objects with the same path, and the runtime merged their counters into one file | Lower `GCOV_PREFIX_STRIP`, clean, and run again |
| *these build trees each hold notes that match … remove the stale tree* | The product was built somewhere else (in a container, on a CI machine), so otto searched the project root. Several build trees there hold different notes that all fit this object | Remove the stale build tree |
| *no notes file for … keep the build tree that produced the product* | No `.gcno` under the project root fits this object | Keep, or restore, the build tree that produced the deployed product |

An object the artifact lists and that was built on this machine never meets
the second error. Its notes sit at the object's own path, and otto uses
them without searching, so a stale build tree or a unit-test tree elsewhere
in the project is never considered.

A fourth failure is not a pairing error: the object's own notes exist, but
they belong to a later build. That is the rebuilt-notes mismatch, covered in
{ref}`coverage-gcc-stamp-guard`.

## Where counters land

Each instrumented process writes its `.gcda` files on exit, by default next
to the build's object files — the absolute path is baked in at compile time,
and it is the *build machine's* path, which on a target usually does not
exist. `GCOV_PREFIX` redirects those writes at run time, and the product's
`cov_dir` is where that directory is declared, so `otto cov get` knows where
to fetch from. Where the two variables are set depends on how the product
runs.

(coverage-daemon-case)=
**A daemon** — a process `install` starts and leaves running — gets them in
its `install` command, and writes its counters when it exits:

```toml
[[products]]
name = "myproduct"
kind = "shell"
artifact = "build/myproduct"
stage_dir = "/opt/myproduct"         # where staging puts the artifact
cov_dir = "/var/coverage/myproduct"
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 setsid /opt/myproduct/myproduct </dev/null >/tmp/myproduct.log 2>&1 &"
```

The `setsid … &` form starts a long-running product and returns; see
{ref}`long-running products <declared-long-running>`.

`GCOV_PREFIX_STRIP` drops that many leading components off the baked-in path
before `GCOV_PREFIX` is prepended, which is what keeps the tree under
`cov_dir` shallow rather than a deep replica of the build machine's layout.
Together the two variables choose the path each `.gcda` is written at, and
otto fetches each file at that path relative to `cov_dir`. An object built at
`/home/dev/agent/build/src/main.cpp.o` writes, with `GCOV_PREFIX_STRIP=4`,
`<cov_dir>/src/main.cpp.gcda`. Keep the strip count low enough that every
object's path stays unique (see
[The one build requirement](#the-one-build-requirement)). `cov_dir` defaults to `/tmp/<name>`, so two products on one host stay apart
without being told to. See
{doc}`../../../configuration/declared-products-tools` for the entry, and
{ref}`the configuration section <coverage-configuration>` for the rest.

(coverage-run-per-test-case)=
**A command-line tool run once per test** writes its counters each time it
exits, so the test sets the variables on every invocation. Read the
directory from the product rather than repeating the path, so the test and
`otto cov get` cannot disagree: `Product.cov_dir` is filled in concrete when
the lab loads.

```python
product = next(p for p in host.products if p.name == "mytool")
result = await host.exec(
    f"GCOV_PREFIX={product.cov_dir} GCOV_PREFIX_STRIP=3 /opt/mytool/mytool --check"
)
```

Each run adds its counts to the `.gcda` files the previous runs left, so
every invocation of the tool across the run is counted.

A kernel module has no `GCOV_PREFIX`; {doc}`kernel-modules` is that path.
Inside a container the same two variables apply; {doc}`containers` is the
bind-mount half.

## Version matching

The `gcov` that processes the counters must match the GCC **major version**
that compiled the product — the on-disk gcov record format changes between
releases (GCC 12 notably changed record length encoding, and GCC 14 added a
counter kind). A mismatched `gcov` fails with *"Incompatible GCC/GCOV
version"* or *"record size mismatch"*, or silently reports 0%. otto reads
the major from the `.gcda` stamp: a product built by the system gcc's major
is read by the system `gcov`, one built by another major by `gcov-<major>`
from `PATH` (`apt install gcc-12` ships `gcov-12`), and that package is
named when it is missing. A missing `gcov` fails only that product: it
gets a `capture.failed` naming the package, and every other product still
captures. `otto test --cov` logs the failure as a warning, `otto cov get`
names it and exits `1`, and `otto cov report` names it in its one warning
about products that failed to capture. A
host record that names a
`toolchain.gcov` is used instead of the stamp — the order is on the
{ref}`main page <coverage-gcov-resolution>`.

## Cross-compiled products

A cross-GCC's `gcov` cannot be discovered from the stamp — it names a
compiler major, never a target — so configure it per host entry in
`lab.json`:

```json
{
    "name": "target1",
    "labs": ["unix"],
    "hosts": [
        {
            "ip": "10.10.200.12",
            "creds": [{ "login": "admin", "password": "secret" }],
            "toolchain": {
                "sysroot": "/opt/toolchains/arm-none-eabi",
                "gcov": "bin/arm-none-eabi-gcov",
                "lcov": "/usr/bin/lcov"
            }
        }
    ]
}
```

`lcov` is a host-side orchestrator, not part of the cross toolchain — point
it at the otto host's own `lcov`.

(coverage-gcc-stamp-guard)=
## Stale deploys: the rebuilt-notes mismatch and the `.gcno` stamp guard

Every GCC compilation stamps a fresh 32-bit value into both the `.gcno`
notes file and the `gcov_info` struct linked into the binary —
recompiling even an *unchanged* source produces a new stamp.  Counters
are only decodable against the exact compilation that produced them.

For an object built on this machine, its own notes file, at the object's
path, is the one candidate otto pairs with. otto does not look for another
when the stamps disagree. A `.gcda` whose stamp does not match those notes
means the product was rebuilt after the deployed binary was built. Collection
fails for that product with the `CoverageDataMismatchError` described in
{ref}`coverage-report-stale-builds`, naming both files. The product's
counters stay on the host, and they still belong to the deployed build, so
there are two ways out:

- **Restore the build that produced the deployed product**, then run
  `otto cov get` again. The same counters now pair with the right notes.
- **Redeploy the current build and run the tests again** (`otto test
  --cov`). The counters on the host belong to the old binary, so a plain
  `otto cov get` would fail the same way. The run's pre-run clean
  (`--cov-clean`, on by default) clears them first; outside a test run,
  clear them with {doc}`../clean` before the new session.

When the product runs in place, straight out of the build tree, that
error and its remedy are all you need.  A build-time guard
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
notes = {
    f: (b[4:8], struct.unpack_from("<I", b, 8)[0])
    for f in glob.glob(str(build / "**" / "*.gcno"), recursive=True)
    for b in [pathlib.Path(f).read_bytes()]
}
blob = binary.read_bytes()
embedded = {
    struct.unpack_from("<I", blob, i + off)[0]
    for ver in {v for v, _ in notes.values()}
    for off in (8, 16)  # 32-bit / 64-bit gcov_info layouts
    for i in range(len(blob) - 24)
    if blob[i : i + 4] == ver
}
stale = {f: s for f, (_, s) in notes.items() if s not in embedded}
if stale:
    sys.exit(
        "stamp guard: %s does not embed the stamp of %s "
        "— it is stale relative to the notes; re-link before deploying"
        % (binary, ", ".join("%s (%#x)" % kv for kv in stale.items()))
    )
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
