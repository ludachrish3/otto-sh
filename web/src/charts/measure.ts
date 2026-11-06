// The label measurer `labelLayout` (options.ts) lays event labels out with —
// kept out of options.ts so that file stays free of the DOM.
import { approxMeasure, LABEL_FONT_FAMILY, LABEL_FONT_SIZE, type MeasureText } from "./options";

const JSDOM_UA = /jsdom/iu;

/** True where a real canvas 2D context exists. Checked BEFORE `getContext`:
 * jsdom's getContext logs "Not implemented" to console.error before it
 * returns null, and the vitest setup fails a test on any captured error. */
function canvasSupported(): boolean {
  return (
    typeof document !== "undefined" &&
    typeof navigator !== "undefined" &&
    !JSDOM_UA.test(navigator.userAgent)
  );
}

function defaultContext(): CanvasRenderingContext2D | null {
  return canvasSupported() ? document.createElement("canvas").getContext("2d") : null;
}

/** Pixel width of a label drawn at `LABEL_FONT_SIZE`px `LABEL_FONT_FAMILY`,
 * measured on a canvas. Falls back to `approxMeasure` where there is no
 * canvas. `getContext` is injectable for tests. Not cached: a width measured
 * before Inter has loaded is the fallback face's, and ChartPanel lays labels
 * out again once `document.fonts.ready` resolves. */
export function createTextMeasure(
  getContext: () => CanvasRenderingContext2D | null = defaultContext,
): MeasureText {
  const ctx = getContext();
  if (ctx === null) return approxMeasure;
  const font = `${LABEL_FONT_SIZE}px ${LABEL_FONT_FAMILY}`;
  return (text) => {
    ctx.font = font;
    return ctx.measureText(text).width;
  };
}
