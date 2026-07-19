// web/src/shell/MarkControl.tsx
// The live marking hub (spec 2026-07-18 §UI surfaces): a composed split
// control — no vendored split button exists, so primary Button + Dropdown,
// matching the AppBar's ButtonUtility row. The label popover serves both
// Mark-now and Start-span (uiStore.markPopover carries which); flows that
// need explicit times live in the EventEditor, reachable from the menu.
import { ChevronDown } from "@untitledui/icons";
import { useEffect, useRef, useState } from "react";
import { Dialog, DialogTrigger, Popover } from "react-aria-components";

import { Button } from "@/components/base/buttons/button";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { TextInput } from "../ui/TextInput";
import { useUiStore } from "../ui/uiStore";
import { blankDraft, endOpenSpan, markNow, startSpan } from "./marking";

export function MarkControl() {
  const session = useActiveSession();
  const markPopover = useUiStore((s) => s.markPopover);
  const openSpan = useUiStore((s) => s.openSpan);
  const { openMarkPopover, closeMarkPopover, armSweep, openEventEditor } = useUiStore(
    (s) => s.actions,
  );
  const addWarning = useReviewStore((s) => s.actions.addWarning);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputElRef = useRef<HTMLInputElement | null>(null);

  // Reset the draft label/error each time the popover opens, then focus.
  useEffect(() => {
    if (markPopover !== null) {
      setLabel("");
      setError(null);
      inputElRef.current?.focus();
    }
  }, [markPopover]);

  if (!session) return null;
  const spanOpen = openSpan?.sessionId === session.id;

  const submit = async () => {
    if (!label.trim()) return;
    try {
      await (markPopover === "start" ? startSpan(label) : markNow(label));
      closeMarkPopover();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
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
            <div className="flex flex-col gap-1">
              <TextInput
                label="Label"
                testId="mark-label-input"
                value={label}
                onChange={setLabel}
                inputRef={(el) => {
                  inputElRef.current = el;
                }}
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
                isDisabled={!spanOpen}
                onAction={() => {
                  void endOpenSpan().catch((err) =>
                    addWarning(
                      `End span failed: ${err instanceof Error ? err.message : String(err)}`,
                    ),
                  );
                }}
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
