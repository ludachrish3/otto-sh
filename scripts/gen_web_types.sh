#!/usr/bin/env bash
# Regenerate web/src/api/types.gen.ts and web/src/api/export.gen.ts from
# otto's live pydantic models.
#
# monitor-meta.schema.json (the internal chart/tab-layout contract, see
# src/otto/models/monitor.py::MonitorMeta) and monitor-export.schema.json
# (the export document contract, see src/otto/models/monitor.py::
# MonitorExport) are exported via `otto schema export`, then each converted
# to a TS type declaration with json-schema-to-typescript. The generated
# files are COMMITTED (not gitignored) so a checkout builds without Node
# ever running codegen, and `make web` re-runs this script and diffs the
# result to catch drift between the pydantic models and the committed TS
# types (see the `web` Makefile target).
#
# monitor-export.schema.json also carries an otherwise-unreferenced `$defs`
# entry for MonitorSessionFragment (see
# otto.models.jsonschema._monitor_export_schema): the live SSE wire model
# reuses MetricRecord/EventRecord/LogEventRecord/SessionMeta verbatim, so
# rather than a second schema document (and a second, duplicate set of TS
# interfaces) it rides as a $defs sibling of the types it's built from.
# --unreachableDefinitions tells json-schema-to-typescript to still emit a
# type for it even though nothing in the export document's `properties`
# references it.
#
# Idempotent: running this twice produces byte-identical output — do not
# edit web/src/api/types.gen.ts or web/src/api/export.gen.ts by hand.
#
# Warnings are errors: json-schema-to-typescript writes nothing to stderr on
# a clean run, so any stderr output from it (a schema construct it can't
# translate, a Node deprecation warning) fails this script with the captured
# text reprinted, the same rule scripts/build_web_no_warnings.sh applies to
# web/'s npm scripts.
set -euo pipefail

cd "$(dirname "$0")/.."

# npm's weekly "New major version of npm available!" notice goes to stderr
# from npx itself, depends on the registry rather than on json2ts, and would
# fail the stderr check below on unchanged code. Same switch as
# scripts/build_web_no_warnings.sh.
export npm_config_update_notifier=false

uv run otto schema export --out schemas

ERR="$(mktemp)"
trap 'rm -f "$ERR"' EXIT

# json2ts <args...>: run json-schema-to-typescript in web/ and fail,
# reprinting it, if it wrote anything to stderr. Captured to a file and
# checked after the command returns -- no process substitution, whose
# writer could still be running when the check reads the file.
json2ts() {
    if ! (cd web && npx json-schema-to-typescript "$@") 2>"$ERR"; then
        cat "$ERR" >&2
        echo "gen_web_types: json-schema-to-typescript failed." >&2
        exit 1
    fi
    if [ -s "$ERR" ]; then
        {
            echo "gen_web_types: json-schema-to-typescript wrote to stderr; any"
            echo "stderr output is a warning, and warnings are errors here:"
            echo "----"
            cat "$ERR"
            echo "----"
        } >&2
        exit 1
    fi
}

json2ts \
    ../schemas/monitor-meta.schema.json \
    -o src/api/types.gen.ts \
    --bannerComment "/* AUTO-GENERATED from monitor-meta.schema.json — run scripts/gen_web_types.sh; do not edit. */"
json2ts \
    --unreachableDefinitions \
    ../schemas/monitor-export.schema.json \
    -o src/api/export.gen.ts \
    --bannerComment "/* AUTO-GENERATED from monitor-export.schema.json — run scripts/gen_web_types.sh; do not edit. */"
