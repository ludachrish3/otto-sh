#!/usr/bin/env bash
# Resolved-value gate for otto's brand violet vs Untitled UI's brand purple.
#
# Untitled UI's vendored theme.css (web/src/styles/theme.css) defines a FULL
# brand color ramp whose 500 is #9E77ED (purple); otto's own @theme block in
# web/src/app.css redeclares --color-brand-500 as #7c5cff (violet) and
# relies on being the LAST declaration to win. web/src/charts/palette.ts
# reads --color-brand-500 at runtime, so which one wins matters for real.
#
# web/src/__tests__/tokens.test.ts asserts SOURCE ORDER in app.css (our
# @theme block's text appears after the theme.css @import's text) -- that
# is blind to any wrong resolution NOT caused by import order (a duplicate
# declaration, a build-tool quirk, a future refactor that reorders things
# without touching the two lines that test greps for). This script instead
# asserts the RESOLVED value in the BUILT stylesheet: what a browser would
# actually compute for var(--color-brand-500), which is what
# charts/palette.ts actually sees.
#
# A vitest test can't do this reliably: `npx vitest run` alone never builds
# the dist (see check_airgap.sh's own note on the equivalent pytest
# landmine), so a unit test reading dist/ would pass or silently skip
# depending on whatever stale build happens to be lying around. This runs
# as a POST-BUILD gate instead (see the `web` Makefile target, alongside
# check_airgap.sh) -- it fails loudly, every time, right after the artifact
# it's checking is produced.
#
# Usage: scripts/check_brand_tokens.sh [dist-dir]  (default:
# src/otto/monitor/static/dist)
set -euo pipefail

DIST="${1:-src/otto/monitor/static/dist}"

if [ ! -d "$DIST" ]; then
    echo "check_brand_tokens: '$DIST' does not exist — run \`make web\` first." >&2
    exit 1
fi

shopt -s nullglob
CSS_FILES=("$DIST"/assets/*.css)
shopt -u nullglob
if [ "${#CSS_FILES[@]}" -eq 0 ]; then
    echo "check_brand_tokens: no built CSS under '$DIST/assets' — run \`make web\` first." >&2
    exit 1
fi

# Tailwind v4 merges every @theme block (ours and theme.css's) into ONE
# deduplicated custom-property declaration at build time -- there is
# exactly one --color-brand-500 in a correctly-built stylesheet, not two
# competing ones left for the browser's cascade to resolve. `tail -n1`
# is a safety net, not the load-bearing part of this check: if that
# assumption ever stops holding, the LAST declaration is still the one
# that wins in CSS cascade order.
resolved="$(grep -aho -- '--color-brand-500:[^;]*;' "${CSS_FILES[@]}" | tail -n1)"

if [ -z "$resolved" ]; then
    echo "check_brand_tokens: FAIL — '--color-brand-500' not found in built CSS under $DIST/assets." >&2
    exit 1
fi

if ! grep -qi '#7c5cff' <<<"$resolved"; then
    echo "check_brand_tokens: FAIL — --color-brand-500 resolved to '$resolved' in the built CSS, not otto's brand violet (#7c5cff). Untitled UI's purple is winning the @theme merge -- check web/src/app.css's @import order (theme.css must be imported BEFORE otto's own @theme block)." >&2
    exit 1
fi

if grep -qi '9e77ed' "${CSS_FILES[@]}"; then
    echo "check_brand_tokens: FAIL — Untitled UI's purple (#9E77ED) still appears in the built CSS under $DIST/assets. charts/palette.ts must never resolve it." >&2
    exit 1
fi

echo "check_brand_tokens: OK — --color-brand-500 resolves to otto's brand violet ($resolved) in the built CSS, and #9E77ED does not appear."
