// web/src/ui/EventColorField.tsx
// The event editor's colour row (#858): named swatches over react-aria's
// ColorSwatchPicker (a single-select listbox, so one tab stop and arrow-key
// navigation). Untitled UI's free set has no colour picker; this is the one
// authored `ui/` addition the 2026-11-05 web UX batch sanctioned (README.md,
// "Authored ui/ components").
//
// Every name, hex and the default come from the event-style registry
// (otto/models/event_style.py, mirrored in api/eventStyle.gen.ts) through
// data/eventStyle.ts -- nothing here restates a colour.
import { Check } from "@untitledui/icons";
import { useId, useRef, useState } from "react";
import { ColorSwatch, ColorSwatchPicker, ColorSwatchPickerItem } from "react-aria-components";
import { Button } from "@/components/base/buttons/button";
import { Tooltip } from "@/components/base/tooltip/tooltip";
import { cx } from "@/utils/cx";
import { DEFAULT_EVENT_COLOR, EVENT_COLOR_PRESETS, type EventDash } from "../api/eventStyle.gen";
import { eventColorName, presetFor } from "../data/eventStyle";

export interface EventColorFieldProps {
  /** The event's colour, `#rrggbb` (matched to a preset case-insensitively). */
  value: string;
  /** Fires with lowercase `#rrggbb` whenever the user picks a colour. */
  onChange: (hex: string) => void;
  /** The event's dash style, for the colour previews. */
  dash: EventDash;
  /** The stored colour when it is not hex (e.g. `"red"`): shows a repair note. */
  legacyColor?: string;
}

interface Slot {
  /** The testid suffix: a preset's id, or `custom`. */
  id: string;
  name: string;
  hex: string;
}

/** One swatch option. Its tooltip is anchored to the option itself and
 * opened by the option's own hover and keyboard-focus state, so the option
 * holds no nested button or trigger and the row keeps its single tab stop. */
function SwatchItem({ slot }: { slot: Slot }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <ColorSwatchPickerItem
      ref={ref}
      color={slot.hex}
      aria-label={slot.name}
      data-testid={`editor-color-${slot.id}`}
      className={({ isFocusVisible }) =>
        cx(
          "relative flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-full outline-hidden pointer-coarse:size-11",
          isFocusVisible && "outline-2 outline-offset-0 outline-focus-ring",
        )
      }
    >
      {({ isSelected, isHovered, isFocusVisible }) => (
        // The vendored Tooltip's trigger wraps no DOM: with `triggerRef` it
        // positions itself on the option, and it portals out of the listbox.
        <Tooltip title={slot.name} isOpen={isHovered || isFocusVisible} triggerRef={ref}>
          {/* Selection is a ring around the swatch plus a check on a
              neutral disc; focus is the separate outline on the target. */}
          <ColorSwatch
            colorName={slot.name}
            className={cx(
              "size-6 rounded-full ring-offset-2 ring-offset-bg-primary transition",
              isSelected ? "ring-2 ring-fg-primary" : "ring-1 ring-black/10 ring-inset",
            )}
          />
          {isSelected && (
            <span className="absolute flex size-4 items-center justify-center rounded-full bg-primary shadow-xs">
              <Check aria-hidden="true" className="size-3 text-fg-primary" strokeWidth={3} />
            </span>
          )}
        </Tooltip>
      )}
    </ColorSwatchPickerItem>
  );
}

// `dash` is part of the interface for the colour previews (#859); the
// swatch row itself draws no line.
export function EventColorField({ value, onChange, legacyColor }: EventColorFieldProps) {
  const legendId = useId();
  const current = value.toLowerCase();
  // The last non-preset colour seen in this editor session, so picking a
  // preset and then the custom slot again gets it back. Adjusted during
  // render (React's "state from props" idiom), not in an effect.
  const [custom, setCustom] = useState<string | null>(presetFor(current) ? null : current);
  if (!presetFor(current) && current !== custom) setCustom(current);

  // The picker keys items by colour, so no two slots may share one: the
  // presets are distinct, and `custom` never holds a preset's hex.
  const slots: Slot[] = [
    ...EVENT_COLOR_PRESETS.map(({ id, name, hex }) => ({ id, name, hex })),
    ...(custom === null ? [] : [{ id: "custom", name: eventColorName(custom), hex: custom }]),
  ];
  const defaultName = eventColorName(DEFAULT_EVENT_COLOR);

  return (
    <fieldset className="min-w-0">
      {/* Both floated, so the legend lays out on one line with the
          persistent name (top-aligned, same line height) rather than in
          the fieldset's border: together they read "Color · Grey". */}
      <legend id={legendId} className="float-left text-sm font-medium text-secondary">
        Color
      </legend>
      <span data-testid="editor-color-name" className="float-left ml-1 text-sm text-tertiary">
        · {eventColorName(current)}
      </span>
      <ColorSwatchPicker
        aria-labelledby={legendId}
        className="clear-left mt-1 flex flex-wrap gap-1"
        value={current}
        onChange={(color) => onChange(color.toString("hex").toLowerCase())}
      >
        {slots.map((slot) => (
          <SwatchItem key={slot.hex} slot={slot} />
        ))}
      </ColorSwatchPicker>
      {legacyColor !== undefined && (
        <div
          data-testid="editor-color-legacy"
          className="mt-1.5 flex flex-col items-start gap-1 text-xs text-tertiary"
        >
          <p>
            This event's colour “{legacyColor}” isn't a hex colour; it is drawn as {defaultName}.
            Pick a colour to replace it.
          </p>
          {/* Pressing the already-selected swatch fires no change, so
              repairing to the drawn default needs its own control. */}
          <Button
            size="sm"
            color="link-color"
            data-testid="editor-color-legacy-repair"
            onPress={() => onChange(DEFAULT_EVENT_COLOR)}
          >
            Use {defaultName}
          </Button>
        </div>
      )}
    </fieldset>
  );
}
