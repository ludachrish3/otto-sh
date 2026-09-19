#!/usr/bin/env bash
# Build repo5's kernel-module products: the library out of tree into
# build/lib/ (git-ignored — docs/examples/kgcov/build.sh does that half),
# then the demo IN PLACE under kmod/demo/ against it. The demo's sources are
# committed here rather than copied: otto's coverage capture anchors every
# measured file to a committed git blob under the SUT repo, so only the
# library (never itself directly measured) needs an out-of-tree build.
#
#   kmod/build.sh [<kernel-release>]
#
# The kernel tree and toolchain are the environment's, exactly as
# docs/examples/kgcov/build.sh takes them: KDIR (default
# /lib/modules/<release>/build), ARCH, CROSS_COMPILE, LLVM, CC, KMAKEFLAGS.
# <release> defaults to the running kernel, or to the tree's own release
# when KDIR is set; both modules' vermagic is checked against it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

if [ -n "${KDIR:-}" ]; then
	[ -d "$KDIR" ] || { echo "KDIR=$KDIR is not a directory" >&2; exit 1; }
	if [ -z "${1:-}" ] && [ ! -f "$KDIR/include/config/kernel.release" ]; then
		echo "$KDIR has no include/config/kernel.release (a source tree needs 'make ... modules_prepare' first); or pass <kernel-release> explicitly" >&2
		exit 1
	fi
	RELEASE="${1:-$(cat "$KDIR/include/config/kernel.release")}"
else
	RELEASE="${1:-$(uname -r)}"
	KDIR="/lib/modules/${RELEASE}/build"
fi
export KDIR

"$REPO/../../docs/examples/kgcov/build.sh" "$REPO/build" "$RELEASE"
# The demo builds IN PLACE (unlike the library, which build.sh always starts
# from a clean copy) so a previous run's objects for another compiler or
# ARCH must be cleaned first, or they can survive into this one's link.
# shellcheck disable=SC2086
make -C "$HERE/demo" KDIR="$KDIR" KGCOV="$REPO/build/lib" ${CC:+CC=$CC} ${KMAKEFLAGS:-} clean
# shellcheck disable=SC2086
make -C "$HERE/demo" KDIR="$KDIR" KGCOV="$REPO/build/lib" ${CC:+CC=$CC} ${KMAKEFLAGS:-}

vermagic="$(modinfo -F vermagic "$HERE/demo/otto_kmod_demo.ko")"
case "$vermagic" in
    "$RELEASE "*) ;;
    *)
        echo "kmod/demo/otto_kmod_demo.ko: vermagic '$vermagic' is not for $RELEASE" >&2
        exit 1
        ;;
esac
ls "$HERE"/demo/*.gcno >/dev/null
echo "built: $REPO/build/lib/otto_kgcov.ko $HERE/demo/otto_kmod_demo.ko (for $RELEASE)"

# The single writer of the toolchain stamp the fixture's e2e helper
# (tests/e2e/cov/_repo5_build.py) reads to know which compiler built these
# modules: a hand run leaves OTTO_KGCOV_TOOLCHAIN unset, so it stamps
# "manual" — a name no helper Toolchain ever carries, so the next
# ensure_kmod_artifacts() call always rebuilds rather than trusting it.
printf '%s\n' "${OTTO_KGCOV_TOOLCHAIN:-manual}" > "$REPO/build/toolchain"
