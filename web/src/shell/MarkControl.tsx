// web/src/shell/MarkControl.tsx
// The live marking hub (spec 2026-07-18 §UI surfaces): a composed split
// control — no vendored split button exists, so primary Button + Dropdown,
// matching the AppBar's ButtonUtility row. The label popover serves both
// Mark-now and Start-span (uiStore.markPopover carries which); flows that
// need explicit times live in the EventEditor, reachable from the menu.
import { ChevronDown } from "@untitledui/icons";
import { useEffect, useState } from "react";
import { Dialog, DialogTrigger, Popover } from "react-aria-components";

import { Button } from "@/components/base/buttons/button";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useActiveSession } from "../data/reviewStore";
import { TextInput } from "../ui/TextInput";
import { useUiStore } from "../ui/uiStore";
import { useOpenFocus } from "../ui/useOpenFocus";
import { blankDraft, endSpanReporting, markNow, startSpan } from "./marking";
import { settleOnOpening } from "./settleOnOpening";
import { markGateKey, useMutationGate } from "./useMutationGate";

/** This control's action names on the shared mark gate. */
const SUBMIT = "appbar-submit";
const END_SPAN = "appbar-end-span";

export function MarkControl() {
  const session = useActiveSession();
  const markPopover = useUiStore((s) => s.markPopover);
  const openSpan = useUiStore((s) => s.openSpan);
  const { openMarkPopover, closeMarkPopover, armSweep, openEventEditor } = useUiStore(
    (s) => s.actions,
  );
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  // One request in flight for this session's marking, shared with the Events
  // compose row and the palette (see useMutationGate.ts's keys).
  const gate = useMutationGate(markGateKey(session?.id ?? ""));
  // The label field has focus each time the popover opens (ui/useOpenFocus.ts).
  const labelFocus = useOpenFocus<HTMLInputElement>(session ? markPopover : null);

  // Reset the draft label/error each time the popover opens.
  useEffect(() => {
    if (markPopover !== null) {
      setLabel("");
      setError(null);
    }
  }, [markPopover]);

  if (!session) return null;
  const spanOpen = openSpan?.sessionId === session.id;

  const submit = () => {
    if (!label.trim()) return Promise.resolve();
    // Settles on the popover opening that sent it: a late result never
    // closes, or shows an error in, a reopened popover (settleOnOpening).
    return gate.run(
      () =>
        settleOnOpening(
          "markPopover",
          () => (markPopover === "start" ? startSpan(label) : markNow(label)),
          { done: closeMarkPopover, fail: ({ message }) => setError(message) },
        ),
      SUBMIT,
    );
  };

  return (
    <div className="flex items-center gap-0.5">
      <DialogTrigger
        isOpen={markPopover !== null}
        onOpenChange={(open) => {
          if (open) openMarkPopover("mark");
          else closeMarkPopover();
        }}
      >
        <Button size="sm" color="secondary" data-testid="mark-button">
          Mark now…
        </Button>
        <Popover placement="bottom end" offset={8}>
          <Dialog
            data-testid="mark-popover"
            aria-label={markPopover === "start" ? "Start span" : "Mark now"}
            className="flex items-start gap-2 rounded-xl bg-primary p-3 shadow-lg ring
              ring-secondary_alt focus:outline-hidden"
          >
            {labelFocus.scope}
            <div className="flex flex-col gap-1">
              <TextInput
                label="Label"
                testId="mark-label-input"
                value={label}
                onChange={setLabel}
                inputRef={labelFocus.ref}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submit();
                }}
              />
              {error !== null && (
                <p data-testid="mark-error" className="max-w-56 text-xs text-error-primary">
                  {error}
                </p>
              )}
            </div>
            <Button
              size="sm"
              color="primary"
              data-testid="mark-submit"
              isLoading={gate.pendingAction === SUBMIT}
              isDisabled={gate.pending && gate.pendingAction !== SUBMIT}
              showTextWhileLoading
              onPress={() => void submit()}
            >
              {markPopover === "start" ? "Start" : "Mark"}
            </Button>
          </Dialog>
        </Popover>
      </DialogTrigger>
      <Dropdown.Root>
        <ButtonUtility
          aria-label="More marking actions"
          data-testid="mark-menu"
          icon={ChevronDown}
          color="tertiary"
          size="sm"
        />
        <Dropdown.Popover>
          <Dropdown.Menu>
            <Dropdown.Section>
              <Dropdown.Item
                id="start-span"
                label="Start span…"
                onAction={() => openMarkPopover("start")}
                data-testid="menu-start-span"
              />
              <Dropdown.Item
                id="end-span"
                label="End span"
                isDisabled={!spanOpen || gate.pending}
                onAction={() => void gate.run(endSpanReporting, END_SPAN)}
                data-testid="menu-end-span"
              />
              <Dropdown.Item
                id="sweep-span"
                label="Sweep span on chart"
                onAction={armSweep}
                data-testid="menu-sweep-span"
              />
              <Dropdown.Item
                id="add-event"
                label="Add event…"
                onAction={() => openEventEditor({ kind: "draft", draft: blankDraft(session) })}
                data-testid="menu-add-event"
              />
            </Dropdown.Section>
          </Dropdown.Menu>
        </Dropdown.Popover>
      </Dropdown.Root>
    </div>
  );
}
