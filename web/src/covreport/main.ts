// Entry point for the covreport bundle (built by vite.covreport.config.ts).
// The Jinja template loads it with `defer`, so the DOM is parsed before this
// runs — the readyState guard covers non-deferred manual loads too.
import { initReportPage } from "./sort";

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => initReportPage());
} else {
  initReportPage();
}
