import type { KeyboardEvent } from "react";
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
// web/README.md's never-hand-edit rule. (The review range's own from/to
// fields no longer use this component at all — RangePicker.tsx uses
// vendored `InputDateBase` at minute granularity.)
//
// `onKeyDown`: MarkControl's label field submits on Enter —
// InputBase already accepts (and spreads) arbitrary native input props
// including onKeyDown; this wrapper just needed to expose it in its own
// whitelist. Extended rather than hand-rolling a second input here —
// TextInput is authored, not vendored, so extending it is allowed.
export function TextInput({
  label,
  type = "text",
  value,
  onChange,
  testId,
  shortcut,
  inputRef,
  onKeyDown,
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
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
}) {
  return (
    <TextField value={value} onChange={onChange} className="inline-flex items-center gap-1.5">
      <Label className="text-xs text-tertiary">{label}</Label>
      {/* The four optional props are spread CONDITIONALLY rather than passed
          as `foo={maybeUndefined}`: under `exactOptionalPropertyTypes`,
          `InputBase`'s vendored props declare `shortcut?: string` (not
          `?: string | undefined`), so handing it an explicit `undefined`
          is an error. Widening OUR OWN prop types above would not help --
          the rejection happens at the target's declaration, and the target
          is vendored. Omitting the key is the only fix available here. */}
      <InputBase
        type={type}
        size="sm"
        wrapperClassName="w-auto"
        {...(testId !== undefined && { "data-testid": testId })}
        {...(shortcut !== undefined && { shortcut })}
        {...(inputRef !== undefined && { ref: inputRef })}
        {...(onKeyDown !== undefined && { onKeyDown })}
      />
    </TextField>
  );
}
