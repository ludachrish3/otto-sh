// web/src/shell/DateFieldCompleteness.tsx
// Reports how much of a react-aria date field the user has filled in, and
// shows that field's message. Rendered as the vendored InputDate's `hint`,
// which sits inside AriaDateField, so DateFieldStateContext is in scope.
//
// Why read the segments: clearing ONE segment never calls the field's
// onChange (react-stately keeps the incomplete value as a display-only
// override), so the parent's controlled value still holds the old instant.
// Only the field's own segment state knows the edit is incomplete.
import { useContext, useEffect } from "react";
import { DateFieldStateContext } from "react-aria-components";

export type DateFill = "empty" | "partial" | "complete";

export function DateFieldCompleteness(props: {
  onFillChange: (fill: DateFill) => void;
  message: string | null;
}): string | null {
  const state = useContext(DateFieldStateContext);
  const editable = state?.segments.filter((s) => s.isEditable) ?? [];
  const blanks = editable.filter((s) => s.isPlaceholder).length;
  const fill: DateFill =
    blanks === 0 ? "complete" : blanks === editable.length ? "empty" : "partial";
  const { onFillChange } = props;
  useEffect(() => {
    onFillChange(fill);
  }, [fill, onFillChange]);
  return props.message;
}
