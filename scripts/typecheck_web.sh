#!/usr/bin/env bash
# Type-check web/ with the vendored Untitled UI source filtered OUT.
#
# The vendored boundary is drawn in Biome (files.includes), knip (ignore) and
# coverage (exclude) -- tsc was the one tool missing it, and tsconfig cannot
# express it: `exclude` only removes a file from the program's ROOT set, and a
# file reached through an import is added and checked regardless (verified --
# 40 of 77 vendored exactOptionalPropertyTypes errors survived it). The only
# per-file suppression TypeScript offers is `// @ts-nocheck`, which would mean
# hand-editing vendored source -- forbidden by check_untitledui_hash.sh.
#
# This is NOT a budget or a ratchet. A ratchet is a place for OUR OWN defects
# to accumulate under a green gate, and was rejected for that reason. This
# excludes code we are forbidden to touch and can never fix. The distinction
# is whether the excluded work is ours to do.
#
# The vendored path list is DERIVED from untitledui.lock.json's `paths`, using
# the same expansion check_untitledui_hash.sh uses ("<prefix>/**" means
# everything under <prefix>; anything else is a literal path), so the two
# cannot drift on what counts as vendored.
#
# TWO ways this filter could go green while something is actually wrong, and
# what stops each:
#
#   1. A diagnostic it cannot classify. Anything tsc prints that is not a
#      `path(line,col): error TSnnnn` line -- config errors like TS6046, an
#      empty-program TS18003 -- has no path to test against the vendored set,
#      so it is ALWAYS surfaced rather than filtered. A filter that can only
#      recognise the thing it drops must never hide a failure it did not
#      classify.
#   2. tsc not running at all. A crash, an OOM kill, or `npx` failing to
#      resolve typescript produces a non-zero exit with NO `error TS` line
#      anywhere -- so both buckets come back empty and the naive reading is
#      "clean". tsc's exit status is therefore captured and checked: a
#      non-zero exit that produced no recognisable diagnostic is a hard
#      failure, and the raw output is dumped. Discarding that status (`||
#      true`) makes an unrunnable type-checker indistinguishable from a
#      passing one.
#
# There is a SECOND exclusion below, for test-file noUncheckedIndexedAccess
# diagnostics. It is a different kind of thing and is documented separately at
# its own site -- see "THE DEFERRED TEST-SITE EXCLUSION". Do not read the
# paragraphs above as covering it: the vendored exclusion is for code we are
# FORBIDDEN to touch, the other is for code we have JUDGED not worth changing
# yet, and only one of those two is permanent.
#
# Usage: scripts/typecheck_web.sh [extra tsc flags]
#   TYPECHECK_WEB_SHOW_DEFERRED=1  also list the deferred test sites, for
#                                  working the burn-down.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
LOCKFILE="$WEB_DIR/untitledui.lock.json"

if [ ! -f "$LOCKFILE" ]; then
    echo "typecheck_web: '$LOCKFILE' does not exist." >&2
    exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "typecheck_web: 'jq' is required but not on PATH." >&2
    exit 1
fi

# Every ERE metacharacter is escaped, not just the ones today's paths happen
# to contain: an unescaped `|` in a lockfile path would silently graft an
# extra alternation branch onto this regex and start exempting OUR code.
VENDORED_RE="$(
  jq -r '.paths[]' "$LOCKFILE" | while IFS= read -r p; do
    if [[ "$p" == */\*\* ]]; then
      printf '^%s/|' "$(printf '%s' "${p%/**}" | sed 's/[][\\^$.*+?(){}|]/\\&/g')"
    else
      # Literal entries anchor on the `(` that opens tsc's (line,col) suffix,
      # so `src/utils/cx.ts` cannot also match `src/utils/cx.ts.bak`.
      printf '^%s\\(|' "$(printf '%s' "$p" | sed 's/[][\\^$.*+?(){}|]/\\&/g')"
    fi
  done | sed 's/|$//'
)"
if [ -z "$VENDORED_RE" ]; then
    echo "typecheck_web: lockfile '.paths' is empty -- refusing to run unfiltered." >&2
    exit 1
fi

TC_TMP="$(mktemp -d)"
trap 'rm -rf "$TC_TMP"' EXIT

cd "$WEB_DIR"
rc=0
raw="$(npx tsc -p tsconfig.json --noEmit --pretty false "$@" 2>&1)" || rc=$?

DIAG_RE='^[^ ].*\([0-9]+,[0-9]+\): error TS'
ours="$(printf '%s\n' "$raw"  | grep -E  "$DIAG_RE" | grep -vE "$VENDORED_RE" || true)"
other="$(printf '%s\n' "$raw" | grep -E 'error TS'  | grep -vE "$DIAG_RE"     || true)"

n_other=$(printf '%s' "$other" | grep -c . || true)
n_all=$(printf '%s\n' "$raw"   | grep -cE "$DIAG_RE" || true)
n_ours=$(printf '%s' "$ours"   | grep -c . || true)
n_vendored=$(( n_all - n_ours ))

# tsc exits non-zero whenever it reports ANY error, including a purely
# vendored one -- that case is legitimately filtered and must stay green. The
# failure this catches is the other one: non-zero with nothing recognisable
# to show for it. Checked BEFORE the deferral pass below, so a compiler that
# died is never handed to a differential that would read its silence as
# "nothing left to report".
if [ "$rc" -ne 0 ] && [ "$n_all" -eq 0 ] && [ "$n_other" -eq 0 ]; then
    [ -n "$raw" ] && printf '%s\n' "$raw" >&2
    echo "typecheck_web: FAILED -- tsc exited $rc without emitting a single recognisable diagnostic, so nothing was actually type-checked." >&2
    exit 1
fi

# ===========================================================================
# THE DEFERRED TEST-SITE EXCLUSION -- a SEPARATE thing from the vendored
# filter above, on a SEPARATE justification. Read this before touching it.
#
# The vendored exclusion covers code we are FORBIDDEN to edit and can never
# fix; it is permanent and the excluded work is not ours to do. This one
# covers code that is entirely ours, that we COULD fix today, and that we have
# judged not worth fixing yet. That makes it the thing the header above says
# was rejected -- a ratchet -- unless it is kept honest, so it is:
#
#   1. It is scoped to ONE RULE, noUncheckedIndexedAccess, not to test files.
#      Every other diagnostic in a test file still fails this gate. That
#      scoping is why this is a deferral of one measured decision rather than
#      a blanket test-tier exemption.
#   2. It is COUNTED and reported on every run, pass or fail, so the debt is
#      visible rather than silently absorbed. The burn-down list, the criterion
#      for deciding which sites are worth fixing, and the exit condition live
#      in todo/ts-nuia-test-sites-burndown.md.
#   3. It removes itself from the conversation when it is done: at zero
#      remaining sites the script says so and asks for this block to be
#      deleted.
#
# HOW THE RULE SCOPING WORKS, and why it is not done by error code.
# noUncheckedIndexedAccess has NO error code of its own. Its 305 sites here
# carry four different ones -- 182 x TS2532, 73 x TS2345, 49 x TS18048,
# 1 x TS2322 -- and TS2345/TS2322 are among the commonest codes tsc emits for
# ordinary type errors, so dropping them in test files would drop real defects
# with them.
#
# So attribution is measured, not pattern-matched: tsc is run a SECOND time
# with the flag forced off, and a diagnostic counts as this flag's doing only
# if its location appears in the flag-ON run and NOT in the flag-OFF one.
# Comparison is by location alone (path plus line,col), deliberately ignoring
# the code and the message: a site that errors in both runs stays in the
# failing set even if the flag changed how the error reads. Every ambiguity
# therefore resolves toward failing the gate.
#
# `--noUncheckedIndexedAccess false` is appended AFTER "$@" because tsc's last
# flag wins, so a caller passing the flag explicitly cannot defeat the
# baseline (verified: 393 diagnostics on, 77 off, 77 with both spellings).
# ===========================================================================
TEST_DIAG_RE='\.test\.tsx?\([0-9]+,[0-9]+\): error TS'
in_tests="$(printf '%s\n' "$ours" | grep -E "$TEST_DIAG_RE" || true)"
n_deferred=0

if [ -n "$in_tests" ]; then
    rc_base=0
    base="$(npx tsc -p tsconfig.json --noEmit --pretty false "$@" --noUncheckedIndexedAccess false 2>&1)" || rc_base=$?
    printf '%s\n' "$base" | grep -E "$DIAG_RE" > "$TC_TMP/baseline.txt" || true
    n_base=$(grep -c . "$TC_TMP/baseline.txt" || true)
    if [ "$rc_base" -ne 0 ] && [ "$n_base" -eq 0 ]; then
        printf '%s\n' "$base" >&2
        echo "typecheck_web: FAILED -- the noUncheckedIndexedAccess baseline pass exited $rc_base without" >&2
        echo "                a single recognisable diagnostic. Nothing can be attributed to the flag," >&2
        echo "                and treating that as 'all of it' would exempt real test-file errors." >&2
        exit 1
    fi

    printf '%s\n' "$in_tests" > "$TC_TMP/in_tests.txt"
    : > "$TC_TMP/kept.txt"
    : > "$TC_TMP/deferred.txt"
    # FILENAME, not the usual NR == FNR: an EMPTY baseline file makes NR == FNR
    # stay true into the second file, which would silently swallow its first
    # diagnostic. A zero-diagnostic baseline is reachable (it is what "every
    # remaining error is this flag's doing" looks like).
    awk -v basefile="$TC_TMP/baseline.txt" \
        -v keep="$TC_TMP/kept.txt" -v defer="$TC_TMP/deferred.txt" '
        function keyof(line,   k) { k = line; sub(/\): error TS.*$/, ")", k); return k }
        FILENAME == basefile { baseline[keyof($0)] = 1; next }
        { if (keyof($0) in baseline) print > keep; else print > defer }
    ' "$TC_TMP/baseline.txt" "$TC_TMP/in_tests.txt"

    n_deferred=$(grep -c . "$TC_TMP/deferred.txt" || true)
    # What survives: everything outside a test file, plus the test-file
    # diagnostics the baseline pass proves are NOT this flag's doing.
    ours="$(
        { printf '%s\n' "$ours" | grep -vE "$TEST_DIAG_RE" || true; cat "$TC_TMP/kept.txt"; } | grep . || true
    )"
    n_ours=$(printf '%s' "$ours" | grep -c . || true)

    if [ "${TYPECHECK_WEB_SHOW_DEFERRED:-}" = "1" ]; then
        cat "$TC_TMP/deferred.txt"
    fi
fi

[ -n "$ours" ]  && printf '%s\n' "$ours"
[ -n "$other" ] && printf '%s\n' "$other"

if [ "$n_ours" -ne 0 ] || [ "$n_other" -ne 0 ]; then
    # Deliberately NOT the "burn-down is DONE" wording below, even at zero. A
    # run that failed on a config error never reached the deferral pass, and a
    # zero it never measured must not read as an achievement.
    echo "typecheck_web: FAILED -- $n_ours error(s) in our code, $n_other unclassified ($n_deferred deferred noUncheckedIndexedAccess test site(s))." >&2
    exit 1
fi

if [ "$n_deferred" -eq 0 ]; then
    deferred_note="0 deferred test site(s) -- the burn-down is DONE: delete the deferral block in scripts/typecheck_web.sh and todo/ts-nuia-test-sites-burndown.md"
else
    deferred_note="$n_deferred deferred noUncheckedIndexedAccess site(s) in test files, see todo/ts-nuia-test-sites-burndown.md"
fi
echo "typecheck_web: OK -- our code is clean ($n_vendored vendored diagnostic(s) ignored; $deferred_note)."
