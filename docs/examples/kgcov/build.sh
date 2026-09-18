#!/usr/bin/env bash
# Build otto_kgcov.ko (the library only) for a kernel release, out of tree.
#
#   build.sh <build-dir> [<kernel-release>]
#
# Copies the library into <build-dir>/lib and builds it there against
# /lib/modules/<release>/build (default: the running kernel), so the source
# tree under docs/examples/ stays clean. The worked consumer,
# tests/repo5/kmod/demo/, is a SUT repo's own product and is built in
# place by that repo's own build.sh (tests/repo5/build.sh), which calls
# this script for the library first — a capture anchors every measured
# file to a committed blob under the SUT repo, so the consumer cannot live
# out here and be built out-of-tree the way the library is.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${1:?usage: build.sh <build-dir> [<kernel-release>]}"
RELEASE="${2:-$(uname -r)}"
KDIR="/lib/modules/${RELEASE}/build"

[ -d "$KDIR" ] || { echo "no kernel build tree at $KDIR (install linux-headers-${RELEASE})" >&2; exit 1; }

mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"
if [ -e "$BUILD_DIR/lib" ] && [ ! -e "$BUILD_DIR/lib/kgcov.c" ]; then
	echo "refusing to remove $BUILD_DIR/lib: it exists but has no kgcov.c in it (wrong <build-dir>?)" >&2
	exit 1
fi
rm -rf "$BUILD_DIR/lib"
mkdir -p "$BUILD_DIR/lib"
cp "$SRC_DIR"/{Kbuild,Makefile,consumer.mk,kgcov.c,kgcov.h,kgcov_gcc.c,kgcov_gcc.h,kgcov_stubs.c} "$BUILD_DIR/lib/"

make -C "$BUILD_DIR/lib" KDIR="$KDIR"

vermagic="$(modinfo -F vermagic "$BUILD_DIR/lib/otto_kgcov.ko")"
case "$vermagic" in "$RELEASE "*) ;; *) echo "lib/otto_kgcov.ko: vermagic '$vermagic' is not for $RELEASE" >&2; exit 1 ;; esac
echo "built: $BUILD_DIR/lib/otto_kgcov.ko (for $RELEASE)"
