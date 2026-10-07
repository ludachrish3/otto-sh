// The monitor-export versions the browser reads and writes. A mirror of the
// declared lists in otto.models.formats (dump spec §13.1), which TypeScript
// cannot import; tests/unit/models/test_export_format_mirror.py fails when
// the two differ. `satisfies` ties both to `Format`, the type export.gen.ts
// generates from the Python read list.
import type { Format } from "../api/export.gen";

export const MONITOR_EXPORT_READ_VERSIONS = [1] as const satisfies readonly Format[];
export const MONITOR_EXPORT_WRITE_VERSIONS = [1] as const satisfies readonly Format[];

/** The one declared write version; `never` once there are two, so tsc fails until a writer chooses. */
type SoleWriteVersion = typeof MONITOR_EXPORT_WRITE_VERSIONS extends readonly [infer Only]
  ? Only
  : never;

/** The `format` every document the browser builds is stamped with. */
export const EXPORT_FORMAT: SoleWriteVersion = MONITOR_EXPORT_WRITE_VERSIONS[0];
