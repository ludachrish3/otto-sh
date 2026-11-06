// The detail affordance for a crowded label cluster (#920). The canvas draws
// at most LABEL_LANES_MAX label lanes and folds the rest into a " (+N)" count
// on one visible label; this lays a transparent button over each such label,
// and pressing it lists every hidden event. The geometry comes from the same
// layout the canvas drew (ChartPanel passes its placements), so a button
// sits exactly on its label at every width.
import { Button, Dialog, DialogTrigger, Popover } from "react-aria-components";

import { formatClockTime } from "../data/time";
import { type EventMarker, LANE_LABEL_STEP_PX, type LabelPlacement, laneTopPx } from "./options";

/** At most this many hidden labels are named in a button's accessible name;
 * the popover lists them all. */
const NAMED_MAX = 10;

/** The button's accessible name. It starts with the label's visible text
 * (WCAG 2.5.3, label in name), then names the hidden events. */
function overflowName(visibleText: string, hidden: EventMarker[]): string {
  const noun = hidden.length === 1 ? "event" : "events";
  const named = hidden.slice(0, NAMED_MAX).map((m) => m.label);
  const rest = hidden.length - named.length;
  const list = named.join(", ") + (rest > 0 ? `, and ${rest} more` : "");
  return `${visibleText}: ${hidden.length} more ${noun}: ${list}`;
}

export function ClusterOverflow(props: {
  placements: LabelPlacement[];
  markers: EventMarker[];
  /** The plot's offset inside the chart element (GRID.left / GRID.top). */
  plotLeftPx: number;
  plotTopPx: number;
  /** Opens the event editor on a listed event. Omitted (a read-only view),
   * the list is plain text. */
  onEventOpen?: ((eventId: number) => void) | undefined;
}) {
  const { placements, markers, plotLeftPx, plotTopPx, onEventOpen } = props;
  const byId = new Map(markers.map((m) => [m.id, m]));
  return placements
    .filter((p) => p.overflowIds.length > 0)
    .map((p) => {
      const hidden = p.overflowIds.flatMap((id) => {
        const m = byId.get(id);
        return m === undefined ? [] : [m];
      });
      const name = overflowName(p.text, hidden);
      return (
        <DialogTrigger key={p.id}>
          <Button
            aria-label={name}
            data-testid={`cluster-overflow-${p.id}`}
            className="pointer-events-auto absolute cursor-pointer rounded-sm bg-transparent
              outline-focus-ring focus-visible:outline-2"
            style={{
              left: plotLeftPx + p.leftPx,
              top: plotTopPx + laneTopPx(p.lane),
              width: p.widthPx,
              height: LANE_LABEL_STEP_PX,
            }}
          />
          <Popover placement="bottom start" offset={4}>
            <Dialog
              data-testid="cluster-overflow-list"
              aria-label={name}
              // The Popover's own style carries react-aria's computed
              // max-height (the room between the trigger and the viewport
              // edge); inheriting it and scrolling keeps a long list on
              // screen, since the modal popover blocks the page's scroll.
              className="max-h-[inherit] max-w-80 overflow-y-auto rounded-xl bg-primary p-2
                shadow-lg ring ring-secondary_alt outline-hidden"
            >
              {({ close }) => (
                <ul className="flex flex-col gap-0.5 text-sm">
                  {hidden.map((m) => {
                    const row = (
                      <>
                        <span className="shrink-0 text-xs text-tertiary">
                          {formatClockTime(m.fromMs)}
                        </span>
                        <span className="min-w-0 truncate">{m.label}</span>
                      </>
                    );
                    // An id-less marker (negative synthetic id) has no wire
                    // row to edit, so it is listed but never offered as one.
                    return (
                      <li key={m.id}>
                        {onEventOpen !== undefined && m.id >= 0 ? (
                          <Button
                            data-testid={`cluster-overflow-item-${m.id}`}
                            onPress={() => {
                              close();
                              onEventOpen(m.id);
                            }}
                            className="flex w-full cursor-pointer items-center gap-2 rounded-md
                              px-2 py-1 text-left text-primary hover:bg-primary_hover
                              focus-visible:bg-primary_hover focus:outline-hidden"
                          >
                            {row}
                          </Button>
                        ) : (
                          <span
                            data-testid={`cluster-overflow-item-${m.id}`}
                            className="flex items-center gap-2 px-2 py-1 text-secondary"
                          >
                            {row}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </Dialog>
          </Popover>
        </DialogTrigger>
      );
    });
}
