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
import * as echartsCore from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

echartsCore.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomInsideComponent,
  MarkLineComponent,
  MarkAreaComponent,
  BrushComponent,
  CanvasRenderer,
]);

export const echarts = echartsCore;
