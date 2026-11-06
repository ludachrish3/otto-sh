// Event colour and dash helpers over the generated registry mirror
// (api/eventStyle.gen.ts, written from otto/models/event_style.py). The
// dashboard normalises an event's style ONCE, where an event enters the
// store (exportDoc.ts's import, fragment.ts's live merge); every consumer
// after that reads `ev.color` / `ev.dash` directly, with no fallback.
import {
  DEFAULT_EVENT_COLOR,
  DEFAULT_EVENT_DASH,
  EVENT_COLOR_PATTERN,
  EVENT_COLOR_PRESETS,
  EVENT_DASH_STYLES,
  type EventColorPreset,
  type EventDash,
} from "../api/eventStyle.gen";
import type { EventRecord } from "../api/export.gen";

/** The registry's pattern as a WHOLE-string match (it ships unanchored). */
const EVENT_COLOR_RE = new RegExp(`^(?:${EVENT_COLOR_PATTERN})$`, "u");

/** Exactly six hex digits, `#` optional: what a user may type as a colour. */
const HEX_INPUT_RE = /^#?(?<digits>[0-9a-fA-F]{6})$/u;

const DASHES: ReadonlySet<string> = new Set(EVENT_DASH_STYLES);

/** A record after `normalizeEventStyle`: a drawable colour and a known dash. */
export type EventStyled<R> = R & {
  color: string;
  dash: EventDash;
  /** The original colour when it was not hex (e.g. `"red"` from an older
   * otto): kept for diagnosis and written back by export, never drawn. */
  legacyColor?: string;
  /** The original dash when it is not a registry style: kept and written
   * back the same way, drawn as the default. */
  legacyDash?: string;
};

/** `#rgb`/`#rrggbb` (any case) → lowercase `#rrggbb`. Callers check the pattern first. */
function expandHex(hex: string): string {
  const digits = hex.slice(1).toLowerCase();
  return digits.length === 3 ? `#${[...digits].map((c) => c + c).join("")}` : `#${digits}`;
}

/** Narrows `value` to one of the registry's dash styles. */
export function isEventDash(value: string): value is EventDash {
  return DASHES.has(value);
}

/** One style field at the store boundary, the same rule for colour and
 * dash: missing → the default; drawable → `drawable`'s form of it; anything
 * else → the default, with the original returned as `legacy`. */
function resolveStyleField<T extends string>(
  value: string | null | undefined,
  drawable: (v: string) => T | null,
  fallback: T,
): { drawn: T; legacy?: string } {
  if (value == null) return { drawn: fallback };
  const drawn = drawable(value);
  return drawn === null ? { drawn: fallback, legacy: value } : { drawn };
}

/**
 * Resolves a record's colour and dash at the store boundary.
 *
 * Colour: missing or `null` → the default; a hex → lowercase `#rrggbb`;
 * anything else → the default, with the original kept as `legacyColor`.
 * Dash: missing or `null` → the default; a registry style as is; anything
 * else → the default, with the original kept as `legacyDash`.
 * Idempotent: an already-normalised record (its legacy fields included)
 * comes back unchanged. `storedEventStyle` is the inverse.
 */
export function normalizeEventStyle<R extends { color?: string | null; dash?: string | null }>(
  record: R,
): EventStyled<R> {
  const color = resolveStyleField(
    record.color,
    (c) => (EVENT_COLOR_RE.test(c) ? expandHex(c) : null),
    DEFAULT_EVENT_COLOR,
  );
  const dash = resolveStyleField(
    record.dash,
    (d) => (isEventDash(d) ? d : null),
    DEFAULT_EVENT_DASH,
  );
  return {
    ...record,
    color: color.drawn,
    dash: dash.drawn,
    ...(color.legacy !== undefined && { legacyColor: color.legacy }),
    ...(dash.legacy !== undefined && { legacyDash: dash.legacy }),
  };
}

/** A normalised record as it is stored: a kept legacy colour or dash goes
 * back in place of the drawn default, so export never destroys what the
 * import kept. */
export function storedEventStyle({
  legacyColor,
  legacyDash,
  ...record
}: EventStyled<EventRecord>): EventRecord {
  return { ...record, color: legacyColor ?? record.color, dash: legacyDash ?? record.dash };
}

/** The preset whose hex is `hex`, compared case-insensitively. */
export function presetFor(hex: string): EventColorPreset | undefined {
  const wanted = hex.toLowerCase();
  return EVENT_COLOR_PRESETS.find((p) => p.hex === wanted);
}

/** A colour's user-visible name: the preset's, or `Custom — #rrggbb`. */
export function eventColorName(hex: string): string {
  return presetFor(hex)?.name ?? `Custom — ${hex.toLowerCase()}`;
}

/** Typed colour text → lowercase `#rrggbb`, or `null` unless it is exactly
 * six hex digits with an optional leading `#`. */
export function normalizeHexInput(text: string): string | null {
  const digits = HEX_INPUT_RE.exec(text)?.groups?.["digits"];
  return digits === undefined ? null : `#${digits.toLowerCase()}`;
}
