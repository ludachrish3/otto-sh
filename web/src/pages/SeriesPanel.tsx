// Left panel of the subject view (UX spec §9): search -> quick-filter
// chips (chart groups + Source) -> series tree with checkboxes. Fully
// controlled; selection state lives in SubjectPage.
import { type ChartNode, sourcesIn } from "../data/seriesTree";
import { Badge } from "../ui/Badge";
import { TextInput } from "../ui/TextInput";

export function SeriesPanel(props: {
  tree: ChartNode[];
  checked: Set<string>;
  onToggle: (key: string) => void;
  search: string;
  onSearch: (value: string) => void;
  chips: Set<string> | null;
  onChips: (chips: Set<string> | null) => void;
  source: string | null;
  onSource: (source: string | null) => void;
}) {
  const { tree, checked, onToggle, search, onSearch, chips, onChips, source, onSource } = props;
  const sources = sourcesIn(tree);

  const toggleChip = (chartKey: string) => {
    const next = new Set(chips ?? []);
    if (next.has(chartKey)) next.delete(chartKey);
    else next.add(chartKey);
    onChips(next.size === 0 ? null : next);
  };

  return (
    <aside
      data-testid="series-panel"
      className="flex w-64 shrink-0 flex-col gap-3 border-r border-gray-200 pr-4 dark:border-gray-800"
    >
      <TextInput label="Search" value={search} onChange={onSearch} testId="series-search" />
      <div className="flex flex-wrap gap-1.5">
        {tree.map((chart) => (
          <button
            key={chart.chartKey}
            type="button"
            data-testid={`chip-${chart.chartKey}`}
            onClick={() => toggleChip(chart.chartKey)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              chips?.has(chart.chartKey)
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            {chart.chartLabel}
          </button>
        ))}
        {sources.map((src) => (
          <button
            key={src}
            type="button"
            data-testid={`chip-source-${src}`}
            onClick={() => onSource(source === src ? null : src)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              source === src
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            src: {src}
          </button>
        ))}
      </div>
      <div className="flex flex-col gap-2 overflow-y-auto text-sm">
        {tree.map((chart) => (
          <div key={chart.chartKey}>
            <p className="mb-1 text-xs font-semibold text-gray-400 uppercase">
              {chart.chartLabel}
              {chart.series.length > 6 ? ` (${chart.series.length})` : ""}
            </p>
            <ul className="flex flex-col gap-0.5">
              {chart.series.map((s) => (
                <li key={s.key} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    data-testid={`series-node-${s.key}`}
                    checked={checked.has(s.key)}
                    onChange={() => onToggle(s.key)}
                    className="accent-brand-600"
                  />
                  <span className="truncate">{s.key === s.label ? s.label : s.host}</span>
                  {s.source !== null && <Badge>{s.source}</Badge>}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {tree.length === 0 && <p className="text-xs text-gray-400">No series match.</p>}
      </div>
    </aside>
  );
}
