#!/usr/bin/env bash
# Warnings-as-errors wrapper for the web/ vite builds (Chris, 2026-07-19):
# vite prints build warnings — chunk-size overruns, rollup/rolldown notices —
# with a "(!)" marker and exits 0, so they only ever scroll past in
# `make web`/`make coverage` output and nobody fixes them. This runs the
# given npm script, echoes its output unchanged, and FAILS if any "(!)"
# warning appeared.
#
# The chunk-size case specifically: vite.config.ts sets an explicit
# chunkSizeWarningLimit budget (see the comment there). Growth past that
# budget prints the "(!)" chunk warning, which this gate turns into a build
# failure — the same pattern as the Python import-budget guard: the ceiling
# is enforced, and raising it requires a deliberate, reviewed edit.
#
# Usage: scripts/build_web_no_warnings.sh <npm-script>   (e.g. build,
# build:covreport). Run from the repo root (make does).
set -euo pipefail

SCRIPT="${1:?usage: build_web_no_warnings.sh <npm-script>}"

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

# tee keeps the normal build output visible; pipefail propagates a failed
# build itself independently of the warning scan.
(cd web && npm run "$SCRIPT" 2>&1) | tee "$LOG"

if grep -q '(!)' "$LOG"; then
    echo "" >&2
    echo "build_web_no_warnings: vite emitted the warning(s) marked (!) above" >&2
    echo "during \`npm run $SCRIPT\` — warnings are errors here. Fix the cause" >&2
    echo "(for chunk-size: the budget lives in web/vite.config.ts's" >&2
    echo "chunkSizeWarningLimit and raising it is a reviewed decision)." >&2
    exit 1
fi
