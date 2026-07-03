import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
// dashboard.css ported verbatim (Task 8) — imported before App renders,
// same load-order relationship as dashboard.html's <link> in <head>.
import "./dashboard.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
