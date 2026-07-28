#!/usr/bin/env bash
# Never-hand-edited gate for web/'s vendored (copy-in) Untitled UI source.
#
# web/README.md's vendored-source section states the rule this enforces:
# never hand-edit web/src/components/** (and the other paths
# web/untitledui.lock.json lists). Untitled UI ships as copy-in source, so
# the only way to tell "upstream changed" from "we edited it" is for our
# tree to be byte-identical to what the CLI emits. A single hand-edit
# destroys that property permanently.
#
# This is the CHEAP HALF of a two-part contract with
# scripts/check_untitledui_drift.sh:
#
#   check_untitledui_drift.sh  — re-vendors with the pinned CLI and diffs.
#       Answers "did UPSTREAM change?". Needs network, takes minutes, so it
#       runs weekly (.github/workflows/untitledui-drift.yml).
#   check_untitledui_hash.sh   — this script. Recomputes the tree's own
#       fingerprint and compares it to the lockfile's recorded contentHash.
#       Answers "did WE change it?". No network, sub-second, so it runs on
#       every push as part of `make check-ts`.
#
# Splitting it this way matters because the drift check CANNOT distinguish
# the two causes: it is a one-directional content diff, so a local hand-edit
# shows up there as "upstream drift" — under a title naming the wrong
# culprit — on every run, forever, until someone reverts it by hand. That is
# not hypothetical: issue #177 was exactly this (a z-50 class hand-added to
# slideout-menu.tsx in fe928c75), and it surfaced six days later, in a
# weekly cron, as a third-party problem. This script fails instead at the
# commit that introduces the edit, while the author still has the context to
# fix it properly (reconcile on OUR side — see web/README.md).
#
# The file list is DERIVED from the lockfile's `paths`, not hardcoded here,
# so it cannot drift from the set check_untitledui_drift.sh compares. The
# expansion matches that script's: a `<prefix>/**` entry means every file
# under <prefix>; anything else is a literal path. That reproduces the
# `contentHash.recipe` recorded in the lockfile.
#
# When this FAILS after a deliberate re-vendor (the legitimate case), the
# fix is to update untitledui.lock.json's contentHash.value (and
# vendoredAt) in the SAME commit as the re-vendored files -- not to weaken
# this check.
#
# Usage: scripts/check_untitledui_hash.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
LOCKFILE="$WEB_DIR/untitledui.lock.json"

if [ ! -f "$LOCKFILE" ]; then
    echo "check_untitledui_hash: '$LOCKFILE' does not exist." >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "check_untitledui_hash: 'jq' is required but not on PATH." >&2
    exit 1
fi

ALGORITHM="$(jq -r '.contentHash.algorithm' "$LOCKFILE")"
EXPECTED="$(jq -r '.contentHash.value' "$LOCKFILE")"
VENDORED_AT="$(jq -r '.vendoredAt' "$LOCKFILE")"
mapfile -t VENDOR_PATHS < <(jq -r '.paths[]' "$LOCKFILE")

if [ "$ALGORITHM" != "sha256" ]; then
    echo "check_untitledui_hash: lockfile's contentHash.algorithm is '$ALGORITHM', but this script only implements sha256." >&2
    exit 1
fi
if [ -z "$EXPECTED" ] || [ "$EXPECTED" = "null" ]; then
    echo "check_untitledui_hash: '$LOCKFILE' has no .contentHash.value." >&2
    exit 1
fi
if [ "${#VENDOR_PATHS[@]}" -eq 0 ]; then
    echo "check_untitledui_hash: '$LOCKFILE' lists no .paths." >&2
    exit 1
fi

cd "$WEB_DIR"

# Validate every lockfile path up front, in THIS shell. Doing it inside the
# hashing pipeline below would put the check in a subshell, where `exit 1`
# only kills that subshell -- the script's real exit would then depend on a
# subtle set -e/pipefail interaction, and a missing path would surface as a
# confusing hash mismatch instead of the precise message here.
for path_spec in "${VENDOR_PATHS[@]}"; do
    if [[ "$path_spec" == */\*\* ]]; then
        prefix="${path_spec%/**}"
        if [ ! -d "$prefix" ]; then
            echo "check_untitledui_hash: vendored dir '$prefix' (from the lockfile's .paths) does not exist under web/." >&2
            exit 1
        fi
    elif [ ! -f "$path_spec" ]; then
        echo "check_untitledui_hash: vendored file '$path_spec' (from the lockfile's .paths) does not exist under web/." >&2
        exit 1
    fi
done

# Build the file list exactly as contentHash.recipe describes: glob entries
# expand to every file beneath them, literal entries pass through, then the
# whole list is sorted under a fixed collation before hashing. LC_ALL=C
# keeps the ordering (and so the digest) identical across machines whose
# locales would otherwise sort differently.
list_files() {
    local path_spec
    for path_spec in "${VENDOR_PATHS[@]}"; do
        if [[ "$path_spec" == */\*\* ]]; then
            find "${path_spec%/**}" -type f
        else
            echo "$path_spec"
        fi
    done
}

actual="$(list_files | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"

if [ "$actual" != "$EXPECTED" ]; then
    echo "check_untitledui_hash: FAIL — web/'s vendored Untitled UI tree no longer matches the contentHash recorded in untitledui.lock.json." >&2
    echo >&2
    printf '  %-36s %s\n' "expected (lockfile, $VENDORED_AT):" "$EXPECTED" >&2
    printf '  %-36s %s\n' "actual (this tree):" "$actual" >&2
    echo >&2
    echo "These files are copy-in vendored source and must stay byte-identical to what the untitledui CLI emits -- web/README.md's never-hand-edit rule. A hand-edit here also makes scripts/check_untitledui_drift.sh report your change as UPSTREAM drift, forever (see issue #177)." >&2
    echo >&2
    echo "If you hand-edited a vendored file: revert it and reconcile on OUR side instead (pass a className from the call site, or add to web/src/ui/**) -- web/README.md's vendored-source section has the worked example." >&2
    echo "If you deliberately re-vendored: update untitledui.lock.json's contentHash.value (and vendoredAt) in the same commit. Recompute with the recipe recorded there under contentHash.recipe." >&2
    echo >&2
    echo "To see WHICH files moved: git status --short web/src/components (and the other paths listed in the lockfile)." >&2
    exit 1
fi

echo "check_untitledui_hash: OK — web/'s vendored Untitled UI tree matches untitledui.lock.json's contentHash (vendored $VENDORED_AT)."
