// Global chrome (UX spec §7, reworked per spec 2026-07-17 decision 9):
// left = brand + the permanent search trigger (never moves with mode);
// right = pause glyph · export glyph (live-only) · ⋮ menu.
// Presets and events live on the subject page; status text/dot are GONE
// — "Historical"/"No data" were redundant (ReviewBar badge, EmptyState)
// and live connection loss now renders as the Reconnecting banner
// (ReconnectingBanner.tsx), which replaced the status cluster as the
// `connection` state's one render site (Task 9).
import {
  Command,
  DotsVertical,
  Download01,
  Moon01,
  PauseCircle,
  Play,
  Sun,
  Upload01,
} from "@untitledui/icons";

import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useIsPaused, useReviewStore } from "../data/reviewStore";
import { SearchTrigger } from "../ui/SearchTrigger";
import {
  EXPORT_BINDING,
  formatBinding,
  IMPORT_BINDING,
  PALETTE_BINDING,
  THEME_BINDING,
} from "../ui/shortcuts";
import { useUiStore } from "../ui/uiStore";
import { exportLoadedDocument, openImportPicker } from "./ImportExport";
import { MarkControl } from "./MarkControl";

export function AppBar() {
  const hasData = useReviewStore((s) => s.rawMonitorSessions !== null);
  const mode = useReviewStore((s) => s.mode);
  const paused = useIsPaused();
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const theme = useUiStore((s) => s.theme);
  const { toggleTheme, openPalette } = useUiStore((s) => s.actions);

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between gap-3 border-b border-secondary px-4"
    >
      <div className="flex items-center gap-3">
        <div data-testid="brand" className="flex items-center gap-2 text-sm font-semibold">
          <span aria-hidden className="text-brand-500">
            ⬡
          </span>
          otto monitor
        </div>
        {/* CommandLayer only mounts inside App's hasData branch (no
            shortcuts on the EmptyState screen per spec) — showing this
            trigger earlier would queue an openPalette() with nothing
            listening, then spring the palette open the moment data loads. */}
        {hasData && <SearchTrigger />}
      </div>
      <div className="flex items-center gap-2">
        {mode === "live" && hasData && <MarkControl />}
        {mode === "live" && (
          <ButtonUtility
            aria-label={paused ? "Resume" : "Pause"}
            tooltip={paused ? "Resume" : "Pause"}
            data-testid="pause-toggle"
            icon={paused ? Play : PauseCircle}
            color="tertiary"
            size="sm"
            onClick={togglePause}
          />
        )}
        {mode === "live" && (
          <ButtonUtility
            aria-label="Export"
            tooltip="Export"
            data-testid="export-button"
            icon={Download01}
            color="tertiary"
            size="sm"
            isDisabled={!hasData}
            onClick={exportLoadedDocument}
          />
        )}
        <Dropdown.Root>
          <ButtonUtility
            aria-label="More actions"
            data-testid="overflow-menu"
            icon={DotsVertical}
            color="tertiary"
          />
          <Dropdown.Popover>
            <Dropdown.Menu>
              <Dropdown.Section>
                <Dropdown.Item
                  id="import"
                  label="Import…"
                  icon={Upload01}
                  addon={formatBinding(IMPORT_BINDING)}
                  onAction={openImportPicker}
                  data-testid="menu-import"
                />
                <Dropdown.Item
                  id="export"
                  label="Export"
                  icon={Download01}
                  addon={formatBinding(EXPORT_BINDING)}
                  onAction={exportLoadedDocument}
                  isDisabled={!hasData}
                  data-testid="menu-export"
                />
              </Dropdown.Section>
              <Dropdown.Separator />
              <Dropdown.Section>
                <Dropdown.Item
                  id="theme"
                  label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                  icon={theme === "dark" ? Sun : Moon01}
                  addon={formatBinding(THEME_BINDING)}
                  onAction={toggleTheme}
                  data-testid="menu-theme"
                />
              </Dropdown.Section>
              {hasData && (
                <>
                  <Dropdown.Separator />
                  <Dropdown.Section>
                    <Dropdown.Item
                      id="shortcuts"
                      label="Keyboard shortcuts…"
                      icon={Command}
                      addon={formatBinding(PALETTE_BINDING)}
                      onAction={openPalette}
                      data-testid="menu-shortcuts"
                    />
                  </Dropdown.Section>
                </>
              )}
            </Dropdown.Menu>
          </Dropdown.Popover>
        </Dropdown.Root>
      </div>
    </header>
  );
}
