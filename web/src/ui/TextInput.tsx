import { Label, TextField } from "react-aria-components";

import { InputBase } from "@/components/base/input/input";

// Untitled UI's high-level `Input` (components/base/input/input.tsx)
// explicitly whitelists which props it forwards from itself down to the
// internal `InputBase` it renders (ref/size/placeholder/icon/shortcut/
// .../type) — a `data-testid` given to `Input` never makes that list, so it
// lands on the outer `TextField` wrapper `Input` renders, not on the real
// `<input>` element. `log-filter-*`/`series-search`'s vitest specs need
// `data-testid` directly on the `<input>` (they use it as an
// `HTMLInputElement` with `fireEvent.change`), so this uses `InputBase`
// directly instead — it's also exported from that same vendored module,
// and (unlike `Input`) spreads any prop it doesn't recognize straight onto
// the `<input>` it renders. Composed here (not hand-edited) per
// web/README.md's never-hand-edit rule — see Task 3's report. (The review
// range's own from/to fields moved off this component entirely in Task 7 —
// RangePicker.tsx uses vendored `InputDateBase` at minute granularity.)
export function TextInput({
  label,
  type = "text",
  value,
  onChange,
  testId,
  shortcut,
  inputRef,
}: {
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  testId?: string;
  /** Keycap hint rendered by the vendored InputBase (e.g. "/"). */
  shortcut?: string;
  /** Ref callback to the real <input> (searchFocus registration). */
  inputRef?: (el: HTMLInputElement | null) => void;
}) {
  return (
    <TextField value={value} onChange={onChange} className="inline-flex items-center gap-1.5">
      <Label className="text-xs text-tertiary">{label}</Label>
      <InputBase
        type={type}
        size="sm"
        data-testid={testId}
        wrapperClassName="w-auto"
        shortcut={shortcut}
        ref={inputRef}
      />
    </TextField>
  );
}
