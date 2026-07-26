import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { applyTheme, loadTheme } from "../theme";
import App from "./App";
import "./covapp.css";

// Pre-paint theme application, same trick as theme.ts's own module side
// effect — explicit here (rather than relying on a transitive import) so
// covapp's single boot entrypoint is legible without tracing imports.
applyTheme(loadTheme());

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
