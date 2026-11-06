#!/usr/bin/env bash
# Build the instrumented container image on the dev VM and save it as a
# tarball.
#
# The product is a static, --coverage build of project1's sample C program.
# Its sources are committed HERE (docker/src/), not compiled straight out of
# tests/project1/product/: otto's coverage capture anchors every measured file
# to a committed git blob under the project under test (tests/project5), and
# project1 is a different fixture project entirely. This is project5's own copy now —
# it is free to drift from tests/project1/product (each fixture project owns its
# own sources; nothing keeps the two in sync, by design). Compiling directly
# into docker/build/ (git-ignored) puts the .gcno files beside these
# committed sources, inside project5, where the report discovers them.
#
# The binary's baked-in .gcda path is this build directory's own absolute
# path, e.g. <build>/product-main.gcda — meaningless once the binary is
# COPYed into the image and run in a container with a different
# filesystem. The products declare `build_root = "docker/build"`
# (.otto/settings.toml), so otto strips ALL of this directory's own
# components on the `docker run` it owns: both .gcda land flat under
# GCOV_PREFIX, with no intermediate directories, whatever this checkout's
# depth. `docker exec` inherits both variables, as the suite calls it.
#
# The image's PID 1 is the product's own `serve` (see Dockerfile and
# src/main.c): a `docker stop` ends it through its SIGTERM handler, so the
# stop is what flushes its counters. `docker exec ... /opt/product <op>` runs
# the short ops beside it as before.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/src"
BUILD="$HERE/build"
mkdir -p "$BUILD"
gcc --coverage -static -O0 -g -o "$BUILD/product" "$SRC/main.c" "$SRC/math_ops.c"

# Two more builds, neither of which goes into the image (its context stays
# build/): build-bind/product is the same product, which the e2e bind-mounts
# into a container at /opt instead (the stopped-container --volumes-from
# proof), and build-unit/test_math_ops is the unit test, run on the dev VM, whose
# directory is cov_container_ref's unit view (.otto/settings.toml).
mkdir -p "$HERE/build-bind" "$HERE/build-unit"
gcc --coverage -static -O0 -g -o "$HERE/build-bind/product" "$SRC/main.c" "$SRC/math_ops.c"
gcc --coverage -O0 -g -o "$HERE/build-unit/test_math_ops" \
    "$HERE/unit/test_math_ops.c" "$SRC/math_ops.c"

docker build -q -t otto-cov-demo:e2e -f "$HERE/Dockerfile" "$BUILD" >/dev/null
docker save -o "$HERE/otto-cov-demo.tar" otto-cov-demo:e2e
gcno_count=$(find "$BUILD" -maxdepth 1 -name '*.gcno' | wc -l)
if [ "$gcno_count" -eq 0 ]; then
    echo "no .gcno beside $BUILD/product: --coverage gcc build produced none (wrong compiler," \
        "wrong flags, or a stale non-instrumented binary reused)" >&2
    exit 1
fi
echo "built: $HERE/otto-cov-demo.tar ($gcno_count .gcno in $BUILD)"
