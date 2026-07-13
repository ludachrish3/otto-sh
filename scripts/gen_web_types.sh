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
set -euo pipefail

cd "$(dirname "$0")/.."

uv run otto schema export --out schemas

(
    cd web
    npx json-schema-to-typescript \
        ../schemas/monitor-meta.schema.json \
        -o src/api/types.gen.ts \
        --bannerComment "/* AUTO-GENERATED from monitor-meta.schema.json — run scripts/gen_web_types.sh; do not edit. */"
    npx json-schema-to-typescript \
        --unreachableDefinitions \
        ../schemas/monitor-export.schema.json \
        -o src/api/export.gen.ts \
        --bannerComment "/* AUTO-GENERATED from monitor-export.schema.json — run scripts/gen_web_types.sh; do not edit. */"
)
