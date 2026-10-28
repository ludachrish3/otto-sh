#!/usr/bin/env bash
#
# Build the extension into build/ext/ under this project, from any directory:
#   ext/build.sh
#
# The inputs come from the environment; a missing one is refused by name.
#   ZEPHYR_WORKSPACE         the Zephyr west workspace
#   ZEPHYR_SDK_INSTALL_DIR   the Zephyr SDK
#   EMBEDDED_GCOV_DIR        an embedded-gcov checkout with the patch applied
#                            (`otto cov embedded-gcov export` writes the patch)
#   OTTO                     (optional) the otto to use, default `otto` on PATH; its
#                            `cov embedded-gcov finish` strips the extension and
#                            checks its gcov stamp
#   ZEPHYR_VENV              (optional) a python venv holding west
#   BOARD                    (optional) the board, default mps2_an385
#
# The build is incremental. If the project has moved since the last build, west
# refuses the old build dir: delete build/ext and run this again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build/ext"

: "${ZEPHYR_WORKSPACE:?set ZEPHYR_WORKSPACE to the Zephyr west workspace}"
: "${ZEPHYR_SDK_INSTALL_DIR:?set ZEPHYR_SDK_INSTALL_DIR to the Zephyr SDK}"
: "${EMBEDDED_GCOV_DIR:?set EMBEDDED_GCOV_DIR to an embedded-gcov checkout with the patch applied}"

# The script cd's into the workspace and CMake resolves paths against ext/, so
# every input is made absolute here, relative to where the script was started.
OTTO="$(command -v "${OTTO:-otto}" || true)"
[ -n "$OTTO" ] || { echo "ERROR: no otto found: put otto on PATH or set OTTO to its console script." >&2; exit 1; }
[ -d "$ZEPHYR_WORKSPACE" ] || { echo "ERROR: ZEPHYR_WORKSPACE=$ZEPHYR_WORKSPACE is not a directory." >&2; exit 1; }
[ -d "$ZEPHYR_SDK_INSTALL_DIR" ] || { echo "ERROR: ZEPHYR_SDK_INSTALL_DIR=$ZEPHYR_SDK_INSTALL_DIR is not a directory." >&2; exit 1; }
OTTO="$(cd "$(dirname "$OTTO")" && pwd)/$(basename "$OTTO")"
ZEPHYR_WORKSPACE="$(cd "$ZEPHYR_WORKSPACE" && pwd)"
ZEPHYR_SDK_INSTALL_DIR="$(cd "$ZEPHYR_SDK_INSTALL_DIR" && pwd)"
if [ ! -e "$EMBEDDED_GCOV_DIR/code" ]; then
    echo "ERROR: EMBEDDED_GCOV_DIR=$EMBEDDED_GCOV_DIR has no code/ directory." >&2
    exit 1
fi
EMBEDDED_GCOV_DIR="$(cd "$EMBEDDED_GCOV_DIR" && pwd)"
if [ -n "${ZEPHYR_VENV:-}" ]; then
    ZEPHYR_VENV="$(cd "$ZEPHYR_VENV" && pwd)"
fi
BOARD="${BOARD:-mps2_an385}"
export ZEPHYR_SDK_INSTALL_DIR

if [ ! -f "$ROOT/ext/src/cov_entry.c" ]; then
    echo "ERROR: ext/src/cov_entry.c is missing; restore it from the starter 'otto init --embedded-gcov' wrote:" >&2
    echo "       cp third_party/embedded-gcov-patch-consumer/cov_entry.c ext/src/" >&2
    exit 1
fi
# The runtime must carry the patch: an unpatched one fails on the device, late.
for patch in "$ROOT"/third_party/embedded-gcov-patch/*.patch; do
    [ -e "$patch" ] || continue
    if ! git -C "$EMBEDDED_GCOV_DIR" apply --reverse --check "$patch" 2>/dev/null; then
        echo "ERROR: EMBEDDED_GCOV_DIR=$EMBEDDED_GCOV_DIR does not carry $patch;" >&2
        echo "       apply it: git -C \"$EMBEDDED_GCOV_DIR\" apply \"$patch\"" >&2
        exit 1
    fi
done

# The SDK's layout differs by version, so find objcopy rather than spell its path.
# `|| true` lets the message below fire when the SDK directory is wrong.
OBJCOPY="$(find "$ZEPHYR_SDK_INSTALL_DIR" -maxdepth 4 -type f -name 'arm-zephyr-eabi-objcopy' 2>/dev/null | head -1)" || true
if [ -z "$OBJCOPY" ]; then
    echo "ERROR: arm-zephyr-eabi-objcopy not found under $ZEPHYR_SDK_INSTALL_DIR" >&2
    exit 1
fi

if [ -n "${ZEPHYR_VENV:-}" ]; then
    # shellcheck disable=SC1091
    source "$ZEPHYR_VENV/bin/activate"
fi
cd "$ZEPHYR_WORKSPACE"
# shellcheck disable=SC1091
source zephyr/zephyr-env.sh

# Zephyr lists the compiled object only as an order-only dependency of the
# extension, so an incremental recompile refreshes the object and its .gcno
# (with a new gcov stamp) but never re-links the extension: the stale extension
# then mismatches the new .gcno and the report reads 0%. Removing the link-tail
# outputs makes ninja regenerate them from the current object.
rm -f "$BUILD/llext/mathops_debug.elf" "$BUILD/zephyr/mathops.llext"

echo "=== west build ($BOARD) -> $BUILD ==="
west build -d "$ROOT/build/ext" "$ROOT/ext" -b "$BOARD" -- -DEMBEDDED_GCOV_DIR="$EMBEDDED_GCOV_DIR"

# Strip the sections the loader cannot relocate, then check the extension still
# shares its gcov stamp with the .gcno that decodes its counters (a stale link
# would read 0%). One command owns both.
"$OTTO" cov embedded-gcov finish \
    "$BUILD/zephyr/mathops.llext" "$BUILD/zephyr/mathops.stripped.llext" \
    --objcopy "$OBJCOPY" --gcno-dir "$BUILD/CMakeFiles/mathops_llext_lib.dir"

echo "=== built $BUILD/zephyr/mathops.stripped.llext ==="
