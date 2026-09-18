#!/usr/bin/env bash
# Build the instrumented container image on the dev VM and save it as a
# tarball.
#
# The product is a static, --coverage build of repo1's sample C program.
# Its sources are committed HERE (docker/src/), not compiled straight out of
# tests/repo1/product/: otto's coverage capture anchors every measured file
# to a committed git blob under the SUT repo under test (tests/repo5), and
# repo1 is a different SUT fixture entirely. This is repo5's own copy now —
# it is free to drift from tests/repo1/product (each fixture repo owns its
# own sources; nothing keeps the two in sync, by design). Compiling directly
# into docker/build/ (git-ignored) puts the .gcno files beside these
# committed sources, inside repo5, where the report discovers them.
#
# The binary's baked-in .gcda path is this build directory's own absolute
# path, e.g. <build>/product-main.gcda — meaningless once the binary is
# COPYed into the image and run in a container with a different
# filesystem. GCOV_PREFIX_STRIP tells the gcov runtime how many leading
# '/'-separated components of that absolute path to drop before prepending
# GCOV_PREFIX; stripping ALL of the build directory's own components lands
# the files flat under GCOV_PREFIX — both .gcda land directly under
# GCOV_PREFIX with no intermediate directories, proven on the dev VM with a
# throwaway container (GCOV_PREFIX set only at `docker run`, no -e on
# `docker exec`, matching exactly how the suite calls it). That count
# depends on this checkout's own path depth (a worktree sits deeper than
# the main checkout), so it is computed here, at build time, and baked into
# the image as an ENV default (see Dockerfile) rather than hardcoded in
# settings.toml — the products' run_args then need only pass GCOV_PREFIX.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/src"
BUILD="$HERE/build"
mkdir -p "$BUILD"
gcc --coverage -static -O0 -g -o "$BUILD/product" "$SRC/main.c" "$SRC/math_ops.c"

# Count BUILD's own '/'-separated components: stripping that many leaves
# just the bare filename (e.g. "product-main.gcda").
strip_count=$(echo "$BUILD" | tr -s '/' '\n' | grep -c .)

docker build -q -t otto-cov-demo:e2e --build-arg GCOV_PREFIX_STRIP="$strip_count" \
    -f "$HERE/Dockerfile" "$BUILD" >/dev/null
docker save -o "$HERE/otto-cov-demo.tar" otto-cov-demo:e2e
gcno_count=$(find "$BUILD" -maxdepth 1 -name '*.gcno' | wc -l)
if [ "$gcno_count" -eq 0 ]; then
    echo "no .gcno beside $BUILD/product: --coverage gcc build produced none (wrong compiler," \
        "wrong flags, or a stale non-instrumented binary reused)" >&2
    exit 1
fi
echo "built: $HERE/otto-cov-demo.tar (GCOV_PREFIX_STRIP=$strip_count, $gcno_count .gcno in $BUILD)"
