#!/usr/bin/env bash
# Untitled UI upstream drift check for web/'s vendored (copy-in) component
# source.
#
# Untitled UI ships as copy-in source, not an npm dependency: `npx
# untitledui add <name>` copies .tsx files straight into our tree, where
# they become our files — no version, no manifest, no lockfile entry.
# Dependabot's npm//web entry (.github/dependabot.yml) already covers every
# ordinary npm package Untitled UI pulls in, but it cannot see this: there
# is nothing to resolve, so nothing to bump. An upstream fix to their
# Select would never reach us, and nothing would tell us it exists. This
# script is the substitute: re-vendor the same component list with the
# pinned CLI (web/untitledui.lock.json) into a throwaway project, then diff
# the result against our committed tree.
#
# Pinning the CLI version does NOT pin the vendored content. The CLI is
# just a downloader; the component source itself is fetched from Untitled
# UI's live registry at run time, so the same pinned CLI version can (and
# does) serve different content on different days. This check therefore
# compares CONTENT, not versions — drift means "the registry served
# something different than what we have", not "the CLI is stale". Some
# drift is expected over time; detecting it is the entire point of this
# script, not a failure of it.
#
# Framework detection is load-bearing. Untitled UI's registry emits a
# Next.js App Router-flavored variant (a `"use client";` directive
# prepended to every component file) unless the target project looks like
# a Vite project — and it decides that by the presence of a vite.config.ts,
# not by inspecting package.json's dependencies. Re-vendoring into a bare
# directory that only has a package.json is easy to do by accident and
# silently produces the wrong flavor: every component would then show
# permanent, unfixable-by-design "drift" that is really just a harness bug
# (web/ is a Vite project; it never wants `"use client";`). This script
# copies web/'s real vite.config.ts (alongside package.json/tsconfig.json)
# into the throwaway project for exactly this reason — verified by hand
# that doing so reproduces our committed tree byte-for-byte when there is
# no real upstream drift. Do not drop that copy step to "simplify" this
# script; doing so reintroduces a false positive on every single run.
#
# Needs network access (npx fetches from the Untitled UI registry and npm
# installs each component's runtime deps into the throwaway project) — this
# is a dev/CI tooling script, not part of the built dashboard, so it is not
# subject to scripts/check_airgap.sh's air-gap requirement (that check
# gates the shipped bundle's runtime fetches, not build-time tooling).
#
# The throwaway project lives entirely under a mktemp directory and is
# removed on exit; this script never writes into web/ (the CLI mutates
# package.json — loosening exact pins to carets — and installs
# node_modules wherever it runs, so it must never run against web/ itself).
#
# Exit codes are load-bearing for untitledui-drift.yml, which must not file
# an "Untitled UI upstream drift" issue over a tooling/network failure (an
# npm registry blip looking like upstream drift is exactly the bug this
# contract fixes):
#   0 — no drift: every vendored file matches a fresh re-vendor.
#   1 — the check itself failed (bad lockfile, missing dependency, or the
#       CLI's `init`/`add` erroring out — e.g. a network blip). Not a
#       verdict on drift either way.
#   2 — genuine drift: the re-vendor completed and found a content or
#       missing-file mismatch.
#
# Usage: scripts/check_untitledui_drift.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
LOCKFILE="$WEB_DIR/untitledui.lock.json"

if [ ! -f "$LOCKFILE" ]; then
    echo "check_untitledui_drift: '$LOCKFILE' does not exist." >&2
    exit 1
fi

for bin in jq npx diff; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "check_untitledui_drift: '$bin' is required but not on PATH." >&2
        exit 1
    fi
done

CLI_SPEC="$(jq -r '.cli' "$LOCKFILE")"
VENDORED_AT="$(jq -r '.vendoredAt' "$LOCKFILE")"
mapfile -t COMPONENTS < <(jq -r '.components[]' "$LOCKFILE")
mapfile -t VENDOR_PATHS < <(jq -r '.paths[]' "$LOCKFILE")

if [ -z "$CLI_SPEC" ] || [ "$CLI_SPEC" = "null" ]; then
    echo "check_untitledui_drift: '$LOCKFILE' has no .cli entry." >&2
    exit 1
fi
if [ "${#COMPONENTS[@]}" -eq 0 ]; then
    echo "check_untitledui_drift: '$LOCKFILE' lists no .components." >&2
    exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

PROJECT_DIR="$TMP_ROOT/project"
mkdir -p "$PROJECT_DIR/src"

# Just enough of web/'s own project config for the CLI to make the same
# decisions it would make if run inside web/ itself (see framework-
# detection note above). src/ starts empty on purpose: nothing should
# pre-exist for `add`/`init` to consider already in place and skip.
cp "$WEB_DIR/package.json" "$WEB_DIR/tsconfig.json" "$WEB_DIR/vite.config.ts" "$PROJECT_DIR/"

echo "check_untitledui_drift: re-vendoring ${#COMPONENTS[@]} component(s) with $CLI_SPEC into a throwaway project (this needs network)..."

INIT_LOG="$TMP_ROOT/init.log"
ADD_LOG="$TMP_ROOT/add.log"

# `init` is the only way to fetch src/styles/theme.css; `add` alone never
# writes it. It also drops Next.js-flavored scaffolding (postcss config,
# globals.css, ...) we don't vendor and don't compare below — only
# src/styles/theme.css from this step feeds into the diff.
if ! (cd "$PROJECT_DIR" && npx "$CLI_SPEC" init --yes --vite) >"$INIT_LOG" 2>&1; then
    echo "check_untitledui_drift: 'npx $CLI_SPEC init' failed:" >&2
    cat "$INIT_LOG" >&2
    exit 1
fi

if ! (cd "$PROJECT_DIR" && npx "$CLI_SPEC" add "${COMPONENTS[@]}" --yes) >"$ADD_LOG" 2>&1; then
    echo "check_untitledui_drift: 'npx $CLI_SPEC add' failed:" >&2
    cat "$ADD_LOG" >&2
    exit 1
fi

# Compare every file the manifest says we vendored against the fresh
# re-vendor. This is deliberately one-directional: files the fresh
# re-vendor produces that we do NOT have (e.g. date-picker.tsx and
# date-range-picker.tsx, which `add range-calendar`/`add date-picker` both
# also emit as bundle siblings, but which we never kept — see
# 5eaeb03's commit message) are not drift, they're files we chose not to
# vendor, and are not reported. A file we DO have that a fresh re-vendor
# no longer produces at all is real drift (removed/renamed upstream) and
# is reported as "missing" below, same as a content mismatch.
offenders=()
missing=()
compared=0

for path_spec in "${VENDOR_PATHS[@]}"; do
    if [[ "$path_spec" == */\*\* ]]; then
        prefix="${path_spec%/**}"
        while IFS= read -r -d '' f; do
            rel="${f#"$WEB_DIR"/}"
            fresh="$PROJECT_DIR/$rel"
            compared=$((compared + 1))
            if [ ! -f "$fresh" ]; then
                missing+=("$rel")
            elif ! diff -q "$f" "$fresh" >/dev/null 2>&1; then
                offenders+=("$rel")
            fi
        done < <(find "$WEB_DIR/$prefix" -type f -print0 | sort -z)
    else
        rel="$path_spec"
        fresh="$PROJECT_DIR/$rel"
        compared=$((compared + 1))
        if [ ! -f "$fresh" ]; then
            missing+=("$rel")
        elif ! diff -q "$WEB_DIR/$rel" "$fresh" >/dev/null 2>&1; then
            offenders+=("$rel")
        fi
    fi
done

if [ "${#missing[@]}" -gt 0 ] || [ "${#offenders[@]}" -gt 0 ]; then
    echo "check_untitledui_drift: FAIL — upstream Untitled UI content has changed since $VENDORED_AT (re-vendored with the pinned $CLI_SPEC):" >&2
    if [ "${#missing[@]}" -gt 0 ]; then
        echo >&2
        echo "Vendored file(s) a fresh re-vendor no longer produces at all (removed/renamed upstream):" >&2
        printf '  %s\n' "${missing[@]}" >&2
    fi
    if [ "${#offenders[@]}" -gt 0 ]; then
        echo >&2
        echo "Vendored file(s) whose content changed upstream:" >&2
        for rel in "${offenders[@]}"; do
            echo >&2
            echo "--- $rel ---" >&2
            diff -u "$WEB_DIR/$rel" "$PROJECT_DIR/$rel" >&2 || true
        done
    fi
    echo >&2
    echo "This can be genuine upstream drift, not a bug in this check — Untitled UI's registry is live, so a pinned CLI version does not pin its output (see web/untitledui.lock.json's note). A human should review the diff above and decide whether/how to re-vendor; never auto-apply, since re-vendoring can change class names and markup that call sites depend on." >&2
    # Exit 2, not 1: this is genuine DRIFT (the re-vendor ran fine and found
    # a content mismatch), distinct from every exit 1 above (a lockfile/
    # tooling problem, or `init`/`add` itself failing — e.g. a network
    # blip). untitledui-drift.yml's workflow branches on this so a tooling
    # failure fails the job loudly instead of opening a misleading "Untitled
    # UI upstream drift" issue over what was really a registry timeout.
    exit 2
fi

echo "check_untitledui_drift: OK — $compared vendored file(s) match a fresh re-vendor with $CLI_SPEC."
