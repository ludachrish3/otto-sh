#!/usr/bin/env bash
# Build repo5's kernel-module products for the running kernel (the bed's:
# 6.8.0-86-generic, the same release as the dev VM): the library out of
# tree into build/lib/ (git-ignored — docs/examples/kgcov/build.sh does
# this half), then the demo IN PLACE under kmod/demo/ against it. The
# demo's sources are committed here rather than copied: otto's coverage
# capture anchors every measured file to a committed git blob under the
# SUT repo, so only the library (never itself directly measured) needs an
# out-of-tree build.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE="${1:-$(uname -r)}"
KDIR="/lib/modules/${RELEASE}/build"

"$HERE/../../docs/examples/kgcov/build.sh" "$HERE/build" "$RELEASE"
make -C "$HERE/kmod/demo" KDIR="$KDIR" KGCOV="$HERE/build/lib"

vermagic="$(modinfo -F vermagic "$HERE/kmod/demo/otto_kmod_demo.ko")"
case "$vermagic" in
    "$RELEASE "*) ;;
    *)
        echo "kmod/demo/otto_kmod_demo.ko: vermagic '$vermagic' is not for $RELEASE" >&2
        exit 1
        ;;
esac
ls "$HERE"/kmod/demo/*.gcno >/dev/null
echo "built: $HERE/build/lib/otto_kgcov.ko $HERE/kmod/demo/otto_kmod_demo.ko (for $RELEASE)"
