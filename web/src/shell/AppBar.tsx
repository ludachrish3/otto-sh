// Global chrome (UX spec §7): brand fixed left; far right = status text ·
// status dot · ⋯ menu (Import/Export/theme — the infrequent actions).
// Pause appears here in live mode only — a later phase; review has none.
import { useState } from "react";

import { useReviewStore } from "../data/reviewStore";
import { loadTheme, saveTheme, type Theme } from "../theme";
import { OverflowMenu } from "../ui/Menu";
import { exportLoadedDocument, openImportPicker } from "./ImportExport";

export function AppBar() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const [theme, setTheme] = useState<Theme>(loadTheme);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    saveTheme(next);
    setTheme(next);
  };

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between border-b border-gray-200 px-4
        dark:border-gray-800"
    >
      <div data-testid="brand" className="flex items-center gap-2 text-sm font-semibold">
        <span aria-hidden className="text-brand-500">
          ⬡
        </span>
        otto monitor
      </div>
      <div className="flex items-center gap-3">
        <span data-testid="status-text" className="text-sm text-gray-500 dark:text-gray-400">
          {hasData ? "Historical" : "No data"}
        </span>
        <span
          data-testid="status-dot"
          className={`h-2.5 w-2.5 rounded-full ${
            hasData ? "bg-status-historical" : "bg-gray-300 dark:bg-gray-600"
          }`}
        />
        <OverflowMenu
          items={[
            { id: "import", label: "Import…", onAction: openImportPicker, testId: "menu-import" },
            {
              id: "export",
              label: "Export",
              onAction: exportLoadedDocument,
              isDisabled: !hasData,
              testId: "menu-export",
            },
            {
              id: "theme",
              label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
              onAction: toggleTheme,
              testId: "menu-theme",
            },
          ]}
        />
      </div>
    </header>
  );
}
