// The live-stream (`monitor-live-stream`) versions the browser reads and
// writes: its own declared format, not the export document's. A mirror of the
// declared lists in otto.models.formats (dump spec §13.1), which TypeScript
// cannot import; tests/unit/models/test_browser_format_mirrors.py fails when
// the two differ. `satisfies` ties both to the `format` of the
// MonitorSessionFragment type export.gen.ts generates from the Python model.
import type { MonitorSessionFragment } from "../api/export.gen";

type StreamFormat = NonNullable<MonitorSessionFragment["format"]>;

export const MONITOR_STREAM_READ_VERSIONS = [1] as const satisfies readonly StreamFormat[];
export const MONITOR_STREAM_WRITE_VERSIONS = [1] as const satisfies readonly StreamFormat[];

/** The one declared write version; `never` once there are two, so tsc fails until a writer chooses. */
type SoleWriteVersion = typeof MONITOR_STREAM_WRITE_VERSIONS extends readonly [infer Only]
  ? Only
  : never;

/** The `format` every fragment the browser builds is stamped with. */
export const STREAM_FORMAT: SoleWriteVersion = MONITOR_STREAM_WRITE_VERSIONS[0];
