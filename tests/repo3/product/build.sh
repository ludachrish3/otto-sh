#!/usr/bin/env bash
#
# Build the repo3 LLEXT coverage extension (cov_ext.stripped.llext) into the
# given build dir. This is the single command the TestEmbeddedCoverage suite
# runs to keep the product up to date before loading it — the embedded analogue
# of repo1's `make -C product clean all`.
#
# Usage:
#   build.sh <build-dir> [zver]     # e.g. build.sh ~/build/cov_ext_app v3_7
#
#   zver is v2_7 | v3_7 | v4_4 (default v3_7). May also be set via ZVER=.
#   The venv, workspace, SDK, and board are all auto-selected from zver; each
#   can be overridden individually via the env vars below.
#
# Env overrides (default to the dev VM's Vagrant-provisioned locations):
#   ZVER                     Zephyr version tag                (v3_7)
#   ZEPHYR_VENV              python venv with west            (~/zephyr-venv-<zver>)
#   ZEPHYR_WORKSPACE         the Zephyr west workspace        (~/zephyrproject-<zver>)
#   ZEPHYR_SDK_INSTALL_DIR   the Zephyr SDK                   (~/zephyr-sdk-<ver>)
#   BOARD                    board name passed to west build   (mps2_an385 or mps2/an385)
#
# SDK layout note: SDK 0.16.8 (used for 2.7/3.7) has a flat layout:
#   arm-zephyr-eabi/bin/arm-zephyr-eabi-objcopy
# SDK 1.0.1 (used for 4.4) has an extra gnu/ segment:
#   gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-objcopy
# The objcopy path is resolved with `find` to handle both layouts.
#
# Recipe and the why-behind-each-step live in ./README.md and ../docs/feasibility.md.
set -euo pipefail

BUILD_DIR="${1:?usage: build.sh <build-dir> [zver]}"

ZVER="${ZVER:-v3_7}"            # v2_7 | v3_7 | v4_4 ; or pass as 2nd positional arg
[ "${2:-}" != "" ] && ZVER="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCT_DIR="$SCRIPT_DIR"                       # tests/repo3/product
REPO3_DIR="$(dirname "$SCRIPT_DIR")"            # tests/repo3
TOPLEVEL="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SUBMODULE="$REPO3_DIR/third_party/embedded-gcov"
PATCH="$REPO3_DIR/third_party/patches/embedded-gcov-zephyr-gcc12plus.patch"

ZEPHYR_VENV="${ZEPHYR_VENV:-$HOME/zephyr-venv-${ZVER}}"
ZEPHYR_WORKSPACE="${ZEPHYR_WORKSPACE:-$HOME/zephyrproject-${ZVER}}"
case "${ZVER}" in
    v4_4) ZSDK="${ZSDK:-1.0.1}"  ;;
    *)    ZSDK="${ZSDK:-0.16.8}" ;;
esac
ZEPHYR_SDK_INSTALL_DIR="${ZEPHYR_SDK_INSTALL_DIR:-$HOME/zephyr-sdk-${ZSDK}}"
export ZEPHYR_SDK_INSTALL_DIR

case "${ZVER}" in
    v4_4) BOARD="${BOARD:-mps2/an385}" ;;
    *)    BOARD="${BOARD:-mps2_an385}" ;;
esac

# `|| true` keeps `set -e`/`pipefail` from aborting before the guard below can
# fire its actionable message (e.g. a missing/wrong SDK dir makes `find` exit 1).
OBJCOPY="$(find "$ZEPHYR_SDK_INSTALL_DIR" -maxdepth 4 -type f -name 'arm-zephyr-eabi-objcopy' 2>/dev/null | head -1)" || true
if [ -z "$OBJCOPY" ]; then
    echo "ERROR: arm-zephyr-eabi-objcopy not found under $ZEPHYR_SDK_INSTALL_DIR" >&2
    exit 1
fi

# 1. embedded-gcov submodule + gcc-12+ patch (idempotent). The runtime is NASA's
#    embedded-gcov (a submodule otto doesn't own); the patch is `#if __GNUC__>=12`
#    gated, so it covers gcc 12 through 14 (3.7's gcc 12.2 and 4.4's gcc 14.3).
if [ ! -e "$SUBMODULE/code" ]; then
    echo "=== embedded-gcov: initializing submodule ==="
    git -C "$TOPLEVEL" submodule update --init -- "${SUBMODULE#"$TOPLEVEL"/}"
fi
if git -C "$SUBMODULE" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "=== embedded-gcov: gcc-12+ patch already applied ==="
else
    echo "=== embedded-gcov: applying gcc-12+ patch ==="
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
#
#    Force the LLEXT link tail to track the (re)compiled object. Zephyr's LLEXT
#    codegen lists cov_ext.c.obj only as an *order-only* ninja dep of the
#    generated extension (llext/cov_ext_debug.elf is a bare `cmake -E copy` of the
#    object, named only inside the command string, not as a build input), so an
#    incremental recompile updates cov_ext.c.{obj,gcno} with a fresh gcov stamp
#    but never re-links cov_ext.llext — the stale extension's stamp then mismatches
#    the new .gcno and `otto cov report` dies in geninfo with "stamp mismatch with
#    notes file" (0% coverage). Removing the link-tail outputs makes ninja
#    regenerate them from the current object, since it always rebuilds a *missing*
#    output regardless of order-only input timestamps. Cheap: a copy + the strip
#    below, not a full Zephyr rebuild.
rm -f "$BUILD_DIR/llext/cov_ext_debug.elf" "$BUILD_DIR/zephyr/cov_ext.llext"
echo "=== west build ($BOARD) -> $BUILD_DIR ==="
if ! west build -b "$BOARD" -d "$BUILD_DIR" "$PRODUCT_DIR"; then
    echo "=== incremental build rejected the existing dir; retrying pristine ==="
    west build -p always -b "$BOARD" -d "$BUILD_DIR" "$PRODUCT_DIR"
fi

# 4. Strip the sections LLEXT 3.7 cannot relocate (-ENOEXEC on .init_array etc.);
#    cov_init calls the gcov constructor explicitly via an __asm__ alias instead.
echo "=== strip -> cov_ext.stripped.llext ==="
"$OBJCOPY" \
    --remove-section='.init_array*' --remove-section='.fini_array*' \
    --remove-section='.rel.init_array*' --remove-section='.rel.fini_array*' \
    --strip-debug \
    "$BUILD_DIR/zephyr/cov_ext.llext" "$BUILD_DIR/zephyr/cov_ext.stripped.llext"

# 5. Coherence guard (defense in depth). The extension we load and the .gcno used
#    to decode its .gcda must share a gcov stamp, or geninfo fails late with
#    "stamp mismatch with notes file" and silently reports 0% coverage. The stamp
#    is a 32-bit word: in the .gcno it sits at byte offset 8; in the (stripped)
#    extension it sits in gcov_info, two words past the gcov version marker (the
#    .gcno's bytes[4:8]). Verify they agree here so a build-graph regression
#    surfaces as an actionable build error instead of a silent miscount.
echo "=== verify gcov stamp: cov_ext.stripped.llext vs cov_ext.c.gcno ==="
python3 - "$BUILD_DIR" <<'PY'
import glob, struct, sys, pathlib
bd = pathlib.Path(sys.argv[1])
gcnos = glob.glob(str(bd / "CMakeFiles" / "*" / "src" / "cov_ext.c.gcno"))
if not gcnos:
    sys.exit("coherence guard: no cov_ext.c.gcno under %s" % bd)
gcno = pathlib.Path(gcnos[0]).read_bytes()
ver, stamp_notes = gcno[4:8], struct.unpack_from("<I", gcno, 8)[0]
blob = (bd / "zephyr" / "cov_ext.stripped.llext").read_bytes()
stamps = [struct.unpack_from("<I", blob, i + 8)[0]
          for i in range(len(blob) - 12) if blob[i:i + 4] == ver]
if not stamps:
    sys.exit("coherence guard: no gcov_info (version %r) found in stripped.llext" % ver)
if stamp_notes not in stamps:
    sys.exit("coherence guard FAILED: .gcno stamp %#x not in loaded extension "
             "stamps %s — the shipped binary does not match the notes (stale link)."
             % (stamp_notes, [hex(s) for s in stamps]))
print("coherence guard OK: stamp %#x matches" % stamp_notes)
PY

echo "=== built $BUILD_DIR/zephyr/cov_ext.stripped.llext ==="
