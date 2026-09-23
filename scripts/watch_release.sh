#!/usr/bin/env bash
# Spy on a `make release` that is already running in another terminal, and
# report its progress as a stream of one-line events.
#
# `make release` is a long (~40min) chain of gates that prints to the terminal
# that started it. This watcher observes it from the outside -- no shared fd,
# no cooperation from the release -- so you can follow it from another shell,
# a tmux pane, or an agent session. Usage:
#
#     scripts/watch_release.sh [PID]      # PID defaults to the running `make release`
#
# It emits three kinds of line:
#
#     STAGE: <target>        the chain advanced to a new gate
#     LANE green|RED: <f>    a junit file landed, with its parsed counts
#     DONE:|STOPPED:         the release exited, with or without a new tag
#
# Two traps shaped the implementation, and both produce a *confidently wrong*
# reading rather than an obvious failure, so don't "simplify" them back:
#
# 1. Stage markers must anchor to the start of a command line. The whole
#    release chain runs as ONE `/bin/sh -c` process whose argv quotes the
#    literal text of every stage -- including `bump-my-version` and `git-cliff`,
#    which have not run yet. An unanchored grep of the process table matches
#    that wrapper on the first poll and reports the last stage immediately.
#    (Same family as `pgrep -f` matching its own wrapper, different source:
#    here the false match comes from the watched process's argv.)
#
# 2. Splitting a line needs `sed`, not `tr`. `tr '>' '>\n'` is a silent no-op
#    -- tr maps char-to-char and discards the surplus replacement char -- which
#    leaves the junit XML as one long line, so a greedy capture reads the LAST
#    `time=` in the blob (a <testcase>'s) instead of the <testsuite>'s. It
#    reported an 841-test, 207s lane as taking 0.497s.
#
# A junit file is written when a lane ENDS, so LANE lines are completions, not
# starts; a lane that hangs or is killed produces no file at all. That gap is
# covered by the stage poll and by the exit branch, which always emits a final
# DONE/STOPPED line -- silence never means success.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JUNIT_DIR="$REPO/reports/junit"
POLL_SECONDS="${POLL_SECONDS:-20}"

# The gates of the `release` target, in the order the recipe chains them.
STAGES=(clean-dist web-install check-python release-matrix release-kgcov-matrix \
        docs nox web dashboard-all validate-ts profile wheel-check build)

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

REL_PID="${1:-}"
if [ -z "$REL_PID" ]; then
    # Match the top-level `make release` only: anchored, so the `/bin/sh -c`
    # recipe wrapper (which also contains the words) cannot win.
    REL_PID="$(ps -eo pid=,args= \
        | sed -n 's/^ *\([0-9]\+\) \+make release$/\1/p' | head -1)"
fi
if [ -z "$REL_PID" ] || ! kill -0 "$REL_PID" 2>/dev/null; then
    echo "watch_release: no running \`make release\` found -- pass its PID explicitly." >&2
    exit 1
fi

echo "watch_release: watching pid $REL_PID (poll ${POLL_SECONDS}s)"

# Pull one attribute off an XML element line. Anchored on a non-name character
# so `tests=` cannot be matched inside another attribute's name.
attr() {
    sed -n 's/.*[^_a-zA-Z]'"$1"'="\([^"]*\)".*/\1/p' <<<"$2" | head -1
}

# Lanes already on disk belong to an earlier run; record their mtimes so only
# files this run writes (new path, or rewritten path) become events.
declare -A seen
while IFS= read -r f; do
    seen["$f"]="$(stat -c %Y "$f" 2>/dev/null || echo 0)"
done < <(find "$JUNIT_DIR" -name '*.xml' 2>/dev/null)

emit_new_lanes() {
    local f mtime line tests fails errs skips secs verdict
    while IFS= read -r f; do
        mtime="$(stat -c %Y "$f" 2>/dev/null || echo 0)"
        if [ "${seen[$f]:-}" = "$mtime" ]; then
            continue
        fi
        seen["$f"]="$mtime"
        line="$(head -c 4000 "$f" | sed 's/>/>\n/g' | grep -m1 '<testsuite ' || true)"
        tests="$(attr tests "$line")"
        fails="$(attr failures "$line")"
        errs="$(attr errors "$line")"
        skips="$(attr skipped "$line")"
        secs="$(attr time "$line")"
        # An unparseable file is reported, not swallowed.
        if [ -z "$tests" ]; then
            echo "LANE ????: ${f#"$JUNIT_DIR"/}  (no <testsuite> element parsed)"
            continue
        fi
        if [ "${fails:-0}" != "0" ] || [ "${errs:-0}" != "0" ]; then
            verdict="RED"
        else
            verdict="green"
        fi
        echo "LANE $verdict: ${f#"$JUNIT_DIR"/}  tests=$tests failures=${fails:-?} errors=${errs:-?} skipped=${skips:-?} ${secs:-?}s"
    done < <(find "$JUNIT_DIR" -name '*.xml' 2>/dev/null | sort)
}

current_stage() {
    local procs="$1" s found=""
    for s in "${STAGES[@]}"; do
        if grep -qE "^make (--no-print-directory )?(-[a-zA-Z]+ )*$s( |\$)" <<<"$procs"; then
            found="$s"
        fi
    done
    # Post-gate phases, in order. Anchored -- see trap 1 in the header.
    if grep -qE "^(uv run python |[^ ]*/)?scripts/release_bump\.py" <<<"$procs"; then
        found="release_bump.py"
    fi
    if grep -qE "^[^ ]*/?git-cliff " <<<"$procs"; then found="git-cliff"; fi
    if grep -qE "^[^ ]*/?bump-my-version " <<<"$procs"; then found="bump-my-version"; fi
    printf '%s' "$found"
}

prev=""
while true; do
    emit_new_lanes

    if ! kill -0 "$REL_PID" 2>/dev/null; then
        cd "$REPO"
        tag="$(git tag --points-at HEAD | head -1)"
        dist="$(find dist/ -maxdepth 1 -type f ! -name '.*' -printf '%f ' 2>/dev/null)"
        if [ -n "$tag" ]; then
            echo "DONE: release finished -- tagged $tag; dist: ${dist:-<empty>}"
        else
            echo "STOPPED: \`make release\` exited with no new tag at HEAD" \
                 "(describe: $(git describe --tags 2>/dev/null || echo none);" \
                 "dist: ${dist:-<empty>}) -- likely a failed stage, last seen at: ${prev:-unknown}"
        fi
        exit 0
    fi

    cur="$(current_stage "$(ps -eo args=)")"
    if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then
        echo "STAGE: $cur  ($(date +%H:%M:%S))"
        prev="$cur"
    fi

    sleep "$POLL_SECONDS"
done
