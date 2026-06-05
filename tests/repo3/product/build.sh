#!/usr/bin/env bash
#
# Build the repo3 LLEXT coverage extension (cov_ext.stripped.llext) into the
# given build dir. This is the single command the TestEmbeddedCoverage suite
# runs to keep the product up to date before loading it — the embedded analogue
# of repo1's `make -C product clean all`.
#
# Usage:
#   build.sh <build-dir>            # e.g. build.sh ~/build/cov_ext_app
#
# Env overrides (default to the dev VM's Vagrant-provisioned locations):
#   ZEPHYR_VENV              python venv with west            (~/zephyr-venv-v3_7)
#   ZEPHYR_WORKSPACE         the Zephyr 3.7 west workspace    (~/zephyrproject-v3_7)
#   ZEPHYR_SDK_INSTALL_DIR   the Zephyr SDK                   (~/zephyr-sdk-0.16.8)
#
# Recipe and the why-behind-each-step live in ./README.md and ../docs/feasibility.md.
set -euo pipefail

BUILD_DIR="${1:?usage: build.sh <build-dir>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_DIR="$SCRIPT_DIR"                       # tests/repo3/product
REPO3_DIR="$(dirname "$SCRIPT_DIR")"            # tests/repo3
TOPLEVEL="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SUBMODULE="$REPO3_DIR/third_party/embedded-gcov"
PATCH="$REPO3_DIR/third_party/patches/embedded-gcov-zephyr-gcc12.patch"

ZEPHYR_VENV="${ZEPHYR_VENV:-$HOME/zephyr-venv-v3_7}"
ZEPHYR_WORKSPACE="${ZEPHYR_WORKSPACE:-$HOME/zephyrproject-v3_7}"
ZEPHYR_SDK_INSTALL_DIR="${ZEPHYR_SDK_INSTALL_DIR:-$HOME/zephyr-sdk-0.16.8}"
export ZEPHYR_SDK_INSTALL_DIR
OBJCOPY="$ZEPHYR_SDK_INSTALL_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-objcopy"

# 1. embedded-gcov submodule + gcc-12 patch (idempotent). The runtime is NASA's
#    embedded-gcov (a submodule otto doesn't own); the patch adds gcc-12 support.
if [ ! -e "$SUBMODULE/code" ]; then
    echo "=== embedded-gcov: initializing submodule ==="
    git -C "$TOPLEVEL" submodule update --init -- "${SUBMODULE#"$TOPLEVEL"/}"
fi
if git -C "$SUBMODULE" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "=== embedded-gcov: gcc-12 patch already applied ==="
else
    echo "=== embedded-gcov: applying gcc-12 patch ==="
    git -C "$SUBMODULE" apply "$PATCH"
fi

# 2. Zephyr build environment.
cd "$ZEPHYR_WORKSPACE"
# shellcheck disable=SC1091
source "$ZEPHYR_VENV/bin/activate"
# shellcheck disable=SC1091
source zephyr/zephyr-env.sh

# 3. Build the LLEXT extension. Incremental (no `-p always`): ninja rebuilds only
#    changed sources, so the .llext + .gcno track edits without a full rebuild.
#    Idempotent: re-running rebuilds in place. If the build dir was initialized
#    for a *different* source tree (e.g. a prior build from another checkout or
#    worktree), `west build` refuses to reconfigure — fall back to a pristine
#    rebuild so the script succeeds regardless of any pre-existing build state.
echo "=== west build (mps2_an385) -> $BUILD_DIR ==="
if ! west build -b mps2_an385 -d "$BUILD_DIR" "$PRODUCT_DIR"; then
    echo "=== incremental build rejected the existing dir; retrying pristine ==="
    west build -p always -b mps2_an385 -d "$BUILD_DIR" "$PRODUCT_DIR"
fi

# 4. Strip the sections LLEXT 3.7 cannot relocate (-ENOEXEC on .init_array etc.);
#    cov_init calls the gcov constructor explicitly via an __asm__ alias instead.
echo "=== strip -> cov_ext.stripped.llext ==="
"$OBJCOPY" \
    --remove-section='.init_array*' --remove-section='.fini_array*' \
    --remove-section='.rel.init_array*' --remove-section='.rel.fini_array*' \
    --strip-debug \
    "$BUILD_DIR/zephyr/cov_ext.llext" "$BUILD_DIR/zephyr/cov_ext.stripped.llext"

echo "=== built $BUILD_DIR/zephyr/cov_ext.stripped.llext ==="
