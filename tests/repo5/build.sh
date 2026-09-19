#!/usr/bin/env bash
# Build every product of the fixture: the kernel-module half (kmod/build.sh:
# the library out of tree, the demo in place, for the running kernel or the
# environment's KDIR/toolchain), then the container-image half
# (docker/build.sh). Each half is its own script so a suite that needs only
# one builds only that one; this is the human entry point that builds both,
# sequenced under `set -e` so a failed kernel build stops the image build.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/kmod/build.sh" "$@"
"$HERE/docker/build.sh"
