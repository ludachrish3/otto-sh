#!/usr/bin/env bash
# Regenerate web/src/api/types.gen.ts from otto's live pydantic models.
#
# monitor-meta.schema.json (the /api/meta wire contract, see
# src/otto/models/monitor.py::MonitorMeta) is exported via `otto schema
# export`, then converted to a TS type declaration with
# json-schema-to-typescript. The generated file is COMMITTED (not
# gitignored) so a checkout builds without Node ever running codegen, and
# `make web` re-runs this script and diffs the result to catch drift between
# the pydantic models and the committed TS types (see the `web` Makefile
# target).
#
# Idempotent: running this twice produces byte-identical output — do not
# edit web/src/api/types.gen.ts by hand.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run otto schema export --out schemas

(
    cd web
    npx json-schema-to-typescript \
        ../schemas/monitor-meta.schema.json \
        -o src/api/types.gen.ts \
        --bannerComment "/* AUTO-GENERATED from monitor-meta.schema.json — run scripts/gen_web_types.sh; do not edit. */"
)
