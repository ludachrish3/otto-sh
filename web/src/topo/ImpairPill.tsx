// The impair marker. Positioned on the canvas; bare in the legend — one
// component so the key and the thing it explains cannot diverge.
export function ImpairPill(props: { impair?: string; testId?: string; x?: number; y?: number }) {
  const { impair, testId, x, y } = props;
  const positioned = x !== undefined && y !== undefined;
  return (
    <span
      data-testid={testId}
      style={
        positioned ? { transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` } : undefined
      }
      className={`${positioned ? "absolute " : ""}rounded-full border border-gray-300 bg-white
        px-1.5 py-0.5 text-[10px] whitespace-nowrap text-gray-500 dark:border-gray-700
        dark:bg-gray-950 dark:text-gray-400`}
    >
      {impair === undefined ? "impair" : `impair · ${impair}`}
    </span>
  );
}
