#!/usr/bin/env bash
# Build project_ctr's coverage product and its runtime image, on the dev VM.
#
# The product is compiled INSIDE a container, with this project directory
# bind-mounted at /build: the binary's baked-in counter paths are then
# /build/build/<object>.gcda — paths that only exist in a container — while
# the .gcno notes land here, in build/ (git-ignored), beside the committed
# sources the capture anchors to. The compiler image is the kmodcov 5.15
# column's (scripts/provision_kmodcov_kernels.sh), an Ubuntu 22.04 with gcc
# already provisioned on the dev VM; the runtime image is built FROM that
# same Ubuntu, so the dynamically linked binary runs in the container. No
# network is needed: both bases are local images.
#
# Outputs (all under build/):
#   counter                    the --coverage binary the product stages
#   main.gcno, ops.gcno        its notes
#   otto-cov-ctr-runtime.tar   `docker save` of the runtime image, for the e2e
#                              to `docker load` onto test3 (which has no internet)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"

COMPILER_IMAGE="otto-kmodcov-kernel:5.15"
RUNTIME_IMAGE="otto-cov-ctr-runtime:e2e"

for image in "$COMPILER_IMAGE" arm64v8/ubuntu:22.04; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "missing image $image on the dev VM: provision it with" \
            "scripts/provision_kmodcov_kernels.sh" >&2
        exit 1
    fi
done

rm -rf "$BUILD"
mkdir -p "$BUILD"
# As the invoking user, so nothing root-owned lands in the checkout.
in_compiler() {
    docker run --rm -u "$(id -u):$(id -g)" -v "$HERE:/build" -w /build "$COMPILER_IMAGE" "$@"
}
in_compiler gcc --coverage -O0 -g -c src/main.c -o build/main.o
in_compiler gcc --coverage -O0 -g -c src/ops.c -o build/ops.o
in_compiler gcc --coverage -o build/counter build/main.o build/ops.o

gcno_count=$(find "$BUILD" -maxdepth 1 -name '*.gcno' | wc -l)
if [ "$gcno_count" -ne 2 ]; then
    echo "expected 2 .gcno in $BUILD, found $gcno_count" >&2
    exit 1
fi

docker build -q -t "$RUNTIME_IMAGE" "$HERE/docker" >/dev/null
docker save -o "$BUILD/otto-cov-ctr-runtime.tar" "$RUNTIME_IMAGE"
echo "built: $BUILD/counter ($gcno_count .gcno), $RUNTIME_IMAGE -> $BUILD/otto-cov-ctr-runtime.tar"
