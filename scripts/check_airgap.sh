#!/usr/bin/env bash
# Air-gap gate for the built React monitor dashboard (src/otto/monitor/static/dist/).
# otto runs in air-gapped labs, so the dashboard must never depend on a
# CDN/font/analytics fetch at runtime. This greps the built JS/CSS/HTML for any
# absolute http(s) URL and fails if one shows up that isn't on the allowlist
# below. Usage: scripts/check_airgap.sh [dist-dir]  (default:
# src/otto/monitor/static/dist)
#
# Extraction uses `grep -o` (one match per line of output) rather than
# whole-line filtering: vite/react bundle output packs many logically distinct
# strings onto a handful of physical lines, so filtering whole lines against
# the allowlist could let a genuinely bad URL hide on the same line as an
# allowlisted one. Matching per-URL avoids that.
set -euo pipefail

DIST="${1:-src/otto/monitor/static/dist}"

if [ ! -d "$DIST" ]; then
    echo "check_airgap: '$DIST' does not exist — run \`make web\` first." >&2
    exit 1
fi

# Allowlist: exact URL strings known to be inert (never fetched by the
# browser at runtime). Add new entries only after confirming the string is a
# literal/namespace identifier, not something passed to fetch()/XHR/<script
# src>/<link href>/<img src> — and say why below.
ALLOWLIST=(
    # React's minified-error decoder: built into a thrown Error's message
    # text ("Minified React error #31; visit https://react.dev/errors/31 for
    # the full message"). Only ever read by a developer off a stack trace —
    # React itself does not fetch it.
    'https://react.dev/errors/'
    # XML/SVG/MathML namespace URIs used by React's DOM renderer
    # (createElementNS / namespaced-attribute lookups), and by Plotly's own
    # SVG rendering (d3-selection's namespace map). Per the XML Namespaces
    # spec these are opaque identifiers, not URLs that get dereferenced.
    'http://www.w3.org/1998/Math/MathML'
    'http://www.w3.org/1999/xlink'
    'http://www.w3.org/1999/xhtml'
    'http://www.w3.org/2000/svg'
    'http://www.w3.org/2000/xmlns/'
    'http://www.w3.org/XML/1998/namespace'
    # plotly.js-gl2d-dist-min (Task 6): otto only ever draws `scattergl`
    # line charts via this bundle — none of the following are ever fetched
    # by that code path, only ever surfaced as inert config defaults, doc
    # comments, or user-clickable links.
    #   - `topojsonURL`'s schema default, for choropleth/geo trace types
    #     otto's dashboard never uses.
    'https://cdn.plot.ly/un/'
    #   - the modebar's "Produced with Plotly" attribution link and the
    #     animation-API's thrown-error message text — both user-facing,
    #     same category as the already-allowlisted `react.dev/errors/` link.
    'https://plotly.com/'
    'https://plotly.com/javascript/animations/'
    #   - the WebGL-unavailable fallback message's href/link text (shown,
    #     never fetched, and only when the browser lacks WebGL at all).
    'http://get.webgl.org'
    'https://get.webgl.org'
    #   - bundled d3-format/d3-time-format's format-string documentation
    #     links (constants, surfaced only in developer-facing error text).
    'https://github.com/d3/d3-format/tree/v1.4.5#d3-format'
    'https://github.com/d3/d3-time-format/tree/v2.2.3#locale_format'
    # tailwindcss v4 (Task 5): its bundled `/*! tailwindcss vX.Y.Z | MIT
    # License | https://tailwindcss.com */` banner comment atop the built
    # CSS — a license attribution string baked in by the tailwind compiler,
    # never fetched by anything at runtime.
    'https://tailwindcss.com'
)

# One match per output line, with file:line: prefix for diagnostics. The
# trailing-character class excludes the delimiters that typically close a URL
# literal in minified JS/CSS/HTML (quotes, backtick, backslash, space, paren)
# so the match ends where the URL actually does.
matches="$(grep -rEno 'https?://[^"'"'"'\\ )`]*' "$DIST" \
    --include='*.js' --include='*.css' --include='*.html' || true)"

if [ -z "$matches" ]; then
    echo "check_airgap: OK — no absolute URLs found under $DIST."
    exit 0
fi

offenders=()
total=0
while IFS=: read -r file line url; do
    [ -z "$url" ] && continue
    total=$((total + 1))
    allowed=0
    for pattern in "${ALLOWLIST[@]}"; do
        # Exact string equality (`=`), deliberately not substring/glob: a
        # prefix or pattern match here would let a real CDN URL ride an
        # allowlist entry (e.g. `https://react.dev/errors/`-as-prefix would
        # also pass `https://react.dev/errors/../evil.example.com`). Every
        # ALLOWLIST entry must be matched verbatim.
        if [ "$url" = "$pattern" ]; then
            allowed=1
            break
        fi
    done
    if [ "$allowed" -eq 0 ]; then
        offenders+=("$file:$line: $url")
    fi
done <<< "$matches"

if [ "${#offenders[@]}" -gt 0 ]; then
    echo "check_airgap: FAIL — non-allowlisted absolute URL(s) under $DIST:" >&2
    printf '  %s\n' "${offenders[@]}" >&2
    echo "otto must run fully air-gapped. If this URL is genuinely inert (never fetched at runtime), add it to ALLOWLIST in scripts/check_airgap.sh with a comment explaining why." >&2
    exit 1
fi

echo "check_airgap: OK — $total absolute URL occurrence(s) under $DIST, all allowlisted (inert)."
