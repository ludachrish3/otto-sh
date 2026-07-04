# Monitor Phase 3 Plan A — ship-as-noted follow-ups

Source: final whole-branch review of the Plan A (metrics) branch, merged to main
2026-07-04 (tip `221990d`). All items were triaged non-blocking; none affect the
green gate. Spec: `docs/superpowers/specs/2026-07-03-monitor-metrics-phase3-design.md`.

## Code

- **`SnmpMetric.to_point` is dead in the production path** (`src/otto/monitor/snmp.py`).
  `process_snmp_values` inlines its own `raw * scale` + `round(..., 2)`, so the same
  scaling logic exists twice — latent drift risk if scaling ever changes. Decide:
  delete `to_point` (a small breaking API call — it is public-ish and unit-tested) or
  route `process_snmp_values` through it for gauges.
- **`resolve_snmp_metric` runs for `None`-valued OIDs** in `process_snmp_values`
  before the skip filter — harmless wasted work; hoist the skip if ever touched.
- **`parser.parse` defensive branches untested**: `except ValueError` in
  `NetDevParser`/`PerCoreCpuParser`, `contextlib.suppress` for malformed
  `procs_blocked` in `ProcCountParser` — add fixtures if these paths ever matter.
- **`MemParser` multi-`Mem:`-line behavior changed** from first-wins (old early
  return) to last-wins (loop). Unreachable with real `free -b`; note if the parser
  is ever reused on multi-sample input.
- **`PerCoreCpuParser` / `DiskIoParser` PLR2004 noqa** on field-count checks could be
  named constants (matches existing `TopCpu`/`Mem` convention today, so cosmetic).

## Tests

- **Churn-in-parser coverage**: no test exercises `RateTracker.prune` *through*
  `NetDevParser`/`DiskIoParser` (interface/device vanishing then reappearing →
  re-baseline, no stale-rate spike). Primitive-level prune is covered in
  `tests/unit/monitor/test_rates.py`.
- **No direct duplicate-pattern-string test** for `register_host_parsers` with
  `re.Pattern` (registration-time dupe raises via generic Registry machinery, which
  is covered generically in `tests/unit/registry/`).

## Docs

- **`otto/examples/monitor.py` docstring** shows only the per-host registration
  form; add the project-wide `register_parsers([...])` variant (mirroring
  `otto/monitor/parsers.py`'s module docstring) and optionally a `>>>` doctest to
  match the sibling `otto.examples.*` modules.
- **`register_host_parsers` docstring** doesn't mention that re-registering the same
  *pattern string* raises at registration time (unlike exact ids, which re-register
  freely); the fact lives only in the `HOST_PATTERN_PARSERS` module comment.

## Out-of-repo / bed

- **Firmware agent must grow the `.2.<i>` (network) and `.3.<i>` (filesystem) OID
  subtrees** — the manager side + descriptors shipped; the contract table is in
  `docs/guide/monitor.md` (marked firmware-facing). Charts light up when the agent
  serves the OIDs; until then, old firmware degrades gracefully (warn-once per OID).
- **Zephyr bed redeploy still pending** (fs-shell mount-leak fix); `make
  qemu-restart` remains the recovery for a wedged embedded bed.

## Deferred by design (already recorded in the spec's Future Work — not new)

CLI metric toggles (Phase 4), shipped `NetstatSocketsParser`/tool auto-detection,
per-series chart override, friendly device-served interface names, embedded
memory/CPU-detail OID subtrees (`.4`/`.5`), log events as chart markers (Plan B+).
