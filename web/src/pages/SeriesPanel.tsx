// Left panel of the subject view (UX spec §9): search -> quick-filter
// chips (chart groups + Source) -> series tree with checkboxes. Fully
// controlled; selection state lives in SubjectPage.
//
// The chip groups are two separate vendored TagGroups (chart-group chips are
// genuinely multi-select; the Source chip is single-select but must still
// deselect on a second click of the same chip, which is why it passes
// `disallowEmptySelection={false}` — TagGroup defaults single-select to
// disallowing an empty selection, same as a radio group would).
//
// Chip test hooks ride on `data-key`, NOT `data-testid`. Tag drops
// `data-testid` on the floor: its prop destructuring lists a fixed set of
// names with no `...rest` capture, so an unrecognized prop never reaches the
// DOM at all — not even on a wrapper, the way Badge/Select/Input's testid
// gaps do. A `<span data-testid=...>` wrapped AROUND `<Tag>` doesn't work
// either: react-aria's TagList collection-builder does a special hidden-tree
// walk to find Tag items, and a plain host element (span) between TagList and
// Tag makes the scanner drop the item entirely (verified — it renders an
// empty `role="grid"`, zero tags).
//
// Putting the testid span inside `children` DID render, but silently cost the
// chips their accessible name: Tag derives `textValue` only from plain-string
// children (`typeof children === "string"`, same destructuring gap means a
// caller-supplied `textValue` is dropped too), and react-aria feeds that
// straight into the tag's `aria-label`. Non-string children => no textValue =>
// no aria-label at all, which is exactly what react-aria's dev-only
// "A `textValue` prop is required for <Tag> elements" warning was reporting.
// It was not cosmetic.
//
// So children stay plain strings, and the per-chip hook is the `data-key`
// attribute react-aria already stamps on each tag root from the `id` prop
// below (verified in the rendered DOM). The enclosing TagGroup does spread
// rest props, so the group-level `data-testid` there namespaces the two
// groups — chart chips and source chips can share a key without colliding.
import { Badge } from "@/components/base/badges/badges";
import { Checkbox } from "@/components/base/checkbox/checkbox";
import { Tag, TagGroup, TagList } from "@/components/base/tags/tags";
import { type ChartNode, sourcesIn } from "../data/seriesTree";
import { registerSearchInput } from "../ui/searchFocus";
import { formatBinding, SEARCH_BINDING } from "../ui/shortcuts";
import { TextInput } from "../ui/TextInput";

export function SeriesPanel(props: {
  /** The FILTERED tree — drives the series checkbox list below the chips. */
  tree: ChartNode[];
  /** The UNFILTERED chart list — drives the filter chips, so selecting one
   * chip never removes the others (they are the controls that DO the
   * filtering; deriving them from the filtered `tree` made every non-selected
   * chip vanish, killing multi-select — TODO item 3). */
  allCharts: ChartNode[];
  checked: Set<string>;
  onToggle: (key: string) => void;
  search: string;
  onSearch: (value: string) => void;
  chips: Set<string> | null;
  onChips: (chips: Set<string> | null) => void;
  source: string | null;
  onSource: (source: string | null) => void;
}) {
  const { tree, allCharts, checked, onToggle, search, onSearch, chips, onChips, source, onSource } =
    props;
  const sources = sourcesIn(allCharts);

  return (
    <aside
      data-testid="series-panel"
      className="flex w-64 shrink-0 flex-col gap-3 border-r border-secondary pr-4"
    >
      <TextInput
        label="Search"
        value={search}
        onChange={onSearch}
        testId="series-search"
        shortcut={formatBinding(SEARCH_BINDING)}
        inputRef={registerSearchInput}
      />
      <div className="flex flex-col gap-2">
        {allCharts.length > 0 && (
          <TagGroup
            data-testid="chart-chips"
            label="Chart filters"
            selectionMode="multiple"
            selectedKeys={chips ?? new Set()}
            onSelectionChange={(keys) => {
              const next =
                keys === "all"
                  ? new Set(allCharts.map((c) => c.chartKey))
                  : new Set([...keys].map(String));
              onChips(next.size === 0 ? null : next);
            }}
          >
            <TagList className="flex flex-wrap gap-1.5">
              {allCharts.map((chart) => (
                <Tag key={chart.chartKey} id={chart.chartKey}>
                  {chart.chartLabel}
                </Tag>
              ))}
            </TagList>
          </TagGroup>
        )}
        {sources.length > 0 && (
          <TagGroup
            data-testid="source-chips"
            label="Source filters"
            selectionMode="single"
            disallowEmptySelection={false}
            selectedKeys={source ? new Set([source]) : new Set()}
            onSelectionChange={(keys) => {
              const arr = keys === "all" ? [] : [...keys];
              onSource(arr.length > 0 ? String(arr[0]) : null);
            }}
          >
            <TagList className="flex flex-wrap gap-1.5">
              {sources.map((src) => (
                <Tag key={src} id={src}>
                  {`src: ${src}`}
                </Tag>
              ))}
            </TagList>
          </TagGroup>
        )}
      </div>
      <div className="flex flex-col gap-2 overflow-y-auto text-sm">
        {tree.map((chart) => (
          <div key={chart.chartKey}>
            <p className="mb-1 text-xs font-semibold text-quaternary uppercase">
              {chart.chartLabel}
              {chart.series.length > 6 ? ` (${chart.series.length})` : ""}
            </p>
            <ul className="flex flex-col gap-0.5">
              {chart.series.map((s) => (
                <li key={s.key} className="flex items-center gap-2">
                  <Checkbox
                    size="sm"
                    data-testid={`series-node-${s.key}`}
                    isSelected={checked.has(s.key)}
                    onChange={() => onToggle(s.key)}
                  />
                  <span className="truncate">{s.key === s.label ? s.label : s.host}</span>
                  {s.source !== null && (
                    <Badge type="color" size="sm" color="gray">
                      {s.source}
                    </Badge>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {tree.length === 0 && <p className="text-xs text-tertiary">No series match.</p>}
      </div>
    </aside>
  );
}
