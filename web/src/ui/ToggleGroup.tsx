import { Radio, RadioGroup } from "react-aria-components";

/** Segmented single-choice control (range presets). RadioGroup gives the
 * roving-selection semantics; visual is a joined pill row. */
export function ToggleGroup({
  options,
  selectedId,
  onSelect,
  testId,
  label,
}: {
  options: { id: string; label: string }[];
  selectedId: string;
  onSelect: (id: string) => void;
  testId?: string;
  label?: string;
}) {
  return (
    <RadioGroup
      aria-label={label ?? "options"}
      value={selectedId}
      onChange={onSelect}
      data-testid={testId}
      className="inline-flex overflow-hidden rounded-lg border border-gray-300
        dark:border-gray-700"
    >
      {options.map((opt) => (
        <Radio
          key={opt.id}
          value={opt.id}
          className="cursor-pointer border-r border-gray-300 px-3 py-1.5 text-sm
            text-gray-600 last:border-r-0 data-[selected]:bg-brand-600 data-[selected]:text-white
            hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300
            dark:data-[selected]:bg-brand-500 dark:hover:bg-gray-800"
        >
          {opt.label}
        </Radio>
      ))}
    </RadioGroup>
  );
}
