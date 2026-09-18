#!/usr/bin/env bash
# Warnings-as-errors wrapper for web/'s npm scripts — the vite builds and
# the vitest suite (Chris, 2026-07-19): a warning is a failure, because a
# warning that only scrolls past in `make web`/`make docs`/`make coverage`
# output never gets fixed. This runs the given npm script, streams its output
# live, and FAILS if the npm script:
#
#   1. wrote ANYTHING to stderr (the captured stderr is reprinted in the
#      failure message), or
#   2. printed a "(!)" (vite/rollup) or " WARN " (rolldown) marker line on
#      stdout, in case a tool reports a warning there instead.
#
# Rule 1 is the primary one, and it is deliberately format-independent. The
# gate used to be rule 2 alone — a grep for the two markers over the combined
# log — and two warnings slipped straight through it (2026-09-18), both on
# stderr and neither carrying a marker:
#   - Vite's html plugin: `<script src="./cov_data/index.js"> in "/covapp.html"
#     can't be bundled without type="module" attribute`, via config.logger.warn
#     with no "(!)" prefix;
#   - Tailwind (@tailwindcss/node): "Found 1 warning while optimizing generated
#     CSS", a bare console.warn after its Lightning CSS pass that bypasses
#     Vite's logger entirely, so no Vite-side hook (customLogger/onwarn) could
#     ever have caught it.
# Scanning for known message shapes loses to the next tool with its own
# format; "the script writes nothing to stderr" does not. A clean run of each
# wrapped npm script writes zero stderr bytes.
#
# The chunk-size case specifically: vite.config.ts sets an explicit
# chunkSizeWarningLimit budget (see the comment there). Growth past that
# budget prints the "(!) Some chunks are larger" warning — on STDERR under
# Vite 8 (its builtin:vite-reporter), so rule 1 is what turns it into a build
# failure — the same pattern as the Python import-budget guard: the ceiling
# is enforced, and raising it requires a deliberate, reviewed edit.
#
# The vitest suite runs through this too. Its setup file already fails a test
# that calls console.warn/console.error, but that misses what happens outside
# a test body: process.emitWarning (Node deprecations, MaxListeners),
# beforeAll/afterAll output, and vitest's own messages. The any-stderr rule
# catches all of them unchanged.
#
# Lint (check, knip) and the coverage reporters (e2e:coverage-report,
# coverage:merged) run through it as well: every web/ npm script a Makefile
# gate calls, pinned by tests/unit/test_ci_web_gate.py.
#
# Usage: scripts/build_web_no_warnings.sh <npm-script>   (e.g. build,
# build:covapp, test, check). Run from the repo root (make does): it runs
# `npm run` in web/.
set -euo pipefail

SCRIPT="${1:?usage: build_web_no_warnings.sh <npm-script>}"

# npm's own update notice ("npm notice New major version ...") goes to stderr
# and depends on the registry, not on this build; keep it out of the gate.
export npm_config_update_notifier=false

OUT="$(mktemp)"
ERR="$(mktemp)"
trap 'rm -f "$OUT" "$ERR"' EXIT

# Split the streams without losing either from the terminal: inside the group,
# stderr goes into the pipe to `tee "$ERR" >&2` (captured, still shown on
# stderr) while stdout is parked on fd 3, which the outer pipe hands to
# `tee "$OUT"` (captured, still shown on stdout). Both tees are ordinary
# pipeline members, so bash waits for them — the files are complete once this
# line returns (process substitution would race that). pipefail makes the
# status the script's own when it fails; it is propagated below as-is. A
# non-zero status can also be a tee dying (SIGPIPE, 141, when whatever reads
# this script's output goes away), so the message names both, not just npm.
status=0
{ (cd web && npm run "$SCRIPT") 2>&1 1>&3 3>&- | tee "$ERR" >&2; } 3>&1 | tee "$OUT" || status=$?

if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "build_web_no_warnings: \`npm run $SCRIPT\` or the tee capturing its output failed (exit $status)." >&2
    exit "$status"
fi

if [ -s "$ERR" ]; then
    {
        echo ""
        echo "build_web_no_warnings: \`npm run $SCRIPT\` wrote to stderr. Any stderr"
        echo "output counts as a warning, and warnings are errors"
        echo "here. Fix the cause. The captured stderr:"
        echo "----"
        cat "$ERR"
        echo "----"
    } >&2
    exit 1
fi

if grep -qE '\(!\)| WARN ' "$OUT"; then
    echo "" >&2
    echo "build_web_no_warnings: \`npm run $SCRIPT\` printed warning(s) above —" >&2
    echo "matched '(!)' (vite/rollup) or ' WARN ' (rolldown) on stdout;" >&2
    echo "warnings are errors here. Fix the cause (for chunk-size: the budget" >&2
    echo "lives in web/vite.config.ts's chunkSizeWarningLimit and raising it" >&2
    echo "is a reviewed decision)." >&2
    exit 1
fi
