#!/usr/bin/env bash
#
# Build the extension with the dev VM's toolchain: the bed's counterpart of the
# environment the track page tells a reader to set up.
#   bed-build.sh <embedded-gcov checkout with the patch applied>
set -euo pipefail

# The capture runner passes the otto it runs.
: "${OTTO:?set OTTO to the otto console script to build with}"
export OTTO
HERE="$(dirname "$(readlink -f "$0")")"
export ZEPHYR_WORKSPACE="$HOME/zephyrproject-v3_7"
export ZEPHYR_SDK_INSTALL_DIR="$HOME/zephyr-sdk-0.16.8"
export ZEPHYR_VENV="$HOME/zephyr-venv-v3_7"
export EMBEDDED_GCOV_DIR="${1:?usage: bed-build.sh <embedded-gcov checkout>}"

exec "$HERE/build.sh"
