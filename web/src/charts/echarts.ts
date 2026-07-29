// Tree-shaken echarts core (UX spec §5): canvas renderer, line charts,
// and exactly the components the review stack uses. Direct instance
// management (the spec's confirmed choice) — no echarts-for-react.
import { LineChart } from "echarts/charts";
import {
  BrushComponent,
  DataZoomInsideComponent,
  GridComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { connect, init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomInsideComponent,
  MarkLineComponent,
  MarkAreaComponent,
  BrushComponent,
  CanvasRenderer,
]);

/** The two entry points ChartPanel needs, and nothing else. Named imports
 * rather than `import * as echartsCore` (performance/noNamespaceImport):
 * re-exporting the whole `echarts/core` namespace object made every export
 * on it reachable, which is the opposite of this module's stated purpose.
 * The object shape is kept because seven test files `vi.mock` this module
 * as `{ echarts: { init, connect } }`. */
export const echarts = { init, connect };
