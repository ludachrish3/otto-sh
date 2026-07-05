// The `<header>` chrome: host selector, theme/pause/export controls, and the
// SSE status readout. IDs match dashboard.html's markup exactly (the
// DOM-parity contract) — see docs/superpowers/plans/2026-07-02-monitor-
// phase2-react-port.md's Global Constraints.
import { useState } from "react";

import type { Hosts } from "../api/types.gen";
import { statusDotClass, statusText, useMonitorActions, useMonitorStore } from "../store";
import { applyBodyTheme, loadTheme, saveTheme, type Theme } from "../theme";

// Module-level stable reference: a fresh `[]` literal returned from a
// zustand selector is a NEW array on every call, which — since zustand v5's
// `useSyncExternalStore` snapshot comparison is `Object.is` — makes the
// store look like it's changing on every render and can trigger React error
// #185's "getSnapshot should be cached" infinite loop. Hoisting the fallback
// keeps the selector's return value referentially stable when `meta` is
// null.
const EMPTY_HOSTS: Hosts = [];

function Header() {
  const hosts = useMonitorStore((s) => s.meta?.hosts ?? EMPTY_HOSTS);
  const selectedHost = useMonitorStore((s) => s.selectedHost);
  const connection = useMonitorStore((s) => s.connection);
  const paused = useMonitorStore((s) => s.paused);
  const { selectHost, togglePause } = useMonitorActions();

  // `theme.ts` already applied the persisted theme to `body.light` at module
  // load (before this component ever rendered — see its comment), so this
  // state only needs to seed the button's own icon/title from the same
  // source, not re-apply anything on mount.
  const [theme, setTheme] = useState<Theme>(() => loadTheme());

  const handleThemeToggle = () => {
    const next: Theme = theme === "light" ? "dark" : "light";
    setTheme(next);
    applyBodyTheme(next);
    saveTheme(next);
  };

  // dashboard.js's pause-btn is disabled until the stream reaches 'live',
  // and re-disabled on disconnect — enabled exactly when connection is live
  // (paused or not).
  const pauseDisabled = connection !== "live";

  return (
    <header>
      <h1>Otto Monitor</h1>
      <select
        id="host-select"
        title="Select host to view"
        value={selectedHost ?? ""}
        onChange={(e) => {
          selectHost(e.target.value || null);
        }}
      >
        {hosts.length === 0 ? (
          <option value="">historical</option>
        ) : (
          <>
            <option value="" disabled>
              Select host
            </option>
            {hosts.map((host) => (
              <option key={host} value={host}>
                {host}
              </option>
            ))}
          </>
        )}
      </select>
      <button
        type="button"
        id="theme-btn"
        title={theme === "light" ? "Switch to dark mode" : "Switch to light mode"}
        onClick={handleThemeToggle}
      >
        {theme === "light" ? "🌙" : "☀️"}
      </button>
      <button
        type="button"
        id="pause-btn"
        title={paused ? "Resume live updates" : "Pause live updates"}
        disabled={pauseDisabled}
        onClick={togglePause}
      >
        {paused ? "▶" : "⏸"}
      </button>
      <a id="export-btn" href="/api/export/json" title="Download metrics as JSON">
        ↓ Export
      </a>
      <span id="status-label">{statusText(connection, paused)}</span>
      <div id="status-dot" className={statusDotClass(connection, paused)} />
    </header>
  );
}

export default Header;
