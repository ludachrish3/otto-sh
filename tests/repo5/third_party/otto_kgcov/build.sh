#!/usr/bin/env bash
# Build otto_kgcov.ko (the library only) for a kernel, out of tree.
#
#   build.sh <build-dir> [<kernel-release>]
#
# Copies the library into <build-dir>/lib and builds it there, so the source
# tree stays clean. The kernel tree is $KDIR when set (a distro headers
# package or a prepared source tree, anywhere), else
# /lib/modules/<release>/build for <release> (default: the running kernel).
# ARCH, CROSS_COMPILE, LLVM, CC and KMAKEFLAGS reach kbuild unchanged, so a
# cross build or a clang build is the same command with the environment a
# module of your own would take — see
# docs/cli/cov/instrumenting/kernel-modules.md. <release> is what the
# built module's vermagic is checked against; with KDIR set and no <release>
# it is read from the tree's include/config/kernel.release (what kbuild bakes
# into vermagic; a headers package has it, a source tree after
# `modules_prepare`).
#
# The worked consumer, tests/repo5/kmod/demo/, is a SUT repo's own product
# and is built in place by that repo's own script (tests/repo5/kmod/build.sh),
# which calls this one for the library first — a capture anchors every
# measured file to a committed blob under the SUT repo, so the consumer cannot
# live out here and be built out-of-tree the way the library is.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${1:?usage: build.sh <build-dir> [<kernel-release>]}"
if [ -n "${KDIR:-}" ]; then
	[ -d "$KDIR" ] || { echo "KDIR=$KDIR is not a directory" >&2; exit 1; }
	if [ -z "${2:-}" ] && [ ! -f "$KDIR/include/config/kernel.release" ]; then
		echo "$KDIR has no include/config/kernel.release (a source tree needs 'make ... modules_prepare' first); or pass <kernel-release> explicitly" >&2
		exit 1
	fi
	RELEASE="${2:-$(cat "$KDIR/include/config/kernel.release")}"
else
	RELEASE="${2:-$(uname -r)}"
	KDIR="/lib/modules/${RELEASE}/build"
	[ -d "$KDIR" ] || { echo "no kernel build tree at $KDIR (install linux-headers-${RELEASE}, or set KDIR)" >&2; exit 1; }
fi

mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"
if [ -e "$BUILD_DIR/lib" ] && [ ! -e "$BUILD_DIR/lib/kgcov.c" ]; then
	echo "refusing to remove $BUILD_DIR/lib: it exists but has no kgcov.c in it (wrong <build-dir>?)" >&2
	exit 1
fi
rm -rf "$BUILD_DIR/lib"
mkdir -p "$BUILD_DIR/lib"
cp "$SRC_DIR"/{Kbuild,Makefile,consumer.mk,kgcov.c,kgcov.h,kgcov_gcov.h,kgcov_gcc.c,kgcov_gcc_abi.c,kgcov_clang.c,kgcov_version.h} "$BUILD_DIR/lib/"
[ -f "$SRC_DIR/kgcov_local.h" ] && cp "$SRC_DIR/kgcov_local.h" "$BUILD_DIR/lib/"

# CC goes on the command line: the kernel's own Makefile assigns CC and only
# a command-line value overrides that. ARCH, CROSS_COMPILE and LLVM are read
# from the environment by kbuild itself. KMAKEFLAGS is split on whitespace on
# purpose — it is a list of make arguments.
# shellcheck disable=SC2086
make -C "$BUILD_DIR/lib" KDIR="$KDIR" ${CC:+CC=$CC} ${KMAKEFLAGS:-}

vermagic="$(modinfo -F vermagic "$BUILD_DIR/lib/otto_kgcov.ko")"
case "$vermagic" in "$RELEASE "*) ;; *) echo "lib/otto_kgcov.ko: vermagic '$vermagic' is not for $RELEASE" >&2; exit 1 ;; esac
echo "built: $BUILD_DIR/lib/otto_kgcov.ko (for $RELEASE)"
