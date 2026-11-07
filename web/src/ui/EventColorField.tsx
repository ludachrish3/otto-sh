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
//
// The row's last item, Custom… (#859), opens a popover with a hue wheel
// around a saturation x brightness area, an exact hex field and two marker
// previews. Its one value is a temporary HSB colour (so a hue survives a
// pass through grey); Use colour serialises it once into the editor draft,
// and the editor's Save stays the only write.
import { Check } from "@untitledui/icons";
import { useId, useRef, useState } from "react";
import {
  type Color,
  ColorArea,
  ColorField,
  ColorSwatch,
  ColorSwatchPicker,
  ColorSwatchPickerItem,
  ColorThumb,
  ColorWheel,
  ColorWheelTrack,
  Dialog,
  DialogTrigger,
  Popover,
  parseColor,
} from "react-aria-components";
import { Button } from "@/components/base/buttons/button";
import { HintText } from "@/components/base/input/hint-text";
import { InputBase } from "@/components/base/input/input";
import { Label } from "@/components/base/input/label";
import { Tooltip } from "@/components/base/tooltip/tooltip";
import { cx } from "@/utils/cx";
import { DEFAULT_EVENT_COLOR, EVENT_COLOR_PRESETS, type EventDash } from "../api/eventStyle.gen";
import { MARKER_LINE_WIDTH, SPAN_FILL_ALPHA, svgDashArray, withAlpha } from "../charts/markerStyle";
import { chartTheme, LABEL_FONT_FAMILY, LABEL_FONT_SIZE } from "../charts/options";
import { eventColorName, normalizeHexInput, presetFor } from "../data/eventStyle";
import { plotSurfaces } from "../theme";

export interface EventColorFieldProps {
  /** The event's colour, `#rrggbb` (matched to a preset case-insensitively). */
  value: string;
  /** Fires with lowercase `#rrggbb` whenever the user picks a colour. */
  onChange: (hex: string) => void;
  /** The event's dash style, for the colour previews. */
  dash: EventDash;
  /** The stored colour when it is not hex (e.g. `"red"`): shows a repair note. */
  legacyColor?: string;
  /** Every control refuses changes, e.g. while the editor's Save is pending. */
  isDisabled?: boolean;
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
function SwatchItem({ slot, isDisabled }: { slot: Slot; isDisabled: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <ColorSwatchPickerItem
      ref={ref}
      color={slot.hex}
      aria-label={slot.name}
      data-testid={`editor-color-${slot.id}`}
      isDisabled={isDisabled}
      className={({ isFocusVisible, isDisabled: disabled }) =>
        cx(
          "relative flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-full outline-hidden pointer-coarse:size-11",
          isFocusVisible && "outline-2 outline-offset-0 outline-focus-ring",
          disabled && "cursor-not-allowed opacity-50",
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

const HEX_ERROR = "Enter six hexadecimal digits, for example #00abcd";

/** The popover's state: the temporary colour Use colour applies, and whether
 * the user's latest typing was refused. */
interface CustomDraft {
  /** HSB, never round-tripped through hex while the user drags. */
  color: Color;
  /** The hex field's text, exactly as typed: RAC's commit (blur/Enter)
   * would revert refused text to the old colour, beside the error. A wheel
   * or area change resets it to the colour's hex. */
  rawText: string;
  /** On from typing that is not six hex digits until valid typing or a
   * wheel/area change; RAC's commit-time revert of the text never clears it. */
  invalidAttempt: boolean;
}

/** The draft for a colour the user produced on the wheel or area (or the
 * editor's colour on open): valid, with the text showing its hex. */
function pickedDraft(color: Color): CustomDraft {
  return { color, rawText: color.toString("hex"), invalidAttempt: false };
}

function hsb(hex: string): Color {
  return parseColor(hex).toFormat("hsb");
}

/**
 * The hex field. RAC's ColorField reports only PARSED colours, on commit
 * (blur or Enter), and that commit silently reverts unparsable text to the
 * previous colour -- so judging it by its own onChange would let a stale
 * colour through. This adapter judges the RAW text instead, from the
 * vendored InputBase's own input events, and reports it upward. Both the
 * colour and the shown text are the popover draft's: the input's plain
 * `value` prop beats the one RAC's context supplies (mergeProps), so RAC's
 * revert never reaches the screen.
 */
function ValidatedHexField({
  color,
  rawText,
  invalidAttempt,
  onText,
}: {
  color: Color;
  rawText: string;
  invalidAttempt: boolean;
  onText: (raw: string) => void;
}) {
  return (
    // No onChange, on purpose: RAC's onChange is its commit of a PARSED
    // colour, and wiring it would reopen the stale-value path. The price is
    // that the arrow keys and the mouse wheel do not step the hex.
    <ColorField
      value={color}
      // RAC's autoFocus, not the input's: React focuses an autoFocus input
      // during commit, before the popover's focus scope registers, and the
      // editor slideout's contained scope then pulls focus back out.
      // react-aria focuses in an effect, once the scope is in place.
      autoFocus
      isInvalid={invalidAttempt}
      // The draft's text is the user's choice; RAC's own invalid styling
      // would otherwise also fire on its native validation.
      validationBehavior="aria"
      className="flex flex-col gap-1.5"
    >
      <Label>Hex</Label>
      <InputBase
        size="sm"
        isInvalid={invalidAttempt}
        data-testid="custom-color-hex"
        value={rawText}
        onChange={(event) => onText(event.currentTarget.value)}
      />
      {/* HintText takes ColorField's errorMessage slot, so RAC links it to
          the input through aria-describedby. */}
      {invalidAttempt && (
        <HintText isInvalid size="sm">
          {HEX_ERROR}
        </HintText>
      )}
    </ColorField>
  );
}

/** One chart-surface preview: the event's point marker, a span with its
 * faint fill, and short labels, drawn with the chart's own marker style
 * (charts/markerStyle.ts) over the plot surface of that theme. */
function MarkerPreview({ dark, color, dash }: { dark: boolean; color: string; dash: EventDash }) {
  const theme = chartTheme(dark);
  const surfaces = plotSurfaces();
  const dasharray = svgDashArray(dash);
  const caption = dark ? "Dark chart" : "Light chart";
  const font = { fontFamily: LABEL_FONT_FAMILY, fontSize: LABEL_FONT_SIZE };
  return (
    <figure className="flex min-w-0 flex-col gap-1">
      <figcaption className="text-xs text-tertiary">{caption}</figcaption>
      <svg
        data-testid={`custom-color-preview-${dark ? "dark" : "light"}`}
        viewBox="0 0 128 56"
        className="h-14 w-full rounded-md ring-1 ring-secondary"
        style={{ backgroundColor: dark ? surfaces.dark : surfaces.light }}
        role="img"
        aria-label={`${caption} preview`}
      >
        <line
          data-marker="point"
          x1="16.5"
          x2="16.5"
          y1="0"
          y2="56"
          stroke={color}
          strokeWidth={MARKER_LINE_WIDTH}
          {...(dasharray !== undefined && { strokeDasharray: dasharray })}
        />
        <text x="20" y="12" fill={theme.muted} {...font}>
          mark
        </text>
        <rect
          data-marker="span"
          x="64.5"
          y="0.5"
          width="48"
          height="55"
          fill={withAlpha(color, SPAN_FILL_ALPHA)}
          stroke={color}
          strokeWidth={MARKER_LINE_WIDTH}
          {...(dasharray !== undefined && { strokeDasharray: dasharray })}
        />
        <text x="68" y="12" fill={theme.ink} {...font}>
          span
        </text>
      </svg>
    </figure>
  );
}

const THUMB =
  "size-5 rounded-full border-2 border-white shadow-[0_0_0_1px_rgb(0_0_0/0.4),inset_0_0_0_1px_rgb(0_0_0/0.4)] data-focus-visible:size-6";

/** The popover body, mounted per open: its draft starts from the editor's
 * colour every time, and closing it (Cancel, Escape, an outside click)
 * simply discards it. */
function CustomColorBody({
  initial,
  dash,
  isDisabled,
  onApply,
  onCancel,
}: {
  initial: string;
  dash: EventDash;
  isDisabled: boolean;
  onApply: (hex: string) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<CustomDraft>(() => pickedDraft(hsb(initial)));
  // A wheel or area change is a colour the user produced: it is valid.
  const fromPicker = (color: Color) => setDraft(pickedDraft(color));
  // Typing keeps its text either way; only valid text moves the colour.
  const fromText = (rawText: string) => {
    const hex = normalizeHexInput(rawText);
    setDraft((prev) =>
      hex === null
        ? { ...prev, rawText, invalidAttempt: true }
        : { color: hsb(hex), rawText, invalidAttempt: false },
    );
  };
  const hex = draft.color.toString("hex").toLowerCase();

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="relative mx-auto size-56">
        <ColorWheel
          outerRadius={112}
          innerRadius={92}
          value={draft.color}
          onChange={fromPicker}
          className="absolute inset-0"
        >
          <ColorWheelTrack />
          <ColorThumb className={THUMB} />
        </ColorWheel>
        {/* A centring wrapper: RAC styles the area itself position: relative. */}
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <ColorArea
            colorSpace="hsb"
            xChannel="saturation"
            yChannel="brightness"
            value={draft.color}
            onChange={fromPicker}
            className="pointer-events-auto size-32 rounded-md"
          >
            <ColorThumb className={THUMB} />
          </ColorArea>
        </div>
      </div>
      <ValidatedHexField
        color={draft.color}
        rawText={draft.rawText}
        invalidAttempt={draft.invalidAttempt}
        onText={fromText}
      />
      <div className="grid grid-cols-2 gap-2">
        <MarkerPreview dark={false} color={hex} dash={dash} />
        <MarkerPreview dark color={hex} dash={dash} />
      </div>
      <div className="flex justify-end gap-2">
        <Button size="sm" color="secondary" data-testid="custom-color-cancel" onPress={onCancel}>
          Cancel
        </Button>
        <Button
          size="sm"
          color="primary"
          data-testid="custom-color-apply"
          isDisabled={isDisabled || draft.invalidAttempt}
          onPress={() => onApply(hex)}
        >
          Use colour
        </Button>
      </div>
    </div>
  );
}

/** Custom…: the row's last item, a second tab stop after the swatch listbox. */
function CustomColorPopover({
  value,
  dash,
  isDisabled,
  onChange,
}: {
  value: string;
  dash: EventDash;
  isDisabled: boolean;
  onChange: (hex: string) => void;
}) {
  return (
    <DialogTrigger>
      <Button
        size="sm"
        color="secondary"
        data-testid="editor-color-custom-open"
        isDisabled={isDisabled}
      >
        Custom…
      </Button>
      {/* The vendored popover's surface tokens, not select/popover.tsx's
          trigger-width sizing; RAC caps the height, so it scrolls. */}
      <Popover
        placement="bottom end"
        offset={4}
        className="w-[min(320px,calc(100vw-32px))] overflow-y-auto rounded-lg bg-primary shadow-lg ring-1 ring-secondary_alt outline-hidden"
      >
        <Dialog
          aria-label="Custom event colour"
          data-testid="custom-color-dialog"
          className="outline-hidden"
        >
          {({ close }) => (
            <CustomColorBody
              initial={value}
              dash={dash}
              isDisabled={isDisabled}
              onCancel={close}
              onApply={(hex) => {
                onChange(hex);
                close();
              }}
            />
          )}
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}

export function EventColorField({
  value,
  onChange,
  dash,
  legacyColor,
  isDisabled = false,
}: EventColorFieldProps) {
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
      {/* Custom… cannot be a listbox item (it is a button), so the row is
          a wrapper holding the picker and then Custom…, last in DOM and
          visual order. */}
      <div className="clear-left mt-1 flex flex-wrap items-center gap-1">
        <ColorSwatchPicker
          aria-labelledby={legendId}
          className="flex flex-wrap gap-1"
          value={current}
          onChange={(color) => onChange(color.toString("hex").toLowerCase())}
        >
          {slots.map((slot) => (
            <SwatchItem key={slot.hex} slot={slot} isDisabled={isDisabled} />
          ))}
        </ColorSwatchPicker>
        <CustomColorPopover
          value={current}
          dash={dash}
          isDisabled={isDisabled}
          onChange={onChange}
        />
      </div>
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
            isDisabled={isDisabled}
            onPress={() => onChange(DEFAULT_EVENT_COLOR)}
          >
            Use {defaultName}
          </Button>
        </div>
      )}
    </fieldset>
  );
}
